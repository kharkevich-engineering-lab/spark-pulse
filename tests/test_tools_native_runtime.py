"""Tests for the native solo deployment runtime.

The container side is driven through ``MockDockerService`` over
``MockDockerClient`` — the same fake the container-service contract test uses —
so the production ``DockerService`` code path is what actually runs. Recipes,
the model catalogue and the deployment record file are patched per test; the
engine registry is the bundled one (offline, no indexes).
"""

from __future__ import annotations

import importlib
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from spark_pulse import tools
from spark_pulse.config import config
from spark_pulse.engines import EngineRegistry, Topology, reset_registry
from spark_pulse.mock.docker import MockDockerClient, MockDockerService
from spark_pulse.tools.docker import PullCancelled
from spark_pulse.tools.labels import (
    DEPLOYMENT_LABEL,
    GENERATION_LABEL,
    MANAGED_LABEL,
    RANK_LABEL,
    WORLD_SIZE_LABEL,
)
from spark_pulse.tools.ssh import SSHError, SSHErrorType

# pytest-env forces SIMULATION_MODE=1, so ``from spark_pulse.tools import
# native_runtime`` hands back the mock re-export. Patching module globals only
# reaches the implementation if we hold the real module, so resolve it here.
nr = importlib.import_module("spark_pulse.tools.native_runtime")

# ── Fixtures ────────────────────────────────────────────────────────────────

V1_RECIPE = {
    "id": "qwen3-8b",
    "name": "Qwen3 8B",
    "model": "Qwen/Qwen3-8B",
    "container": "vllm-node",
    "command": (
        "vllm serve Qwen/Qwen3-8B --port {port} "
        "--tensor-parallel-size {tensor_parallel} "
        "--distributed-executor-backend ray"
    ),
    "defaults": {"port": 8000, "tensor_parallel": 1},
    "mods": [],
    "env": {},
}

V1_TP2 = {
    **V1_RECIPE,
    "id": "qwen3-8b-tp2",
    "defaults": {"port": 8000, "tensor_parallel": 2},
}

#: The three-node fleet's recipe. One GPU per node means the world size *is*
#: the node count, so a three-node deployment has to occupy three ranks.
V1_TP3 = {
    **V1_RECIPE,
    "id": "qwen3-8b-tp3",
    "defaults": {"port": 8000, "tensor_parallel": 3},
}

V1_SOLO_ONLY = {**V1_RECIPE, "id": "solo-only", "solo_only": True}
V1_CLUSTER_ONLY = {
    **V1_TP2,
    "id": "cluster-only",
    "cluster_only": True,
}
V1_MIN_NODES = {**V1_TP3, "id": "min-three", "min_nodes": 3}

V1_UNKNOWN_TAG = {
    **V1_RECIPE,
    "id": "gpt-oss-mxfp4",
    "name": "GPT-OSS MXFP4",
    "container": "vllm-node-mxfp4",
}

V1_NO_PORT = {
    **V1_RECIPE,
    "id": "qwen3-8b-noport",
    "command": "vllm serve Qwen/Qwen3-8B --port {port}",
    "defaults": {},
}

V1_WITH_MODS = {**V1_RECIPE, "id": "qwen3-8b-mods", "mods": ["fix-qwen"]}

V2_RECIPE = {
    "id": "generic",
    "name": "Generic",
    "model": "Qwen/Qwen3-8B",
    "recipe_version": "2",
    "container": "",
    "command": "",
    "params": {"host": "0.0.0.0"},
    "defaults": {"port": 30000},
    "mods": [],
    "env": {},
}

RECIPES = {
    r["id"]: r
    for r in (
        V1_RECIPE,
        V1_TP2,
        V1_TP3,
        V1_UNKNOWN_TAG,
        V1_NO_PORT,
        V1_WITH_MODS,
        V2_RECIPE,
        V1_SOLO_ONLY,
        V1_CLUSTER_ONLY,
        V1_MIN_NODES,
    )
}

CATALOGUE = [{"id": "Qwen/Qwen3-8B", "source": "hf", "path": "/models/qwen3-8b"}]


@pytest.fixture
def registry(tmp_path):
    """Bundled engine specs only — no index fetching, no network."""
    reset_registry()
    with patch.object(type(config), "engine_indexes", property(lambda self: [])):
        instance = EngineRegistry(cache_dir=tmp_path / "engine-cache")
        with patch(
            "spark_pulse.tools.native_runtime.get_registry", return_value=instance
        ):
            yield instance
    reset_registry()


@pytest.fixture
def records(tmp_path):
    """Point the shared deployments.json at a temp file."""
    path = tmp_path / "deployments.json"
    with patch.object(tools.deployment_records, "RECORDS_FILE", path):
        yield path


@pytest.fixture
def recipes():
    with patch.object(
        tools.recipes, "get_recipe", side_effect=lambda rid, *a, **kw: RECIPES.get(rid)
    ) as mocked:
        yield mocked


@pytest.fixture
def catalogue():
    with patch.object(
        tools.models,
        "get_model",
        side_effect=lambda mid: next((m for m in CATALOGUE if m["id"] == mid), None),
    ) as mocked:
        yield mocked


@pytest.fixture
def docker():
    return MockDockerService(MockDockerClient())


@pytest.fixture
def native(registry, records, recipes, catalogue):
    """Everything a plan() call needs, wired to temp state."""
    return nr


# ── plan() ──────────────────────────────────────────────────────────────────


class TestPlan:
    def test_renders_the_same_script_the_engine_would(self, native, registry):
        """The plan's script is exactly what spark_pulse.engines renders."""
        plan = native.plan("qwen3-8b")

        engine = registry.engine("vllm", "default")
        expected = engine.render(
            V1_RECIPE,
            model=None,
            params={},
            extra_args=[],
            topology=Topology(nodes=[]),
            node_rank=0,
        )

        assert plan.ranks[0]["script"] == expected.script
        assert plan.launch_command == expected.command
        assert "--tensor-parallel-size 1" in plan.launch_command
        # The Ray backend never survives, and one node carries the rendezvous
        # flags exactly as two do.
        assert "--distributed-executor-backend" not in plan.launch_command
        assert plan.launch_command.endswith(
            "--nnodes 1 --node-rank 0 --master-addr 127.0.0.1 --master-port 29501"
        )
        assert plan.solo is True
        assert plan.node_count == 1


class TestTopologyConstraints:
    """The constraints the recipe parser has always produced, now enforced."""

    def test_solo_only_refuses_more_than_one_node(self, native):
        with pytest.raises(native.NativeRuntimeError) as exc:
            native.plan("solo-only", nodes=["a", "b"], solo=False)
        assert "solo_only" in str(exc.value)

    def test_solo_only_deploys_on_one_node(self, native):
        assert native.plan("solo-only").node_count == 1

    def test_cluster_only_refuses_one_node(self, native):
        with pytest.raises(native.NativeRuntimeError) as exc:
            native.plan("cluster-only")
        assert "cluster_only" in str(exc.value)

    def test_cluster_only_accepts_two_nodes(self, native):
        assert native.plan("cluster-only", nodes=PAIR, solo=False).node_count == 2

    def test_min_nodes_is_enforced(self, native):
        with pytest.raises(native.NativeRuntimeError) as exc:
            native.plan("min-three", nodes=["a", "b"], solo=False)
        assert "at least 3 nodes" in str(exc.value)

    def test_min_nodes_is_satisfied(self, native):
        plan = native.plan("min-three", nodes=NODES, solo=False)
        assert plan.node_count == 3


class TestCapacity:
    """One GPU per node, so tensor parallelism has to span nodes."""

    def test_two_way_parallelism_on_one_node_is_refused(self, native):
        with pytest.raises(native.NativeRuntimeError) as exc:
            native.plan("qwen3-8b-tp2")
        reason = str(exc.value)
        assert "one GPU per node" in reason
        assert "need 2, have 1" in reason

    def test_two_way_parallelism_across_two_nodes_is_planned(self, native):
        plan = native.plan("qwen3-8b-tp2", nodes=PAIR, solo=False)
        assert plan.node_count == 2
        assert "--tensor-parallel-size 2" in plan.launch_command


class TestVersionGuard:
    """A legacy tag can map to an image too old for the rendered flags."""

    def _with_version(self, registry, version):
        spec = registry.get("vllm", "default")
        spec.framework_version = version
        return spec

    def test_an_old_vllm_is_refused_with_a_reason(self, native, registry):
        self._with_version(registry, "0.10.2")
        with pytest.raises(native.NativeRuntimeError) as exc:
            native.plan("qwen3-8b")
        reason = str(exc.value)
        assert "0.10.2" in reason
        assert "0.11.1" in reason

    def test_the_first_supported_release_plans(self, native, registry):
        self._with_version(registry, "0.11.1")
        assert native.plan("qwen3-8b").launch_command

    def test_an_undeclared_version_warns_instead(self, native, registry):
        self._with_version(registry, "")
        plan = native.plan("qwen3-8b")
        assert any("0.11.1" in w for w in plan.warnings)

    def test_maps_a_v1_container_tag_to_the_engine_image(self, native):
        """``container: vllm-node`` resolves through the engine's legacy tags."""
        plan = native.plan("qwen3-8b")

        assert plan.engine == "vllm"
        assert plan.variant == "default"
        assert plan.image_ref.startswith(
            "ghcr.io/kharkevich-engineering-lab/spark-pulse-engine/vllm"
        )
        assert plan.warnings == []

    def test_unknown_container_tag_warns_and_falls_back(self, native):
        """An unclaimed tag is a warning, not a failure."""
        plan = native.plan("gpt-oss-mxfp4")

        assert plan.image_ref
        assert any("vllm-node-mxfp4" in w for w in plan.warnings)

    def test_explicit_engine_override_wins_over_the_tag(self, native, registry):
        plan = native.plan("qwen3-8b", engine="vllm", variant="default")
        assert plan.engine == "vllm"
        assert plan.image_ref == registry.get("vllm", "default").image_ref

    def test_sglang_refuses_a_v1_recipe(self, native):
        """A vLLM command template cannot run on SGLang; plan says why."""
        with pytest.raises(native.NativeRuntimeError) as exc:
            native.plan("qwen3-8b", engine="sglang")

        assert "sglang" in str(exc.value)
        assert "command" in str(exc.value)

    def test_sglang_accepts_a_v2_recipe(self, native):
        plan = native.plan("generic", engine="sglang")

        assert plan.engine == "sglang"
        assert "sglang.launch_server" in plan.launch_command
        assert plan.readiness_path == "/health"
        assert plan.rendezvous_port == 50000

    def test_missing_model_is_refused(self, native):
        """A model that is not in the catalogue stops the plan by default."""
        with pytest.raises(native.NativeRuntimeError) as exc:
            native.plan("qwen3-8b", model="nobody/not-downloaded")

        assert "not in the local catalogue" in str(exc.value)

    def test_missing_model_allowed_explicitly(self, native):
        plan = native.plan(
            "qwen3-8b", model="nobody/not-downloaded", allow_missing_model=True
        )
        assert plan.model == "nobody/not-downloaded"

    def test_unknown_recipe_is_refused(self, native):
        with pytest.raises(native.NativeRuntimeError):
            native.plan("nope")

    def test_port_comes_from_the_recipe_when_it_has_one(self, native):
        assert native.plan("qwen3-8b").port == 8000

    def test_port_override_wins(self, native):
        plan = native.plan("qwen3-8b", params={"port": 9123})
        assert plan.port == 9123
        assert "--port 9123" in plan.launch_command

    def test_port_is_allocated_from_the_configured_range(self, native):
        """With no port anywhere, one is taken from default_port_range_*."""
        plan = native.plan("qwen3-8b-noport")

        assert config.default_port_range_start <= plan.port
        assert plan.port <= config.default_port_range_end
        assert f"--port {plan.port}" in plan.launch_command

    def test_allocation_skips_ports_already_deployed(self, native, records):
        first = native.plan("qwen3-8b-noport")
        records.write_text(
            json.dumps(
                [
                    {
                        "id": "other",
                        "status": "running",
                        "port": first.port,
                        "runtime": "native",
                    }
                ]
            )
        )
        second = native.plan("qwen3-8b-noport")
        assert second.port != first.port

    def test_mods_need_engine_support(self, native):
        recipe = {**V1_WITH_MODS, "command": "", "container": ""}
        with patch.object(
            tools.recipes, "get_recipe", side_effect=lambda rid, *a, **kw: recipe
        ):
            with pytest.raises(native.NativeRuntimeError) as exc:
                native.plan("qwen3-8b-mods", engine="sglang")
        assert "does not support" in str(exc.value)

    def test_container_spec_follows_the_engine_profile(self, native):
        spec = native.plan("qwen3-8b").container

        assert spec.name.startswith("spark-pulse-")
        assert spec.command == "sleep infinity"
        assert spec.privileged is True
        assert spec.ipc_host is True
        assert spec.network_host is True
        # Host networking publishes nothing: the engine binds the port itself.
        assert spec.port_mappings == []
        assert spec.ulimits == {"nofile": "1048576:1048576"}
        assert spec.labels[MANAGED_LABEL] == "true"

    def test_hf_cache_is_mounted_at_the_container_home(self, native):
        mounts = native.plan("qwen3-8b").container.mounts
        assert "/root/.cache/huggingface" in mounts.values()
        # Engine cache dirs land under /root, as upstream mounts them.
        assert all(v.startswith("/root") or v.startswith("/") for v in mounts.values())

    def test_hf_cache_is_mounted_exactly_once(self, native):
        # The engine declares ~/.cache/huggingface itself and HF_HOME targets
        # the same container path; docker refuses duplicate destinations.
        mounts = native.plan("qwen3-8b").container.mounts
        targets = list(mounts.values())
        assert targets.count("/root/.cache/huggingface") == 1

    def test_env_carries_engine_and_recipe_variables(self, native):
        env = native.plan("qwen3-8b").container.env
        assert env["PYTORCH_CUDA_ALLOC_CONF"] == "expandable_segments:True"
        assert env["HF_HOME"] == "/root/.cache/huggingface"

    def test_extra_args_are_appended_quoted(self, native):
        plan = native.plan("qwen3-8b", extra_args=["--enable-prefix-caching"])
        assert plan.launch_command.endswith("--enable-prefix-caching")

    def test_plan_is_json_serialisable(self, native):
        json.dumps(native.plan("qwen3-8b").to_dict())


# ── start() ─────────────────────────────────────────────────────────────────


class TestStart:
    def test_idle_container_then_mods_then_exec_then_ready(self, native, docker):
        plan = native.plan("qwen3-8b")

        record = native.start(plan, docker=docker, wait=True)

        assert record["status"] == "running"
        assert record["runtime"] == "native"
        assert record["container_name"] == plan.container.name
        assert record["engine"] == "vllm"
        assert record["image_ref"] == plan.image_ref
        assert record["pid"] is None
        assert record["port"] == plan.port

        container = docker.client.containers.get(plan.container.name)
        # The idle container is started first, then the script is exec'd into it.
        execs = container.executed_commands
        assert any(native.SCRIPT_PATH in cmd for cmd in execs)
        assert any("/proc/1/fd/1" in cmd for cmd in execs)

    def test_the_record_is_persisted(self, native, docker, records):
        plan = native.plan("qwen3-8b")
        native.start(plan, docker=docker, wait=True)

        # Read back through the store rather than off the file: state lives
        # in the database now, and what this asserts is that the record
        # persisted, not where it landed.
        saved = tools.deployment_records.load()
        assert [d["id"] for d in saved] == [plan.deployment_id]
        assert saved[0]["runtime"] == "native"

    def test_container_carries_the_deployment_label(self, native, docker):
        plan = native.plan("qwen3-8b")
        native.start(plan, docker=docker, wait=True)

        container = docker.client.containers.get(plan.container.name)
        assert container.labels[DEPLOYMENT_LABEL] == plan.deployment_id

    def test_container_dying_early_fails_with_logs(self, native, docker):
        """A container that exits before readiness fails, quoting its logs."""
        plan = native.plan("qwen3-8b")

        def _die(name):
            container = docker.client.containers.get(name)
            container.log_lines.append("CUDA error: out of memory")
            container.status = "exited"
            container.attrs["State"]["Running"] = False
            return {
                "status": "exited",
                "running": False,
                "id": container.id,
                "state": {},
            }

        with patch.object(docker, "get_container_status", side_effect=_die):
            record = native.start(plan, docker=docker, wait=True, ready_timeout=5)

        assert record["status"] == "error"
        assert "exited before the engine became ready" in record["error_message"]
        assert "CUDA error: out of memory" in record["error_message"]

    def test_readiness_timeout_fails(self, native, docker):
        plan = native.plan("qwen3-8b")
        with patch.object(native, "probe_ready", return_value=False):
            record = native.start(plan, docker=docker, wait=True, ready_timeout=1)

        assert record["status"] == "error"
        assert "did not become ready" in record["error_message"]

    def test_a_cluster_plan_is_no_longer_refused_by_the_runtime(self, native, docker):
        """The runtime starts every rank; the *dispatcher* still refuses one.

        Flipping the dispatcher is the step that removes the upstream cluster
        path, and it comes last. Until then the runtime is capable and the
        refusal lives in exactly one place — ``tools.deploy_dispatch``, which
        ``test_router_deployments`` covers.
        """
        plan = native.plan("qwen3-8b-tp2", nodes=PAIR, solo=False)
        assert [r.rank for r in plan.rank_plans] == [0, 1]
        assert len(plan.ranks) == 2

    def test_start_without_waiting_returns_immediately(self, native, docker):
        plan = native.plan("qwen3-8b")
        record = native.start(plan, docker=docker, wait=False)
        assert record["status"] == "running"


# ── Image pull ──────────────────────────────────────────────────────────────


def _forget_image(docker, ref: str) -> None:
    """Drop an image from the simulated host so a deploy has to pull it.

    The mock client seeds the published engine images, which is what the
    Images page wants but not what a pull test wants.
    """
    try:
        docker.client.images.remove(ref)
    except Exception:
        pass


class TestImagePull:
    """The pull is explicit and visible — the worst of the first hardware run."""

    def test_plan_reports_a_present_image(self, native, docker):
        """A pulled image is reported present, with its size, and no warning."""
        plan = native.plan("qwen3-8b")
        docker.client.images.add(plan.image_ref, size=26_843_545_600)

        with patch.object(nr, "_docker_service", return_value=docker):
            replanned = native.plan("qwen3-8b")

        assert replanned.image_present is True
        assert replanned.image_size_bytes == 26_843_545_600
        assert not any("will be pulled" in w for w in replanned.warnings)

    def test_plan_warns_when_the_image_is_absent(self, native, docker):
        """A missing image is a warning, never a planning failure."""
        plan = native.plan("qwen3-8b")
        _forget_image(docker, plan.image_ref)
        with patch.object(nr, "_docker_service", return_value=docker):
            plan = native.plan("qwen3-8b")

        assert plan.image_present is False
        assert plan.image_size_bytes is None
        assert any("will be pulled" in w for w in plan.warnings)

    def test_start_pulls_the_image_and_emits_events(self, native, docker):
        """A missing image is pulled before the container, with progress."""
        events: list[tuple[str, dict]] = []

        def _capture(event_type, deployment_id, message="", metadata=None):
            events.append((event_type.value, metadata or {}))

        plan = native.plan("qwen3-8b")
        _forget_image(docker, plan.container.image)
        with patch.object(nr, "publish_event", side_effect=_capture):
            record = native.start(plan, docker=docker, wait=True)

        assert record["status"] == "running"
        names = [name for name, _ in events]
        assert "image.pull.started" in names
        assert "image.pull.progress" in names
        assert "image.pull.completed" in names
        # The pull happens before the container exists.
        assert names.index("image.pull.completed") < names.index(
            "deployment_container_started"
        )
        percents = [
            meta["percent"] for name, meta in events if name == "image.pull.progress"
        ]
        assert percents and percents[-1] == 100.0
        assert docker.image_exists(plan.container.image)

    def test_start_skips_the_pull_when_the_image_is_present(self, native, docker):
        """No pull events when the host already has the image."""
        plan = native.plan("qwen3-8b")
        docker.client.images.add(plan.container.image)
        events: list[str] = []

        with patch.object(
            nr,
            "publish_event",
            side_effect=lambda t, *a, **kw: events.append(t.value),
        ):
            native.start(plan, docker=docker, wait=True)

        assert not [e for e in events if e.startswith("image.pull")]

    def test_a_failed_pull_fails_the_deployment(self, native, docker):
        """A pull failure surfaces as an errored deployment, not a stuck one."""
        plan = native.plan("qwen3-8b")
        _forget_image(docker, plan.container.image)
        with patch.object(
            docker, "pull_image", side_effect=RuntimeError("registry unreachable")
        ):
            record = native.start(plan, docker=docker, wait=True)

        assert record["status"] == "error"
        assert "registry unreachable" in record["error_message"]

    def test_the_record_shows_pulling_while_the_pull_runs(self, native, docker):
        """GET /api/deployments/{id} tells the truth during a long pull."""
        plan = native.plan("qwen3-8b")
        _forget_image(docker, plan.container.image)
        seen: list[str] = []

        real_pull = docker.pull_image

        def _watching_pull(ref, progress=None, **kwargs):
            record = nr.get_deployment(plan.deployment_id)
            seen.append(str((record or {}).get("status")))
            return real_pull(ref, progress, **kwargs)

        with patch.object(docker, "pull_image", side_effect=_watching_pull):
            native.start(plan, docker=docker, wait=True)

        assert seen == ["pulling"]


# ── Lifecycle ───────────────────────────────────────────────────────────────


class TestPerEngineBlocks:
    """A v2 recipe's non-default engine must not inherit the default's setup."""

    V2 = {
        "id": "two-engine",
        "recipe_version": "2",
        "name": "Two engines",
        "model": "Qwen/Qwen3-8B",
        "engine": "vllm",
        "params": {"port": 8000, "host": "0.0.0.0"},
        "engine_specs": {
            "vllm": {"env": {"VLLM_ONLY": "1"}, "mods": ["mods/vllm-only"]},
            "sglang": {"env": {"SGLANG_ONLY": "1"}, "mods": []},
        },
        "defaults": {"port": 8000},
        "env": {"VLLM_ONLY": "1"},
        "mods": ["mods/vllm-only"],
    }

    @pytest.fixture
    def two_engine(self, native):
        with patch.object(
            tools.recipes,
            "get_recipe",
            side_effect=lambda rid, *a, **kw: (
                self.V2 if rid == "two-engine" else RECIPES.get(rid)
            ),
        ):
            yield native

    def test_the_non_default_engine_gets_its_own_env(self, two_engine):
        plan = two_engine.plan("two-engine", engine="sglang")
        env = plan.container.env
        assert env.get("SGLANG_ONLY") == "1"
        assert "VLLM_ONLY" not in env

    def test_the_non_default_engine_gets_its_own_mods(self, two_engine):
        # The default engine's mods would otherwise be applied to an engine
        # that cannot even run them.
        assert two_engine.plan("two-engine", engine="sglang").mods == []
        assert two_engine.plan("two-engine", engine="vllm").mods == ["mods/vllm-only"]


class TestMods:
    """Mods are the part upstream's bash did for us; get the contract right."""

    @pytest.fixture
    def checkout(self, tmp_path):
        """A spark-vllm-docker-shaped checkout with one real mod."""
        mod = tmp_path / "mods" / "fix-qwen"
        mod.mkdir(parents=True)
        (mod / "run.sh").write_text("#!/bin/bash\ncp t.jinja $WORKSPACE_DIR/x.jinja\n")
        (mod / "t.jinja").write_text("{}")
        with patch.object(
            type(nr.config), "spark_vllm_path", property(lambda self: str(tmp_path))
        ):
            yield tmp_path

    def test_a_repo_relative_mod_path_resolves(self, checkout):
        # Recipes name mods the way upstream does, from the checkout root.
        assert nr._resolve_mod_dir("mods/fix-qwen").name == "fix-qwen"

    def test_a_bare_mod_name_resolves_too(self, checkout):
        assert nr._resolve_mod_dir("fix-qwen").name == "fix-qwen"

    def test_a_missing_mod_fails_the_deploy(self, checkout):
        # Skipping it silently costs 15 minutes and an unexplained engine error.
        with pytest.raises(nr.NativeRuntimeError, match="no run.sh"):
            nr._resolve_mod_dir("mods/not-here")

    def test_mods_run_with_workspace_dir_set_to_the_engine_workdir(
        self, native, docker, checkout
    ):
        plan = native.plan("qwen3-8b-mods")
        plan.mods = ["mods/fix-qwen"]

        native.start(plan, docker=docker, wait=True)

        container = docker.client.containers.get(plan.container.name)
        ran = [c for c in container.executed_commands if "run.sh" in c]
        assert ran, "the mod's run.sh was never executed"
        # Recipes reference mod-dropped files by bare name, so this must be the
        # image workdir, not /workspace.
        assert f"WORKSPACE_DIR={plan.workdir}" in ran[0]
        assert plan.workdir == "/workspace/vllm"


class TestLifecycle:
    def _running(self, native, docker):
        plan = native.plan("qwen3-8b")
        native.start(plan, docker=docker, wait=True)
        return plan

    def test_stop_removes_the_container_and_marks_the_record(self, native, docker):
        plan = self._running(native, docker)

        record = native.stop_deployment(plan.deployment_id, docker=docker)

        assert record["status"] == "stopped"
        assert record["stopped_at"]
        assert plan.container.name not in [
            c.name for c in docker.list_managed_containers()
        ]

    def test_delete_drops_the_record(self, native, docker, records):
        plan = self._running(native, docker)

        assert native.delete_deployment(plan.deployment_id, docker=docker) is True
        assert tools.deployment_records.load() == []

    def test_delete_of_an_unknown_deployment_is_false(self, native, docker):
        assert native.delete_deployment("nope", docker=docker) is False

    def test_logs_come_from_the_container(self, native, docker):
        plan = self._running(native, docker)
        logs = native.get_logs(plan.deployment_id, 100, docker=docker)
        assert plan.container.name in logs

    def test_logs_for_an_unknown_deployment(self, native, docker):
        assert native.get_logs("nope", docker=docker) == "Deployment not found"

    def test_status_reports_readiness(self, native, docker):
        plan = self._running(native, docker)
        state = native.status(plan.deployment_id, docker=docker)

        assert state["ready"] is True
        assert state["status"] == "running"
        assert state["container"]["running"] is True

    def test_status_of_a_gone_container_is_stopped(self, native, docker):
        plan = self._running(native, docker)
        docker.stop_container(plan.container.name)

        state = native.status(plan.deployment_id, docker=docker)
        assert state["status"] == "stopped"

    def test_list_marks_records_whose_container_vanished(self, native, docker):
        plan = self._running(native, docker)
        docker.stop_container(plan.container.name)

        listed = native.list_deployments(docker=docker)
        assert [d["status"] for d in listed] == ["stopped"]

    def test_list_adopts_an_unknown_labelled_container(self, native, docker, records):
        """Reconciliation: a managed container with no record is adopted."""
        plan = self._running(native, docker)
        tools.deployment_records.save([])  # the container outlives its record

        listed = native.list_deployments(docker=docker)

        assert [d["id"] for d in listed] == [plan.deployment_id]
        assert listed[0]["reconciled"] is True
        assert listed[0]["container_name"] == plan.container.name


# ── Port allocation ─────────────────────────────────────────────────────────


class TestDeleteTearsDown:
    def test_delete_stops_the_container_even_after_an_error(
        self, native, docker, records
    ):
        plan = native.plan("qwen3-8b")
        native.start(plan, docker=docker, wait=True)
        native._update_record(plan.deployment_id, status="error")

        native.delete_deployment(plan.deployment_id, docker=docker)

        # An errored deployment still owns a running container; dropping the
        # record without stopping it leaks the GPU.
        names = [c.name for c in docker.client.containers.list(all=True)]
        assert plan.container.name not in names


class TestDeployDoesNotBlockOnAPull:
    """A 26 GB download must not hold one of the forty request threads.

    ``create_deployment`` is what ``POST /api/deployments`` calls on the
    request thread, so anything slow it does inline is a thread taken out of
    circulation for the duration.
    """

    @staticmethod
    def _wait_until(predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    @staticmethod
    def _endless_pull_stream(*_args, **_kwargs):
        """A pull that keeps producing chunks and never finishes."""

        def _stream():
            while True:
                time.sleep(0.01)
                yield {
                    "status": "Downloading",
                    "id": "layer-a",
                    "progressDetail": {"current": 1, "total": 1_000_000_000},
                }

        return _stream()

    @classmethod
    def _create_off_thread(cls, native, recipe_id: str) -> tuple[dict, float]:
        """Call create_deployment where a blocking one fails instead of hanging.

        The defect under test is precisely that the call does not return, so
        driving it from the test's own thread would wedge the suite rather
        than report anything.
        """
        out: dict = {}
        began = time.monotonic()

        def _call() -> None:
            out["record"] = native.create_deployment(recipe_id)

        caller = threading.Thread(target=_call, name="create-deployment", daemon=True)
        caller.start()
        caller.join(timeout=10)
        elapsed = time.monotonic() - began
        assert not caller.is_alive(), (
            "create_deployment never returned — it is still blocked on the "
            "image pull, holding a request thread"
        )
        return out["record"], elapsed

    def test_the_post_returns_while_the_image_is_still_pulling(
        self, native, docker, records
    ):
        """The call comes back in milliseconds, with the record in "pulling"."""
        image_ref = native.plan("qwen3-8b").container.image
        _forget_image(docker, image_ref)

        with patch.object(nr, "_docker_service", return_value=docker):
            with patch.object(
                docker.client.api, "pull", side_effect=self._endless_pull_stream
            ):
                record, elapsed = self._create_off_thread(native, "qwen3-8b")
                dep_id = record["id"]
                try:
                    assert elapsed < 1.0, (
                        f"create_deployment held the caller for {elapsed:.1f}s "
                        "while the image downloaded"
                    )
                    assert record["status"] == "pulling"
                    # And that is what a GET of the deployment reports too.
                    assert nr.get_deployment(dep_id)["status"] == "pulling"
                    assert self._wait_until(lambda: nr.pull_is_active(dep_id))
                finally:
                    nr.cancel_pull(dep_id)
                    self._wait_until(lambda: not nr.pull_is_active(dep_id))

    def test_a_present_image_still_starts_inline(self, native, docker, records):
        """Nothing is deferred when there is nothing slow to defer."""
        image_ref = native.plan("qwen3-8b").container.image
        docker.client.images.add(image_ref)

        with patch.object(nr, "_docker_service", return_value=docker):
            record = native.create_deployment("qwen3-8b")

        assert record["status"] == "running"
        names = [c.name for c in docker.client.containers.list(all=True)]
        assert record["container_name"] in names

    def test_a_teardown_during_the_pull_stops_it(self, native, docker, records):
        """Deleting mid-pull ends the download instead of orphaning it."""
        image_ref = native.plan("qwen3-8b").container.image
        _forget_image(docker, image_ref)

        with patch.object(nr, "_docker_service", return_value=docker):
            with patch.object(
                docker.client.api, "pull", side_effect=self._endless_pull_stream
            ):
                record, _ = self._create_off_thread(native, "qwen3-8b")
                dep_id = record["id"]
                assert self._wait_until(lambda: nr.pull_is_active(dep_id))

                assert native.delete_deployment(dep_id, docker=docker) is True

                assert self._wait_until(
                    lambda: not nr.pull_is_active(dep_id)
                ), "the pull kept running for a deployment that no longer exists"

        assert nr.get_deployment(dep_id) is None
        # The pull never completed, so nothing was written to the image store.
        assert docker.image_exists(image_ref) is False

    def test_a_cancelled_pull_leaves_the_record_stopped_not_errored(
        self, native, docker, records
    ):
        """A deliberate teardown is not a crash, and must not read as one."""
        plan_obj = native.plan("qwen3-8b")
        _forget_image(docker, plan_obj.container.image)
        dep_id = plan_obj.deployment_id

        def _cancel_once_running(ref, progress=None, **kwargs):
            nr.cancel_pull(dep_id)
            raise PullCancelled(f"pull of {ref} cancelled")

        with patch.object(docker, "pull_image", side_effect=_cancel_once_running):
            record = native.start(plan_obj, docker=docker, wait=True)

        assert record["status"] == "stopped"
        assert record.get("error_message") is None

    def test_cancel_pull_reports_whether_there_was_one(self, native, docker):
        """Nothing in flight is not an error, just a False."""
        assert nr.cancel_pull("no-such-deployment") is False
        assert nr.pull_is_active("no-such-deployment") is False


class TestAllocatePort:
    def test_skips_taken_ports(self):
        first = nr.allocate_port()
        assert nr.allocate_port({first}) != first

    def test_raises_when_the_range_is_exhausted(self):
        with patch.object(nr, "_port_free", return_value=False):
            with pytest.raises(nr.NativeRuntimeError) as exc:
                nr.allocate_port()
        assert "no free port" in str(exc.value)


# ── Container path rewriting ────────────────────────────────────────────────


class TestContainerPaths:
    def test_home_prefix_becomes_root(self):
        home = str(Path.home())
        assert nr._container_path(f"{home}/.cache/vllm") == "/root/.cache/vllm"

    def test_absolute_paths_outside_home_are_kept(self):
        assert nr._container_path("/data/models") == "/data/models"


# ── Ranks ───────────────────────────────────────────────────────────────────


class JournalDocker(MockDockerService):
    """A per-node container service that writes down what it was asked to do.

    Each node gets its own client, so a container created on one node is not
    visible on another — which is what makes the ordering and teardown
    assertions below mean anything.
    """

    def __init__(self, journal: list, node: str = "", fail_on: str = ""):
        super().__init__(MockDockerClient())
        self.journal = journal
        self.node = node
        self.fail_on = fail_on
        self.unreachable = False

    def _note(self, verb: str, name: str) -> None:
        self.journal.append((verb, self.node, name))

    def _guard(self) -> None:
        if self.unreachable:
            raise SSHError(
                error_type=SSHErrorType.NETWORK,
                host=self.node,
                message="no route to host",
            )

    def run_container(self, **kwargs):
        self._note("run_container", kwargs["name"])
        self._guard()
        if self.fail_on and self.fail_on in kwargs["name"]:
            raise RuntimeError("the daemon said no")
        return super().run_container(**kwargs)

    def stop_container(self, name: str, timeout: int = 30) -> bool:
        self._note("stop_container", name)
        self._guard()
        return super().stop_container(name, timeout=timeout)

    def get_container_status(self, name: str):
        self._guard()
        return super().get_container_status(name)

    def list_managed_containers(self, labels=None):
        self._guard()
        return super().list_managed_containers(labels)

    def exec_in_container(self, container, command, detach=False, timeout=None):
        self._note("exec_in_container", str(container))
        return super().exec_in_container(
            container, command, detach=detach, timeout=timeout
        )

    def copy_to_container(self, container, local_path, remote_path, timeout=120):
        # The first thing a *launch* does to a rank, which is what makes the
        # launch order observable as distinct from the creation order.
        self._note("copy_to_container", str(container))
        return super().copy_to_container(container, local_path, remote_path, timeout)


class Fleet:
    """One :class:`JournalDocker` per node address, resolved like the real thing."""

    def __init__(self, addresses: list[str], fail_on: str = ""):
        self.journal: list = []
        self.nodes = {
            address: JournalDocker(self.journal, address, fail_on=fail_on)
            for address in addresses
        }

    def services(self, address: str):
        return self.nodes[address]

    def verbs(self, verb: str) -> list[str]:
        return [name for kind, _node, name in self.journal if kind == verb]


NODES = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]


@pytest.fixture(autouse=True)
def registered_nodes():
    """Enroll every address these tests deploy to.

    A plan above one node resolves its nodes through the registry, because
    that is where the per-machine fabric interface names live and NCCL
    pinning is find-or-fail. So a test that deploys to an address has to
    enroll it first, exactly as an operator would. It is autouse rather than
    opt-in on purpose: the size-one tests run with a populated registry too,
    which is how they show that a solo deployment does not read it.
    """
    for index, address in enumerate(NODES):
        tools.node_registry.add_node(
            name=f"fleet-{index}",
            address=address,
            ethernet_interface="eth0",
            infiniband_interfaces=("ib0", "ib1"),
        )
    yield


#: A two-node pair out of the same registry, for the tests that want two.
PAIR = NODES[:2]


@pytest.fixture
def fleet():
    return Fleet(NODES)


class TestSizeOneIsUnchanged:
    """The safety property: at one node the loop has length one.

    Every other test in this file is a size-one deployment driven through the
    unchanged public API, so the whole file is the regression suite. These add
    the two things that are specific to ranks existing at all.
    """

    def test_a_size_one_deploy_makes_exactly_the_calls_it_always_made(
        self, native, records
    ):
        journal: list = []
        docker = JournalDocker(journal)
        plan = native.plan("qwen3-8b")

        native.start(plan, docker=docker, wait=True)

        name = plan.container.name
        # Create the idle container, copy the script in, chmod it, exec it.
        # That is the pre-rank sequence, unchanged.
        assert journal == [
            ("run_container", "", name),
            ("copy_to_container", "", name),
            ("exec_in_container", "", name),
            ("exec_in_container", "", name),
        ]

    def test_one_node_is_one_rank_on_this_machine(self, native, docker):
        plan = native.plan("qwen3-8b")

        record = native.start(plan, docker=docker, wait=True)

        assert len(plan.rank_plans) == 1
        assert plan.rank_plans[0].node == ""
        assert plan.rank_plans[0].is_head is True
        assert record["node_count"] == 1
        assert [r["rank"] for r in record["ranks"]] == [0]
        assert len(docker.client.containers.list(all=True)) == 1

    def test_the_scalar_container_name_is_rank_zeros(self, native, docker):
        plan = native.plan("qwen3-8b")
        record = native.start(plan, docker=docker, wait=True)

        # The alias every existing reader uses — the health router, the UI,
        # the upstream path — is rank zero's, by identity not by copy.
        assert plan.container is plan.rank_plans[0].container
        assert record["container_name"] == plan.rank_plans[0].container.name
        assert native.status(plan.deployment_id, docker=docker)["container_name"] == (
            plan.container.name
        )


class TestRankNaming:
    def test_a_rank_container_carries_deployment_rank_and_generation(self, native):
        plan = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )

        assert [r.container.name for r in plan.rank_plans] == [
            "spark-pulse-dep1-r0-g1",
            "spark-pulse-dep1-r1-g1",
            "spark-pulse-dep1-r2-g1",
        ]

    def test_identity_labels_are_applied_last(self, native):
        plan = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )

        labels = plan.rank_plans[2].container.labels
        assert labels[DEPLOYMENT_LABEL] == "dep1"
        assert labels[GENERATION_LABEL] == "1"
        assert labels[RANK_LABEL] == "2"
        assert labels[WORLD_SIZE_LABEL] == "3"
        # Identity is merged after the metadata block, so nothing can shadow it.
        keys = list(labels)
        assert keys.index(DEPLOYMENT_LABEL) < keys.index(GENERATION_LABEL)
        assert labels[MANAGED_LABEL] == "true"

    def test_the_started_container_really_carries_the_identity(self, native, fleet):
        """The labels have to survive the container service, not just the plan."""
        plan = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )
        native.start(plan, services=fleet.services, wait=True)

        container = fleet.nodes["10.0.0.3"].client.containers.get(
            "spark-pulse-dep1-r2-g1"
        )
        assert container.labels[DEPLOYMENT_LABEL] == "dep1"
        assert container.labels[GENERATION_LABEL] == "1"
        assert container.labels[RANK_LABEL] == "2"
        assert container.labels[WORLD_SIZE_LABEL] == "3"

    def test_only_rank_zero_publishes_the_api_port(self, native):
        with patch.object(
            type(config),
            "docker_overrides",
            property(lambda self: {"network_host": False}),
        ):
            plan = native.plan("qwen3-8b-tp3", nodes=NODES, solo=False)
        assert plan.rank_plans[0].container.port_mappings
        assert plan.rank_plans[1].container.port_mappings == []
        assert plan.rank_plans[2].container.port_mappings == []

    def test_each_rank_renders_its_own_script(self, native):
        plan = native.plan("qwen3-8b-tp3", nodes=NODES, solo=False)

        assert plan.rank_plans[0].script != plan.rank_plans[1].script
        assert "--node-rank 1" in plan.rank_plans[1].command
        assert plan.rank_plans[1].node == "10.0.0.2"

    def test_a_legacy_record_still_resolves_its_container(self, native):
        """Records written before ranks existed carry only the scalar name."""
        legacy = {"id": "old", "container_name": "spark-pulse-old"}
        assert native.rank_entries(legacy) == [
            {
                "rank": 0,
                "node": "",
                "host": "",
                "container_name": "spark-pulse-old",
                "is_head": True,
            }
        ]
        assert native.rank_entries({"id": "old"})[0]["container_name"] == (
            "spark-pulse-old"
        )


class TestStartOrder:
    def test_every_container_exists_before_any_rank_is_launched(self, native, fleet):
        """Upstream's two phases: create them all, then run the command.

        ``launch-cluster.sh`` starts the head container (line 1097) and each
        worker container (1106), applies the mods to all of them (1111-1121),
        and only then execs the serve command (1201-1242). Interleaving the
        two would have rank one already at the rendezvous while rank zero's
        image turns out to be missing.
        """
        plan = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )

        record = native.start(plan, services=fleet.services, wait=True)

        assert record["status"] == "running"
        kinds = [kind for kind, _node, _name in fleet.journal]
        assert kinds.index("copy_to_container") > max(
            index for index, kind in enumerate(kinds) if kind == "run_container"
        )

    def test_workers_are_launched_first_and_rank_zero_last(self, native, fleet):
        plan = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )

        record = native.start(plan, services=fleet.services, wait=True)

        assert record["status"] == "running"
        # Containers are created head-first, exactly as upstream runs them.
        assert fleet.verbs("run_container") == [
            "spark-pulse-dep1-r0-g1",
            "spark-pulse-dep1-r1-g1",
            "spark-pulse-dep1-r2-g1",
        ]
        # The serve command is the other way round: the workers block on the
        # rendezvous, so rank zero must be the last thing to join it.
        assert fleet.verbs("copy_to_container") == [
            "spark-pulse-dep1-r2-g1",
            "spark-pulse-dep1-r1-g1",
            "spark-pulse-dep1-r0-g1",
        ]

    def test_each_rank_lands_on_its_own_node(self, native, fleet):
        plan = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )
        native.start(plan, services=fleet.services, wait=True)

        for rank, address in enumerate(NODES):
            names = [c.name for c in fleet.nodes[address].client.containers.list(True)]
            assert names == [f"spark-pulse-dep1-r{rank}-g1"]

    def test_rank_zero_is_stopped_first(self, native, fleet):
        plan = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )
        native.start(plan, services=fleet.services, wait=True)
        fleet.journal.clear()

        native.stop_deployment("dep1", services=fleet.services)

        # Head first, so the rendezvous collapses instead of leaving the
        # workers in a ten-minute collective timeout.
        assert fleet.verbs("stop_container") == [
            "spark-pulse-dep1-r0-g1",
            "spark-pulse-dep1-r1-g1",
            "spark-pulse-dep1-r2-g1",
        ]


class TestGangFailure:
    def test_one_rank_failing_tears_the_rest_down(self, native, records):
        fleet = Fleet(NODES, fail_on="-r1-")
        plan = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )

        record = native.start(plan, services=fleet.services, wait=True)

        assert record["status"] == "error"
        assert "rank 1 of 3" in record["error_message"]
        assert "torn down" in record["error_message"]
        # Rank 0 was already up when rank 1 failed; it must not survive.
        for address in NODES:
            assert fleet.nodes[address].client.containers.list(all=True) == []
        assert "spark-pulse-dep1-r0-g1" in fleet.verbs("stop_container")
        # And nothing was ever launched: the failure happened while the
        # containers were still idle, which is the whole point of creating
        # them all before running the serve command in any of them.
        assert fleet.verbs("copy_to_container") == []

    def test_the_failed_rank_leaves_no_partial_state(self, native, records):
        fleet = Fleet(NODES, fail_on="-r1-")
        plan = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )

        native.start(plan, services=fleet.services, wait=True)

        # No per-rank restart: the model is sharded across exactly these
        # ranks, so there is nothing to keep half of.
        assert fleet.verbs("run_container").count("spark-pulse-dep1-r1-g1") == 1


class TestGenerations:
    def _running(self, native, fleet, dep_id="dep1"):
        plan = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id=dep_id
        )
        native.start(plan, services=fleet.services, wait=True)
        return plan

    def test_a_second_attempt_gets_the_next_generation(self, native, fleet):
        self._running(native, fleet)

        again = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )

        assert again.generation == 2
        assert again.container.name == "spark-pulse-dep1-r0-g2"

    def test_a_name_from_an_old_generation_is_reaped(self, native, fleet):
        self._running(native, fleet)
        again = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )
        fleet.journal.clear()

        native.start(again, services=fleet.services, wait=True)

        assert "spark-pulse-dep1-r0-g1" in fleet.verbs("stop_container")
        for address in NODES:
            live = [c.name for c in fleet.nodes[address].client.containers.list(True)]
            assert all("-g1" not in name for name in live)

    def test_the_old_generation_is_gone_before_the_new_one_is_created(
        self, native, fleet
    ):
        self._running(native, fleet)
        again = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )
        fleet.journal.clear()

        native.start(again, services=fleet.services, wait=True)

        stops = [
            i for i, (v, _n, name) in enumerate(fleet.journal) if v == "stop_container"
        ]
        creates = [
            i
            for i, (v, _n, name) in enumerate(fleet.journal)
            if v == "run_container" and "-g2" in name
        ]
        assert stops and creates
        assert max(stops) < min(creates)

    def test_another_deployments_containers_are_never_touched(self, native, fleet):
        """Reaping is scoped by the deployment label, not by the name prefix."""
        self._running(native, fleet, dep_id="dep1")
        self._running(native, fleet, dep_id="dep2")
        fleet.journal.clear()

        again = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )
        native.start(again, services=fleet.services, wait=True)

        assert all("dep2" not in name for name in fleet.verbs("stop_container"))
        live = [c.name for c in fleet.nodes[NODES[0]].client.containers.list(True)]
        assert "spark-pulse-dep2-r0-g1" in live

    def test_a_leftover_that_will_not_go_away_fails_the_deploy(self, native, fleet):
        self._running(native, fleet)
        again = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )

        with patch.object(nr, "_confirm_gone", return_value=False):
            record = native.start(again, services=fleet.services, wait=True)

        assert record["status"] == "error"
        assert "did not go away" in record["error_message"]
        # Nothing of the new generation was created.
        for address in NODES:
            live = [c.name for c in fleet.nodes[address].client.containers.list(True)]
            assert all("-g2" not in name for name in live)


class TestOrphans:
    def _running(self, native, fleet):
        plan = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )
        native.start(plan, services=fleet.services, wait=True)
        return plan

    def test_an_unreachable_node_leaves_an_outstanding_orphan(self, native, fleet):
        plan = self._running(native, fleet)
        fleet.nodes["10.0.0.3"].unreachable = True

        record = native.stop_deployment("dep1", services=fleet.services)

        assert record["status"] == "stopped"
        assert [o["rank"] for o in record["orphans"]] == [2]
        assert record["orphans"][0]["node"] == "10.0.0.3"
        assert record["orphans"][0]["container_name"] == "spark-pulse-dep1-r2-g1"
        assert plan.port

    def test_an_orphans_ports_stay_held(self, native, fleet):
        plan = self._running(native, fleet)
        fleet.nodes["10.0.0.3"].unreachable = True
        native.stop_deployment("dep1", services=fleet.services)

        # Stopped, but not released: nothing has confirmed rank 2 is gone, and
        # handing its port out again is the orphan bug this design refuses.
        assert plan.port in nr._ports_in_use()

    def test_ports_are_released_once_the_node_answers(self, native, fleet):
        plan = self._running(native, fleet)
        fleet.nodes["10.0.0.3"].unreachable = True
        native.stop_deployment("dep1", services=fleet.services)

        fleet.nodes["10.0.0.3"].unreachable = False
        fleet.nodes["10.0.0.3"].client.containers.remove_container(
            "spark-pulse-dep1-r2-g1"
        )
        cleared = native.sweep_orphans("dep1", services=fleet.services)

        assert cleared == 1
        assert native.get_deployment("dep1")["orphans"] == []
        assert plan.port not in nr._ports_in_use()

    def test_listing_sweeps_orphans_with_the_real_resolver(self, native, fleet):
        """The sweep runs off the node resolver, never off one passed service.

        Asking this machine's daemon about a container on another node answers
        "missing", which would free the ports on nothing but our own
        ignorance.
        """
        self._running(native, fleet)
        fleet.nodes["10.0.0.3"].unreachable = True
        native.stop_deployment("dep1", services=fleet.services)

        with patch.object(nr, "sweep_orphans") as swept:
            native.list_deployments(docker=fleet.nodes[NODES[0]])
        swept.assert_not_called()

        with patch.object(nr, "sweep_orphans") as swept:
            native.list_deployments()
        swept.assert_called_once_with()

    def test_a_deployment_with_orphans_is_not_dropped(self, native, fleet, records):
        self._running(native, fleet)
        fleet.nodes["10.0.0.3"].unreachable = True

        assert native.delete_deployment("dep1", services=fleet.services) is False
        assert native.get_deployment("dep1") is not None

    def test_a_container_still_present_after_a_stop_is_an_orphan(self, native, fleet):
        self._running(native, fleet)

        with patch.object(nr, "_confirm_gone", return_value=False):
            record = native.stop_deployment("dep1", services=fleet.services)

        assert [o["rank"] for o in record["orphans"]] == [0, 1, 2]
        assert "still present" in record["orphans"][0]["reason"]


class TestPerRankReads:
    def _running(self, native, fleet):
        plan = native.plan(
            "qwen3-8b-tp3", nodes=NODES, solo=False, deployment_id="dep1"
        )
        native.start(plan, services=fleet.services, wait=True)
        return plan

    def test_logs_default_to_rank_zero(self, native, fleet):
        self._running(native, fleet)
        assert "spark-pulse-dep1-r0-g1" in native.get_logs(
            "dep1", 50, services=fleet.services
        )

    def test_logs_can_name_a_rank(self, native, fleet):
        self._running(native, fleet)
        assert "spark-pulse-dep1-r2-g1" in native.get_logs(
            "dep1", 50, rank=2, services=fleet.services
        )

    def test_logs_for_a_rank_that_does_not_exist(self, native, fleet):
        self._running(native, fleet)
        assert "no rank 9" in native.get_logs(
            "dep1", 50, rank=9, services=fleet.services
        )

    def test_status_reports_every_rank(self, native, fleet):
        self._running(native, fleet)

        state = native.status("dep1", services=fleet.services)

        assert [r["rank"] for r in state["ranks"]] == [0, 1, 2]
        assert all(r["container"]["running"] for r in state["ranks"])
        # The scalar stays rank zero's for readers that predate ranks.
        assert state["container"] == state["ranks"][0]["container"]

    def test_an_unreachable_rank_reads_as_unknown_not_dead(self, native, fleet):
        self._running(native, fleet)
        fleet.nodes["10.0.0.2"].unreachable = True

        state = native.status("dep1", services=fleet.services)

        assert state["ranks"][1]["container"]["status"] == "unknown"
        assert state["status"] == "running"

    def test_listing_does_not_stop_a_deployment_on_a_node_it_cannot_see(
        self, native, fleet, docker
    ):
        self._running(native, fleet)

        # ``docker`` here is this machine's service and holds none of the
        # gang's containers. Marking the deployment stopped on that silence
        # would be releasing on inference.
        listed = native.list_deployments(docker=docker)

        assert [d["status"] for d in listed if d["id"] == "dep1"] == ["running"]


class TestADeletedDeploymentStaysDeleted:
    """A record must not come back after a successful delete.

    Every write to the record file is atomic, which is a different guarantee
    from the one that was missing. Two threads that each *load, change, save*
    still lose one of the changes, because the second writes back a list it
    read before the first landed — and a deploy runs its pull on a background
    thread that updates the record while the API thread can delete it.

    The zombie that leaves has no container behind it, holds its port, and is
    listed in the UI until somebody notices and deletes it a second time.
    """

    def test_an_update_racing_a_delete_does_not_resurrect_the_record(
        self, native, docker, records
    ):
        plan_obj = native.plan("qwen3-8b")
        dep_id = plan_obj.deployment_id
        nr.persist_planned_record(plan_obj, "pulling")
        assert nr.get_deployment(dep_id) is not None

        loaded = threading.Event()
        delete_began = threading.Event()
        writer: list[threading.Thread] = []
        real_save = nr._save_records

        def save_pausing_only_the_writer(records_to_save):
            """Hold the writer between its load and its save, nobody else.

            That gap is the whole bug: in the real path it is however long it
            takes to format a status update while an image downloads.
            """
            if writer and threading.current_thread() is writer[0]:
                loaded.set()
                delete_began.wait(timeout=5)
                time.sleep(0.2)
            real_save(records_to_save)

        with patch.object(
            nr, "_save_records", side_effect=save_pausing_only_the_writer
        ):
            thread = threading.Thread(
                target=lambda: nr._update_record(dep_id, status="pulling"),
                name="stale-writer",
                daemon=True,
            )
            writer.append(thread)
            thread.start()

            assert loaded.wait(timeout=5), "the writer never reached its save"
            delete_began.set()
            removed = nr.delete_deployment(dep_id, docker=docker)
            thread.join(timeout=5)

        assert removed is True
        assert nr.get_deployment(dep_id) is None, (
            "a record deleted while a background thread held a stale copy came "
            "back from the dead"
        )
