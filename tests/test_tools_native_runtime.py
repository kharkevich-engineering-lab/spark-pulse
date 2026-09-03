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
from pathlib import Path
from unittest.mock import patch

import pytest

from spark_pulse import tools
from spark_pulse.config import config
from spark_pulse.engines import EngineRegistry, Topology, reset_registry
from spark_pulse.mock.docker import MockDockerClient, MockDockerService
from spark_pulse.tools.labels import DEPLOYMENT_LABEL, MANAGED_LABEL

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
    "defaults": {"port": 8000, "tensor_parallel": 2},
    "mods": [],
    "env": {},
}

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
    r["id"]: r for r in (V1_RECIPE, V1_UNKNOWN_TAG, V1_NO_PORT, V1_WITH_MODS, V2_RECIPE)
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
    with patch.object(tools.deployments, "_DEPLOYMENTS_FILE", path):
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
        # Solo forces tp=1 and strips the Ray backend, as upstream does.
        assert "--tensor-parallel-size 1" in plan.launch_command
        assert "--distributed-executor-backend" not in plan.launch_command

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

        saved = json.loads(records.read_text())
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

    def test_a_cluster_plan_is_refused(self, native, docker):
        plan = native.plan("qwen3-8b", nodes=["a", "b"], solo=False)
        with pytest.raises(native.NativeRuntimeError) as exc:
            native.start(plan, docker=docker)
        assert "cluster" in str(exc.value)

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
        assert json.loads(records.read_text()) == []

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
        records.write_text(json.dumps([]))

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
