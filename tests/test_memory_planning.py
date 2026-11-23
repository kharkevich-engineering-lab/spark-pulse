"""Memory planning: the launch gate, the footprints it learns from, the sizing.

The budget (``test_memory_budget.py``) judges a plan; these hold the parts
that run after it — the check just before the engine starts, the peak a run
was seen holding, and the two inverses of the VRAM estimate the pre-flight
offers as one-click values.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import patch

from spark_pulse import tools
from spark_pulse.service import _SYSTEM_SERVICE_TEMPLATE, _USER_SERVICE_TEMPLATE
from spark_pulse.tools import footprints, vram
from spark_pulse.tools.events import EventType, severity_of

gate = importlib.import_module("spark_pulse.tools.backends.docker.memory_gate")
docker_state = importlib.import_module("spark_pulse.tools.backends.docker.state")
compaction = importlib.import_module("spark_pulse.tools.backends.docker.compaction")
nr = importlib.import_module("spark_pulse.tools.native_runtime")

GIB = 1024**3
TOTAL = 128 * GIB


# ── The gate ────────────────────────────────────────────────────────────────


class Node:
    """A node whose free memory follows a script, one reading per ask."""

    def __init__(self, frees, held=None, total=TOTAL, fail=None):
        self.frees, self.held, self.total, self.fail = (
            list(frees),
            held or {},
            total,
            fail,
        )
        self.compactions: list[tuple[bool, bool]] = []

    def __call__(self, _address):
        return self

    def compact_memory(self, drop_caches=True, compact=True):
        self.compactions.append((drop_caches, compact))
        return SimpleNamespace()

    def memory(self):
        if self.fail:
            raise self.fail
        free = self.frees.pop(0) if len(self.frees) > 1 else self.frees[0]
        return tools.node_stats.NodeMemory(
            model="unified", total_bytes=self.total, free_bytes=free, held=self.held
        )


def _plan(fraction=0.5, nodes=("",), dep="new", intent_hash=""):
    return SimpleNamespace(
        deployment_id=dep,
        rank_plans=[SimpleNamespace(node=n) for n in nodes],
        gpu_memory_utilization=fraction,
        intent_hash=intent_hash,
        memory_budget=[],
    )


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _run(node, plan, timeout=30, records=()):
    clock = Clock()
    events: list = []
    with (
        patch.object(
            tools.node_stats, "node_memory", side_effect=lambda _s: node.memory()
        ),
        patch.object(gate.placement, "_load_records", return_value=list(records)),
        patch.object(
            docker_state,
            "publish_event",
            side_effect=lambda t, d, m="", meta=None: events.append(m),
        ),
    ):
        result = gate.wait_for_room(
            node,
            plan,
            timeout,
            labels=lambda a: a or "spark-01",
            sleep=clock.sleep,
            clock=clock,
        )
    return result, events, clock


def test_room_enough_is_silent():
    result, events, _clock = _run(Node([80 * GIB]), _plan(0.5))
    assert result is None and events == []


def test_short_drops_the_cache_and_waits_for_the_memory_to_come():
    # 0.5 of 128 GiB is 64; the cache drop frees nothing, a stop does.
    node = Node([40 * GIB, 40 * GIB, 40 * GIB, 70 * GIB])
    result, events, clock = _run(node, _plan(0.5))

    assert result is None
    assert node.compactions == [(True, True)]
    assert any("waiting up to 30s" in e for e in events)
    assert "enough for this run's 64.0 GiB" in events[-1]
    assert clock.now == 10.0


def test_a_co_tenant_means_the_caches_only():
    tenant = {"id": "s", "name": "sglang", "status": "running", "ranks": [{"node": ""}]}
    node = Node([10 * GIB], held={"s": 90 * GIB})
    result, _events, _clock = _run(node, _plan(0.5), timeout=0, records=[tenant])

    assert node.compactions == [(True, False)]
    assert "run sglang holds 90.0 GiB" in result


def test_a_co_tenant_still_starting_has_its_claim_counted():
    """The free figure does not show what a starting run has not mapped yet."""
    tenant = {
        "id": "s",
        "name": "sglang",
        "status": "starting",
        "gpu_memory_utilization": 0.5,
        "ranks": [{"node": ""}],
    }
    node = Node([100 * GIB], held={"s": 4 * GIB})
    result, _events, _clock = _run(node, _plan(0.5), timeout=0, records=[tenant])

    assert result is not None
    assert "60.0 GiB of it already promised to runs still starting" in result


def test_the_failure_says_what_would_fit():
    result, _events, _clock = _run(Node([20 * GIB]), _plan(0.5), timeout=0)

    assert result.startswith("not enough free memory on spark-01")
    assert "it asks 0.50 of 128.0 GiB (64.0 GiB)" in result
    assert "lower gpu_memory_utilization to 0.15 or less" in result


def test_an_unreadable_node_is_not_short():
    result, events, _clock = _run(Node([0], fail=RuntimeError("agent gone")), _plan())
    assert result is None
    assert "the engine's own check decides" in events[0]


def test_a_run_with_no_claim_is_not_gated():
    result, events, _clock = _run(Node([1 * GIB]), _plan(None))
    assert result is None and events == []


def test_a_footprint_seen_before_is_the_claim_of_a_run_with_no_fraction():
    footprints.observe("h", TOTAL, 30 * GIB, "earlier")
    result, _events, _clock = _run(
        Node([10 * GIB]), _plan(None, intent_hash="h"), timeout=0
    )
    assert "it asks 30.0 GiB (what it held last time)" in result


def test_a_teardown_ends_the_wait():
    node = Node([1 * GIB])
    with (
        patch.object(
            tools.node_stats, "node_memory", side_effect=lambda _s: node.memory()
        ),
        patch.object(gate.placement, "_load_records", return_value=[]),
        patch.object(docker_state, "publish_event"),
    ):
        assert (
            gate.wait_for_room(
                node, _plan(0.5), 60, cancelled=lambda: True, labels=lambda a: "n"
            )
            is None
        )


def test_the_wait_is_a_warning_in_the_timeline():
    assert severity_of(EventType.DEPLOYMENT_MEMORY_WAIT) == "warning"


# ── Footprints ──────────────────────────────────────────────────────────────


def test_a_footprint_only_rises():
    assert footprints.observe("h", TOTAL, 40 * GIB, "a") == 40 * GIB
    assert footprints.observe("h", TOTAL, 30 * GIB, "b") == 40 * GIB
    assert footprints.observe("h", TOTAL, 50 * GIB, "c") == 50 * GIB
    assert footprints.lookup("h", TOTAL) == 50 * GIB


def test_a_footprint_is_filed_by_node_size_to_the_gib():
    footprints.observe("h", TOTAL, 40 * GIB)
    # MemTotal moves by a page or two across a reboot: the same key.
    assert footprints.lookup("h", TOTAL + 4096) == 40 * GIB
    assert footprints.lookup("h", 24 * GIB) is None


def test_nothing_is_not_a_measurement():
    assert footprints.observe("h", TOTAL, None) is None
    assert footprints.observe("h", TOTAL, 0) is None
    assert footprints.observe("", TOTAL, GIB) is None
    assert footprints.observe("h", None, GIB) is None
    assert footprints.lookup("h", TOTAL) is None
    assert footprints.lookup(None, TOTAL) is None


def test_the_sampler_asks_each_node_once_and_files_each_run():
    records = [
        {
            "id": "a",
            "intent_hash": "ha",
            "status": "running",
            "ready_at": "t",
            "ranks": [{"node": ""}, {"node": "10.0.0.2"}],
        },
        {
            "id": "b",
            "intent_hash": "hb",
            "status": "running",
            "ready_at": "t",
            "ranks": [{"node": ""}],
        },
        # Not ready yet, and finished: neither is a footprint.
        {"id": "c", "intent_hash": "hc", "status": "starting", "ranks": []},
        {"id": "d", "intent_hash": "hd", "status": "stopped", "ready_at": "t"},
    ]
    asked: list[str] = []

    def stats(service):
        asked.append(service)
        if service == "10.0.0.2":
            raise RuntimeError("quiet")
        return TOTAL, {"a": 20 * GIB, "b": 5 * GIB}

    with (
        patch("spark_pulse.tools.deployment_records.load", return_value=records),
        patch.object(tools.node_stats, "memory_by_deployment", side_effect=stats),
    ):
        recorded = footprints.sample_once(services=lambda address: address)

    assert sorted(asked) == ["", "10.0.0.2"]
    assert recorded == 2
    assert footprints.lookup("ha", TOTAL) == 20 * GIB
    assert footprints.lookup("hc", TOTAL) is None


def test_the_sampler_thread_starts_and_stops():
    sampler = footprints.start_sampler()
    assert footprints.start_sampler() is sampler
    footprints.stop_sampler()
    footprints.stop_sampler()


# ── The two inverses of the estimate ────────────────────────────────────────


SHAPE = vram.ModelShape(
    weight_bytes=8 * GIB,
    num_layers=36,
    num_kv_heads=8,
    head_dim=128,
    kv_dtype="bfloat16",
    layout=vram.KV_LAYOUT_DENSE,
)


def test_the_smallest_fraction_rounds_up():
    assert vram.min_fraction(14 * GIB, 128 * GIB) == 0.11
    assert vram.min_fraction(64 * GIB, 128 * GIB) == 0.5
    assert vram.min_fraction(None, 128 * GIB) is None


def test_the_context_that_fits_is_the_estimate_inverted():
    workload = vram.Workload(max_model_len=262144)
    fit = vram.context_that_fits(SHAPE, workload, 20 * GIB)

    assert fit is not None and fit % vram.CONTEXT_STEP == 0
    at_fit = vram.estimate(SHAPE, vram.Workload(max_model_len=fit)).total_bytes
    beyond = vram.estimate(
        SHAPE, vram.Workload(max_model_len=fit + vram.CONTEXT_STEP)
    ).total_bytes
    assert at_fit <= 20 * GIB < beyond


def test_the_context_that_fits_never_suggests_more_than_asked():
    assert (
        vram.context_that_fits(SHAPE, vram.Workload(max_model_len=4096), 100 * GIB)
        == 4096
    )


def test_no_context_fits_when_the_weights_do_not():
    assert (
        vram.context_that_fits(SHAPE, vram.Workload(max_model_len=8192), 9 * GIB)
        is None
    )
    assert vram.context_that_fits(vram.ModelShape(), vram.Workload(), 9 * GIB) is None


# ── What used to keep the control node from compacting at all ───────────────


def test_neither_unit_blocks_the_agents_sudo():
    for template in (_SYSTEM_SERVICE_TEMPLATE, _USER_SERVICE_TEMPLATE):
        assert "NoNewPrivileges" not in template
    # A user unit's sandbox is a user namespace, where /etc reads as uid 65534.
    assert "ProtectSystem" not in _USER_SERVICE_TEMPLATE
    assert "PrivateTmp" not in _USER_SERVICE_TEMPLATE


def test_a_sandboxed_agent_is_told_to_reinstall_the_service():
    result = SimpleNamespace(
        before=None,
        after=None,
        compacted=False,
        dropped_caches=False,
        detail='drop_caches: sudo: The "no new privileges" flag is set',
    )
    message, _meta = compaction.describe("spark-01", result)
    assert "spark-pulse install" in message
