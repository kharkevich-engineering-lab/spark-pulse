"""A deploy that does not fit beside the runs on a node is refused, and says what fits.

Three layers, each tested where it lives. The ledger (:mod:`tools.memory_budget`)
is arithmetic and is tested as arithmetic: fractions against a total, the OS
reserve, the suggestion rounded *down*, and the difference between a claim that
is zero and one that is unknown. The planner gathers the inputs — which runs are
on the node, by the same liveness rule the port choice uses, and what the node
says their containers hold. The pre-flight turns the result into a verdict, and
over budget is ``blocked`` because the engine would refuse anyway, only later.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from spark_pulse import tools
from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.app import create_app
from spark_pulse.tools import memory_budget as mb
from spark_pulse.tools.labels import DEPLOYMENT_LABEL

nr = importlib.import_module("spark_pulse.tools.native_runtime")
real_preflight = importlib.import_module("spark_pulse.tools.preflight")
node_stats = importlib.import_module("spark_pulse.tools.node_stats")
sim = importlib.import_module("spark_pulse.mock.preflight")

GIB = 1024**3
TOTAL = 100 * GIB
RESERVE = mb.OS_RESERVE_BYTES


# ── The ledger ──────────────────────────────────────────────────────────────


class TestFractionInCommand:
    def test_reads_either_engine_s_flag_and_either_spelling(self):
        assert (
            mb.fraction_in_command("vllm serve m --gpu-memory-utilization 0.8") == 0.8
        )
        assert mb.fraction_in_command("x --gpu-memory-utilization=.35 --port 1") == 0.35
        assert (
            mb.fraction_in_command("python -m sglang --mem-fraction-static 0.7") == 0.7
        )

    def test_inside_a_wrapped_command_and_the_last_one_wins(self):
        command = "bash -c 'vllm serve m --gpu-memory-utilization 0.5 --gpu-memory-utilization 0.6'"
        assert mb.fraction_in_command(command) == 0.6

    def test_nothing_or_nonsense_is_no_fraction(self):
        assert mb.fraction_in_command("llama-server -m x.gguf") is None
        assert mb.fraction_in_command("--gpu-memory-utilization 4") is None
        assert mb.fraction_in_command("") is None


class TestRecordFraction:
    def test_a_recorded_fraction_is_read_as_given(self):
        assert mb.record_fraction({"gpu_memory_utilization": 0.8}) == 0.8

    def test_a_recorded_none_is_an_engine_that_takes_none(self):
        # Even with a fraction in the command: the planner already decided.
        record = {
            "gpu_memory_utilization": None,
            "launch_command": "--gpu-memory-utilization 0.5",
        }
        assert mb.record_fraction(record) is None

    def test_an_older_record_is_read_from_its_command(self):
        record = {"launch_command": "vllm serve m --gpu-memory-utilization 0.7"}
        assert mb.record_fraction(record) == 0.7
        assert mb.record_fraction({"launch_command": "llama-server -m x"}) is None

    def test_a_value_that_is_not_a_fraction_is_none(self):
        assert mb.record_fraction({"gpu_memory_utilization": "lots"}) is None
        assert mb.record_fraction({"gpu_memory_utilization": 3}) is None


class TestBudgetForNode:
    def test_a_fraction_claims_its_share_of_the_total(self):
        budget = mb.budget_for_node(
            "", "", "spark", [("a", "qwen", 0.8)], TOTAL, {}, claim_fraction=0.1
        )

        holder = budget.holders[0]
        assert (holder.source, holder.bytes) == ("fraction", int(0.8 * TOTAL))
        assert budget.held_bytes == int(0.8 * TOTAL)
        assert budget.left_bytes == TOTAL - int(0.8 * TOTAL) - RESERVE
        assert budget.claim_bytes == int(0.1 * TOTAL)
        assert budget.fits is True and budget.complete is True

    def test_the_reserve_comes_off_what_is_left(self):
        budget = mb.budget_for_node(
            "", "", "spark", [("a", "a", 0.5)], TOTAL, {}, 0.5, reserve_bytes=0
        )
        assert budget.left_bytes == TOTAL - int(0.5 * TOTAL)
        assert budget.fits is True

        reserved = mb.budget_for_node(
            "", "", "spark", [("a", "a", 0.5)], TOTAL, {}, 0.5
        )
        assert reserved.fits is False

    def test_the_suggestion_is_rounded_down_and_then_fits(self):
        # 100 GiB, 0.80 held, 4 GiB reserved: 16 GiB left, 0.16 of the total.
        budget = mb.budget_for_node("", "", "s", [("a", "a", 0.8)], TOTAL, {}, 0.5)
        assert budget.max_fraction == 0.16
        assert budget.fits is False

        taken = mb.budget_for_node("", "", "s", [("a", "a", 0.8)], TOTAL, {}, 0.16)
        assert taken.fits is True

    def test_rounding_never_lands_a_hair_over(self):
        total = 121 * GIB + 12345
        budget = mb.budget_for_node("", "", "s", [("a", "a", 0.8)], total, {}, 0.5)
        assert budget.max_fraction is not None
        assert budget.max_fraction * total <= budget.left_bytes
        assert budget.max_fraction + 0.01 > budget.left_bytes / total

    def test_nothing_left_is_zero_not_negative(self):
        budget = mb.budget_for_node(
            "", "", "s", [("a", "a", 0.9), ("b", "b", 0.5)], TOTAL, {}, 0.1
        )
        assert budget.left_bytes == 0
        assert budget.max_fraction == 0.0
        assert budget.fits is False

    def test_an_engine_with_no_fraction_is_measured(self):
        budget = mb.budget_for_node(
            "", "", "s", [("b", "bonsai", None)], TOTAL, {"b": 30 * GIB}, 0.5
        )

        holder = budget.holders[0]
        assert (holder.source, holder.bytes) == ("measured", 30 * GIB)
        assert budget.complete is True

    def test_a_process_without_memory_is_unknown_not_zero(self):
        budget = mb.budget_for_node(
            "", "", "s", [("b", "b", None)], TOTAL, {"b": None}, 0.1
        )

        assert budget.holders[0].bytes is None
        assert "not its memory" in budget.holders[0].reason
        assert budget.complete is False
        assert budget.to_dict()["unknown"] == ["b"]

    def test_a_run_the_node_reports_nothing_for_is_unknown(self):
        budget = mb.budget_for_node("", "", "s", [("b", "b", None)], TOTAL, {}, 0.1)

        assert budget.holders[0].bytes is None
        assert "no GPU process" in budget.holders[0].reason

    def test_an_unasked_node_leaves_every_claim_unknown(self):
        budget = mb.budget_for_node(
            "",
            "",
            "s",
            [("a", "a", 0.8), ("b", "b", None)],
            None,
            None,
            0.1,
            reason="could not ask s",
        )

        assert [h.bytes for h in budget.holders] == [None, None]
        assert budget.holders[0].fraction == 0.8
        assert budget.left_bytes is None and budget.max_fraction is None
        assert budget.fits is None and budget.complete is False

    def test_an_empty_node_is_complete_with_nothing_in_it(self):
        budget = mb.budget_for_node("", "", "s", [], None, {}, 0.9)
        assert budget.complete is True and budget.holders == []

    def test_a_claim_in_bytes_stands_in_for_a_fraction(self):
        budget = mb.budget_for_node(
            "", "", "s", [("a", "a", 0.8)], TOTAL, {}, None, claim_bytes=20 * GIB
        )
        assert budget.claim_bytes == 20 * GIB
        assert budget.fits is False


# ── What the node says its containers hold ──────────────────────────────────


def _stats_service(stats, containers):
    return SimpleNamespace(
        get_node_stats=lambda: stats,
        list_managed_containers=lambda: containers,
    )


def _container(cid: str, deployment: str):
    return SimpleNamespace(
        id=cid, name=f"c-{cid}", labels={DEPLOYMENT_LABEL: deployment}
    )


class TestMemoryByDeployment:
    def test_unified_memory_totals_from_the_host_and_sums_per_run(self):
        stats = pb.NodeStats(memory=pb.MemoryStat(total_bytes=TOTAL))
        stats.gpus.add(index=0, name="GB10")
        stats.processes.append(
            pb.GpuProcess(pid=1, container_id="aaaaaaaaaaaa", used_memory_bytes=2 * GIB)
        )
        stats.processes.append(
            pb.GpuProcess(pid=2, container_id="aaaaaaaaaaaa", used_memory_bytes=3 * GIB)
        )
        stats.processes.append(pb.GpuProcess(pid=3, used_memory_bytes=9 * GIB))
        service = _stats_service(stats, [_container("aaaaaaaaaaaa", "dep-a")])

        total, held = node_stats.memory_by_deployment(service)

        assert total == TOTAL
        # The stray process is nobody's run, so it is nobody's claim.
        assert held == {"dep-a": 5 * GIB}

    def test_a_gpu_that_reports_its_own_total_is_the_total(self):
        stats = pb.NodeStats(memory=pb.MemoryStat(total_bytes=TOTAL))
        stats.gpus.add(index=0, memory_total_bytes=80 * GIB)

        total, _ = node_stats.memory_by_deployment(_stats_service(stats, []))

        assert total == 80 * GIB

    def test_a_process_without_memory_makes_its_run_unknown(self):
        stats = pb.NodeStats()
        stats.processes.append(pb.GpuProcess(pid=1, container_id="bbbbbbbbbbbb"))
        stats.processes.append(
            pb.GpuProcess(pid=2, container_id="bbbbbbbbbbbb", used_memory_bytes=GIB)
        )
        service = _stats_service(stats, [_container("bbbbbbbbbbbb", "dep-b")])

        total, held = node_stats.memory_by_deployment(service)

        assert total is None
        assert held == {"dep-b": None}


# ── The planner ─────────────────────────────────────────────────────────────


RECIPE = "bundled/qwen2.5-0.5b-instruct"


def _record(rid: str, name: str, nodes=None, **extra):
    return {
        "id": rid,
        "name": name,
        "status": "running",
        "nodes": nodes,
        "ranks": [
            {"rank": r, "node": n, "container_name": f"c-{rid}-{r}"}
            for r, n in enumerate(nodes or [""])
        ],
        **extra,
    }


class FakeNode:
    """A node service answering stats, and counting that it was asked."""

    def __init__(self, total=TOTAL, held=None, fail: Exception | None = None):
        self.total, self.held, self.fail = total, held or {}, fail
        self.asked = 0

    def __call__(self, _address):
        return self

    def get_node_stats(self):
        self.asked += 1
        if self.fail:
            raise self.fail
        stats = pb.NodeStats(memory=pb.MemoryStat(total_bytes=self.total))
        for index, (deployment, used) in enumerate(self.held.items()):
            stats.processes.append(
                pb.GpuProcess(
                    pid=index + 1,
                    container_id=f"{index:012d}",
                    used_memory_bytes=used,
                )
            )
        return stats

    def list_managed_containers(self):
        return [_container(f"{i:012d}", d) for i, d in enumerate(self.held)]


class TestPlannerLedger:
    def test_the_plan_carries_the_fraction_the_command_renders(self):
        plan = tools.deploy_dispatch.plan_deployment(
            RECIPE, params={"gpu_memory_utilization": 0.35}
        )

        assert plan["gpu_memory_utilization"] == 0.35
        assert "--gpu-memory-utilization 0.35" in plan["launch_command"]

    def test_a_fraction_engine_handed_none_takes_its_default(self):
        engine = SimpleNamespace(
            spec=SimpleNamespace(
                runtime=SimpleNamespace(
                    param_flags={"gpu_memory_utilization": "--gpu-memory-utilization"}
                )
            )
        )
        assert nr._claimed_fraction(engine, "vllm serve m", {}) == 0.9
        assert (
            nr._claimed_fraction(
                engine, "vllm serve m", {"gpu_memory_utilization": 0.4}
            )
            == 0.4
        )
        assert (
            nr._claimed_fraction(engine, "vllm serve m", {"gpu_memory_utilization": 7})
            == 0.9
        )

    def test_an_engine_that_maps_no_fraction_claims_none(self):
        engine = SimpleNamespace(
            spec=SimpleNamespace(runtime=SimpleNamespace(param_flags={}))
        )
        assert nr._claimed_fraction(engine, "--gpu-memory-utilization 0.5", {}) is None

    def test_an_empty_node_is_not_asked(self):
        node = FakeNode()

        budget = nr._memory_budget([], 0.5, "", node)

        assert node.asked == 0
        assert budget[0]["holders"] == [] and budget[0]["total_bytes"] is None

    def test_a_fraction_co_tenant_is_budgeted_from_its_record(self):
        nr._save_records([_record("q", "qwen", gpu_memory_utilization=0.8)])

        (entry,) = nr._memory_budget([], 0.5, "", FakeNode())

        assert entry["holders"][0]["name"] == "qwen"
        assert entry["holders"][0]["source"] == "fraction"
        assert entry["max_fraction"] == 0.16
        assert entry["fits"] is False

    def test_a_no_fraction_co_tenant_is_measured_through_the_node(self):
        nr._save_records([_record("b", "bonsai", gpu_memory_utilization=None)])

        (entry,) = nr._memory_budget([], 0.5, "", FakeNode(held={"b": 30 * GIB}))

        assert entry["holders"][0]["source"] == "measured"
        assert entry["holders"][0]["bytes"] == 30 * GIB
        assert entry["fits"] is True and entry["complete"] is True

    def test_an_unreachable_node_leaves_its_runs_unknown(self):
        nr._save_records([_record("b", "bonsai", gpu_memory_utilization=None)])

        (entry,) = nr._memory_budget([], 0.1, "", FakeNode(fail=OSError("no route")))

        assert entry["holders"][0]["bytes"] is None
        assert "no route" in entry["reason"]
        assert entry["complete"] is False

    def test_runs_are_counted_on_their_own_nodes_only(self):
        nr._save_records(
            [
                _record("far", "far", ["10.0.0.3"], gpu_memory_utilization=0.8),
                _record("near", "near", ["10.0.0.1"], gpu_memory_utilization=0.3),
            ]
        )

        budget = nr._memory_budget(["10.0.0.1", "10.0.0.2"], 0.5, "", FakeNode())

        by_node = {e["node"]: e for e in budget}
        assert [h["name"] for h in by_node["10.0.0.1"]["holders"]] == ["near"]
        assert by_node["10.0.0.2"]["holders"] == []

    def test_a_stopped_run_with_orphans_is_counted_on_the_orphans_nodes(self):
        nr._save_records(
            [
                _record(
                    "o",
                    "orphaned",
                    ["10.0.0.1", "10.0.0.2"],
                    status="stopped",
                    gpu_memory_utilization=0.8,
                    orphans=[{"node": "10.0.0.2", "container_name": "c-o-1"}],
                ),
                _record("gone", "gone", ["10.0.0.1"], status="stopped"),
            ]
        )

        budget = nr._memory_budget(["10.0.0.1", "10.0.0.2"], 0.5, "", FakeNode())

        by_node = {e["node"]: e for e in budget}
        assert by_node["10.0.0.1"]["holders"] == []
        assert [h["name"] for h in by_node["10.0.0.2"]["holders"]] == ["orphaned"]

    def test_the_run_being_planned_is_not_its_own_co_tenant(self):
        nr._save_records([_record("me", "me", gpu_memory_utilization=0.8)])

        (entry,) = nr._memory_budget([], 0.8, "me", FakeNode())

        assert entry["holders"] == []

    def test_an_older_record_is_read_from_its_launch_command(self):
        nr._save_records(
            [
                _record(
                    "old",
                    "old",
                    launch_command="vllm serve m --gpu-memory-utilization 0.6",
                )
            ]
        )

        (entry,) = nr._memory_budget([], 0.5, "", FakeNode())

        assert entry["holders"][0]["fraction"] == 0.6


# ── The pre-flight verdict ──────────────────────────────────────────────────


def _budget(**overrides):
    entry = mb.budget_for_node(
        "", "", "spark-01", [("q", "qwen", 0.8)], TOTAL, {}, 0.5
    ).to_dict()
    entry.update(overrides)
    return entry


def _report(budget: list[dict], **plan):
    """The real pre-flight over the simulated host, with a plan handed in."""
    return real_preflight.run(
        plan={"model": "", "image_ref": "", "memory_budget": budget, **plan},
        targets=[
            real_preflight.NodeTarget(
                id="control", label="spark-01", address="", is_control_plane=True
            )
        ],
        probe_factory=sim.probe_for,
        model_config=lambda _m: None,
    )


def _memory(report):
    return [c for c in report["checks"] if c["id"] == real_preflight.CHECK_MEMORY]


class TestPreflightVerdict:
    def test_over_budget_blocks_and_names_holder_and_remedy(self):
        report = _report([_budget()])

        (check,) = _memory(report)
        assert report["verdict"] == "blocked"
        assert check["status"] == "fail"
        assert "run qwen holds 0.80" in check["observed"]
        assert "at most 0.16 is left" in check["observed"]
        assert "gpu_memory_utilization to 0.16" in check["remedy"]
        # One memory answer, not two that disagree.
        assert not [c for c in report["checks"] if c["id"] == "vram"]

    def test_within_budget_passes(self):
        entry = mb.budget_for_node(
            "", "", "spark-01", [("q", "qwen", 0.8)], TOTAL, {}, 0.1
        ).to_dict()

        report = _report([entry])

        (check,) = _memory(report)
        assert check["status"] == "pass"
        assert "fits in the" in check["observed"]
        assert report["verdict"] != "blocked"

    def test_an_unknown_co_tenant_warns_rather_than_blocks(self):
        entry = mb.budget_for_node(
            "", "", "spark-01", [("b", "bonsai", None)], TOTAL, {}, 0.1
        ).to_dict()

        report = _report([entry])

        (check,) = _memory(report)
        assert check["status"] == "warn"
        assert "could not be sized" in check["observed"]
        assert report["verdict"] != "blocked"

    def test_an_unknown_co_tenant_still_blocks_when_the_known_ones_overflow(self):
        entry = mb.budget_for_node(
            "", "", "spark-01", [("q", "q", 0.9), ("b", "b", None)], TOTAL, {}, 0.5
        ).to_dict()

        assert _memory(_report([entry]))[0]["status"] == "fail"

    def test_an_unanswered_node_warns(self):
        entry = mb.budget_for_node(
            "", "", "spark-01", [("q", "q", 0.8)], None, None, 0.1, reason="no agent"
        ).to_dict()

        (check,) = _memory(_report([entry]))
        assert check["status"] == "warn"
        assert "no agent" in check["observed"]

    def test_a_node_nobody_else_is_on_gets_no_line(self):
        entry = mb.budget_for_node("", "", "spark-01", [], None, {}, 0.9).to_dict()

        assert _memory(_report([entry])) == []
        assert _memory(_report([])) == []

    def test_a_measured_engine_is_judged_by_its_estimate(self):
        entry = mb.budget_for_node(
            "", "", "spark-01", [("q", "qwen", 0.8)], TOTAL, {}, None
        ).to_dict()
        estimate = SimpleNamespace(total_bytes=40 * GIB)
        with patch.object(
            real_preflight, "_vram_estimate", return_value=(estimate, None)
        ):
            (check,) = _memory(_report([entry]))

        assert check["status"] == "fail"
        assert "needs about 40.0 GiB" in check["observed"]
        assert "max_model_len" in check["remedy"]

    def test_a_measured_engine_with_no_estimate_warns(self):
        entry = mb.budget_for_node(
            "", "", "spark-01", [("q", "qwen", 0.5)], TOTAL, {}, None
        ).to_dict()

        (check,) = _memory(_report([entry]))

        assert check["status"] == "warn"
        assert "own claim is unknown" in check["observed"]

    def test_a_fraction_too_small_for_the_model_warns(self):
        entry = mb.budget_for_node(
            "", "", "spark-01", [("q", "qwen", 0.5)], TOTAL, {}, 0.1
        ).to_dict()
        estimate = SimpleNamespace(total_bytes=30 * GIB)
        with patch.object(
            real_preflight, "_vram_estimate", return_value=(estimate, None)
        ):
            (check,) = _memory(_report([entry]))

        assert check["status"] == "warn"
        assert "model needs about 30.0 GiB" in check["observed"]

    def test_a_peer_finds_its_own_entry_by_address(self):
        entry = _budget(node="10.0.0.11", key="10.0.0.11", label="spark-02")
        report = real_preflight.run(
            plan={"model": "", "image_ref": "", "memory_budget": [entry]},
            targets=[
                real_preflight.NodeTarget(id="p", label="spark-02", address="10.0.0.11")
            ],
            probe_factory=sim.probe_for,
            model_config=lambda _m: None,
        )

        assert _memory(report)[0]["node"] == "spark-02"


# ── The API, end to end in simulation ───────────────────────────────────────


@pytest.fixture
def client():
    return TestClient(create_app())


class TestPreviewAndCreate:
    def _first(self, client):
        created = client.post(
            "/api/deployments",
            json={
                "recipe_id": RECIPE,
                "name": "first",
                "params": {"gpu_memory_utilization": 0.8},
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["gpu_memory_utilization"] == 0.8
        return created.json()

    def test_the_preview_carries_the_budget(self, client):
        first = self._first(client)

        plan = client.post(
            "/api/deployments/plan",
            json={"recipe_id": RECIPE, "params": {"gpu_memory_utilization": 0.5}},
        ).json()

        (entry,) = plan["memory_budget"]
        assert plan["gpu_memory_utilization"] == 0.5
        assert [h["id"] for h in entry["holders"]] == [first["id"]]
        assert entry["holders"][0]["fraction"] == 0.8
        assert entry["total_bytes"] and entry["left_bytes"] is not None
        assert 0 < entry["max_fraction"] < 0.5
        assert entry["claim_fraction"] == 0.5 and entry["fits"] is False

    def test_a_create_over_budget_is_refused_and_one_within_goes_ahead(self, client):
        self._first(client)

        refused = client.post(
            "/api/deployments",
            json={"recipe_id": RECIPE, "params": {"gpu_memory_utilization": 0.5}},
        )
        assert refused.status_code == 409
        blocking = refused.json()["detail"]["preflight"]["blocking"]
        assert [c["id"] for c in blocking] == ["memory"]

        fits = client.post(
            "/api/preflight/run",
            json={"recipe_id": RECIPE, "params": {"gpu_memory_utilization": 0.1}},
        ).json()
        assert fits["verdict"] != "blocked"
