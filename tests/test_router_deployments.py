"""Tests for the deployments dispatcher and the /api/deployments router.

The dispatcher decides *which* runtime handles a call. Two rules are checked
here, because they are different on purpose: creating follows the ``runtime``
config flag, while acting on an existing deployment follows that record's own
``runtime`` field, so records survive a flag flip.
"""

from __future__ import annotations

import importlib
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from spark_pulse import tools
from spark_pulse.app import create_app
from spark_pulse.config import config
from spark_pulse.engines import EngineRegistry, reset_registry
from spark_pulse.mock.docker import MockDockerClient, MockDockerService
from spark_pulse.tools.atomic_json import StateFileError

# See the note in test_tools_native_runtime.py — the real modules are what run.
dispatch = importlib.import_module("spark_pulse.tools.deploy_dispatch")
nr = importlib.import_module("spark_pulse.tools.native_runtime")

RECIPE = {
    "id": "qwen3-8b",
    "name": "Qwen3 8B",
    "model": "Qwen/Qwen3-8B",
    "container": "vllm-node",
    "command": "vllm serve Qwen/Qwen3-8B --port {port}",
    "defaults": {"port": 8000},
    "mods": [],
    "env": {},
}

# A recipe whose default parallelism needs two GPUs, and so two Sparks.
_TP2_RECIPE = {
    **RECIPE,
    "command": "vllm serve Qwen/Qwen3-8B --port {port} -tp {tensor_parallel}",
    "defaults": {"port": 8000, "tensor_parallel": 2},
}

CATALOGUE = [{"id": "Qwen/Qwen3-8B", "source": "hf", "path": "/models/qwen3-8b"}]


def _runtime(value: str):
    """Patch the runtime flag without touching the user's settings file."""
    return patch.object(type(config), "runtime", property(lambda self: value))


@pytest.fixture
def env(tmp_path):
    """Temp record file, bundled engine registry, patched recipes/catalogue."""
    reset_registry()
    records = tmp_path / "deployments.json"
    docker = MockDockerService(MockDockerClient())
    with (
        patch.object(type(config), "engine_indexes", property(lambda self: [])),
        patch.object(tools.deployments, "_DEPLOYMENTS_FILE", records),
        patch.object(
            tools.recipes,
            "get_recipe",
            side_effect=lambda rid, *a, **kw: RECIPE if rid == RECIPE["id"] else None,
        ),
        patch.object(
            tools.models,
            "get_model",
            side_effect=lambda mid: next(
                (m for m in CATALOGUE if m["id"] == mid), None
            ),
        ),
        patch(
            "spark_pulse.tools.native_runtime.get_registry",
            return_value=EngineRegistry(cache_dir=tmp_path / "engine-cache"),
        ),
        patch.object(nr, "_docker_service", return_value=docker),
    ):
        yield {"records": records, "docker": docker}
    reset_registry()


@pytest.fixture
def client(env):
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


# ── Dispatcher routing ──────────────────────────────────────────────────────


class TestDispatchRouting:
    def test_upstream_flag_uses_the_upstream_runtime(self, env):
        with _runtime("upstream"):
            with patch.object(
                tools.deployments, "create_deployment", return_value={"id": "up"}
            ) as upstream:
                result = dispatch.create_deployment("qwen3-8b", "n", {"port": 8000})

        assert result == {"id": "up"}
        assert upstream.called

    def test_native_flag_uses_the_native_runtime(self, env):
        with _runtime("native"):
            record = dispatch.create_deployment("qwen3-8b", "n", {})

        assert record["runtime"] == "native"
        assert record["engine"] == "vllm"
        assert record["container_name"].startswith("spark-pulse-")

    def test_native_refuses_a_cluster_request(self, env):
        with _runtime("native"):
            with pytest.raises(nr.NativeRuntimeError) as exc:
                dispatch.create_deployment("qwen3-8b", "n", {}, nodes=["a", "b"])

        assert "cluster deployments are not native yet" in str(exc.value)

    def test_upstream_still_accepts_a_cluster_request(self, env):
        with _runtime("upstream"):
            with patch.object(
                tools.deployments, "create_deployment", return_value={"id": "up"}
            ) as upstream:
                dispatch.create_deployment("qwen3-8b", "n", {}, nodes=["a", "b"])

        assert upstream.call_args.kwargs["nodes"] == ["a", "b"]

    def test_uses_native_helper(self):
        with _runtime("native"):
            assert dispatch.uses_native() is True
            assert dispatch.uses_native(["a"]) is False
        with _runtime("upstream"):
            assert dispatch.uses_native() is False

    def test_existing_records_route_by_their_own_runtime(self, env):
        """A native record stays native after the flag flips back."""
        with _runtime("native"):
            record = dispatch.create_deployment("qwen3-8b", "n", {})

        with _runtime("upstream"):
            stopped = dispatch.stop_deployment(record["id"])
            logs = dispatch.get_logs(record["id"])

        assert stopped["status"] == "stopped"
        assert record["container_name"] in logs

    def test_upstream_records_route_to_the_upstream_runtime(self, env):
        env["records"].write_text(
            json.dumps([{"id": "legacy", "status": "running", "pid": 1234}])
        )
        with _runtime("native"):
            with patch.object(
                tools.deployments, "stop_deployment", return_value={"id": "legacy"}
            ) as upstream:
                dispatch.stop_deployment("legacy")

        assert upstream.called

    def test_list_merges_both_runtimes(self, env):
        env["records"].write_text(
            json.dumps(
                [
                    {
                        "id": "legacy",
                        "status": "stopped",
                        "created_at": "2020-01-01T00:00:00+00:00",
                    }
                ]
            )
        )
        with _runtime("native"):
            native = dispatch.create_deployment("qwen3-8b", "n", {})

        listed = dispatch.list_deployments()
        assert [d["id"] for d in listed] == ["legacy", native["id"]]

    def test_plan_is_always_native(self, env):
        with _runtime("upstream"):
            plan = dispatch.plan_deployment("qwen3-8b")

        assert plan["runtime"] == "native"
        assert plan["engine"] == "vllm"


# ── Router ──────────────────────────────────────────────────────────────────


class TestPlanEndpoint:
    def test_plan_returns_the_resolved_deployment(self, client):
        response = client.post("/api/deployments/plan", json={"recipe_id": "qwen3-8b"})

        assert response.status_code == 200
        data = response.json()
        assert data["engine"] == "vllm"
        assert data["image_ref"]
        assert data["model"] == "Qwen/Qwen3-8B"
        assert data["launch_command"].startswith("vllm serve")
        assert data["container"]["privileged"] is True
        assert data["ranks"][0]["script"].startswith("#!/usr/bin/env bash")

    def test_plan_accepts_engine_model_and_extra_args(self, client):
        response = client.post(
            "/api/deployments/plan",
            json={
                "recipe_id": "qwen3-8b",
                "engine": "vllm",
                "model": "somebody/other-model",
                "extra_args": ["--enable-prefix-caching"],
                "params": {"port": 9321},
            },
        )

        data = response.json()
        assert data["port"] == 9321
        assert data["launch_command"].endswith("--enable-prefix-caching")
        assert data["model"] == "somebody/other-model"

    def test_plan_of_an_unknown_recipe_is_400(self, client):
        response = client.post("/api/deployments/plan", json={"recipe_id": "nope"})
        assert response.status_code == 400
        assert "not found" in response.json()["detail"]

    def test_plan_explains_an_engine_that_cannot_run_the_recipe(self, client):
        response = client.post(
            "/api/deployments/plan", json={"recipe_id": "qwen3-8b", "engine": "sglang"}
        )
        assert response.status_code == 400
        assert "sglang" in response.json()["detail"]


class TestCreateEndpoint:
    def test_create_under_upstream_forks_the_script(self, client):
        with _runtime("upstream"):
            with patch.object(
                tools.deployments,
                "create_deployment",
                return_value={"id": "up", "status": "running"},
            ) as upstream:
                response = client.post(
                    "/api/deployments", json={"recipe_id": "qwen3-8b", "name": "x"}
                )

        assert response.status_code == 200
        assert response.json()["id"] == "up"
        assert upstream.called

    def test_create_under_native_starts_a_container(self, client, env):
        with _runtime("native"):
            response = client.post(
                "/api/deployments",
                json={"recipe_id": "qwen3-8b", "name": "x", "engine": "vllm"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["runtime"] == "native"
        assert data["engine"] == "vllm"
        assert data["container_name"] in [
            c.name for c in env["docker"].list_managed_containers()
        ]

    def test_a_recipe_asking_for_two_gpus_on_one_node_is_refused(self, client, env):
        """The old path silently rewrote this to ``-tp 1``; now it says why.

        One GPU per node means two-way tensor parallelism needs two nodes. A
        rewrite hid that from the operator, and the rewrite is gone.
        """
        with (
            patch.object(
                tools.recipes,
                "get_recipe",
                side_effect=lambda rid, *a, **kw: _TP2_RECIPE,
            ),
            _runtime("native"),
        ):
            response = client.post(
                "/api/deployments",
                json={"recipe_id": "qwen3-8b", "name": "x", "params": {}},
            )

        assert response.status_code == 400
        assert "one GPU per node" in response.json()["detail"]

    def test_an_explicit_tensor_parallel_reaches_the_engine(self, client, env):
        """The caller's own params still override the recipe's defaults."""
        with (
            patch.object(
                tools.recipes,
                "get_recipe",
                side_effect=lambda rid, *a, **kw: _TP2_RECIPE,
            ),
            _runtime("native"),
        ):
            response = client.post(
                "/api/deployments",
                json={
                    "recipe_id": "qwen3-8b",
                    "name": "x",
                    "params": {"tensor_parallel": 1},
                },
            )

        assert response.status_code == 200
        assert "-tp 1" in response.json()["launch_command"]

    def test_create_under_native_refuses_a_cluster(self, client):
        with _runtime("native"):
            response = client.post(
                "/api/deployments",
                json={"recipe_id": "qwen3-8b", "name": "x", "nodes": ["a", "b"]},
            )

        assert response.status_code == 400
        assert "cluster" in response.json()["detail"]

    def test_create_of_an_unknown_recipe_is_404(self, client):
        response = client.post("/api/deployments", json={"recipe_id": "nope"})
        assert response.status_code == 404


class TestLifecycleEndpoints:
    def _create(self, client):
        with _runtime("native"):
            return client.post(
                "/api/deployments", json={"recipe_id": "qwen3-8b", "name": "x"}
            ).json()

    def test_get_returns_live_status(self, client):
        created = self._create(client)
        response = client.get(f"/api/deployments/{created['id']}")

        assert response.status_code == 200
        assert response.json()["ready"] is True

    def test_get_unknown_is_404(self, client):
        assert client.get("/api/deployments/nope").status_code == 404

    def test_logs_come_from_the_container(self, client):
        created = self._create(client)
        response = client.get(f"/api/deployments/{created['id']}/logs")

        assert response.status_code == 200
        assert created["container_name"] in response.json()["logs"]

    def test_delete_stops_a_running_deployment(self, client, env):
        created = self._create(client)
        response = client.delete(f"/api/deployments/{created['id']}")

        assert response.status_code == 200
        assert response.json()["status"] == "stopped"
        assert created["container_name"] not in [
            c.name for c in env["docker"].list_managed_containers()
        ]

    def test_delete_of_a_stopped_deployment_removes_it(self, client):
        created = self._create(client)
        client.delete(f"/api/deployments/{created['id']}")

        response = client.delete(f"/api/deployments/{created['id']}")
        assert response.json() == {"deleted": True, "id": created["id"]}
        assert client.get("/api/deployments").json() == []

    def test_list_shows_the_native_deployment(self, client):
        created = self._create(client)
        listed = client.get("/api/deployments").json()

        assert [d["id"] for d in listed] == [created["id"]]
        assert listed[0]["runtime"] == "native"


class TestConfigExposesRuntime:
    def test_api_config_reports_the_runtime(self, client):
        with _runtime("native"):
            assert client.get("/api/config").json()["runtime"] == "native"
        with _runtime("upstream"):
            assert client.get("/api/config").json()["runtime"] == "upstream"

    def test_settings_report_the_runtime(self, client):
        data = client.get("/api/settings").json()
        assert data["runtime"] in ("upstream", "native")
        assert data["deploy_ready_timeout_seconds"] == 900


class TestRuntimeFlagValidation:
    def test_an_unknown_runtime_falls_back_to_upstream(self):
        with patch.dict("os.environ", {"SPARK_PULSE_RUNTIME": "nonsense"}):
            assert config.runtime == "upstream"

    def test_the_env_var_selects_native(self):
        with patch.dict("os.environ", {"SPARK_PULSE_RUNTIME": "native"}):
            assert config.runtime == "native"
            assert config.native_runtime is True


class TestUnreadableRecordFile:
    """Both runtimes share deployments.json, so both must refuse to read a
    damaged one as "nothing is deployed". See tests/test_state_durability.py."""

    def test_a_missing_record_file_is_an_empty_list(self, env):
        assert not env["records"].exists()
        assert dispatch.list_deployments() == []

    def test_listing_raises_instead_of_reporting_an_empty_cluster(self, env):
        env["records"].write_text('[{"id": "dep-1", "runtime": "nati')

        with pytest.raises(StateFileError) as exc:
            dispatch.list_deployments()

        assert exc.value.path == env["records"]

    def test_the_corrupt_record_file_is_moved_aside(self, env):
        env["records"].write_text("not json at all")

        with pytest.raises(StateFileError):
            dispatch.list_deployments()

        moved = list(env["records"].parent.glob("deployments.json.corrupt.*"))
        assert len(moved) == 1
        assert moved[0].read_text() == "not json at all"
        assert not env["records"].exists()

    def test_creating_a_deployment_leaves_no_temp_file_behind(self, client, env):
        with _runtime("native"):
            response = client.post(
                "/api/deployments",
                json={"recipe_id": "qwen3-8b", "name": "x", "engine": "vllm"},
            )

        assert response.status_code == 200
        assert json.loads(env["records"].read_text())[0]["id"] == response.json()["id"]
        leftovers = [
            p for p in env["records"].parent.iterdir() if p.name.endswith(".tmp")
        ]
        assert leftovers == []
