"""Tests for the deployments dispatcher and the /api/deployments router.

There is one runtime left, so every create is native. What the dispatcher still
decides is what happens to a record made by the *removed* upstream runner: it
routes by the record's own ``runtime`` field, so a deployment that was serving
before the upgrade can still be seen, read, stopped and deleted — see
``TestLegacyRecords``, which is the whole migration contract.
"""

from __future__ import annotations

import importlib
import json
import signal
import threading
from datetime import datetime, timezone
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
records = tools.deployment_records

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


@pytest.fixture
def env(tmp_path):
    """Temp record file, bundled engine registry, patched recipes/catalogue."""
    reset_registry()
    records = tmp_path / "deployments.json"
    docker = MockDockerService(MockDockerClient())
    with (
        patch.object(type(config), "engine_indexes", property(lambda self: [])),
        patch.object(tools.deployment_records, "RECORDS_FILE", records),
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
        # A deploy that had to pull runs on a background thread. Left running,
        # it writes its record *after* the patch above is released — into the
        # real simulation store, where the next test file finds an image
        # apparently in use. Join before the patch goes away.
        _join_deploy_threads()
    reset_registry()


def _join_deploy_threads(timeout: float = 5.0) -> None:
    """Wait for the runtime's own background threads to finish."""
    for thread in list(threading.enumerate()):
        if thread.name.startswith("native-") and thread.is_alive():
            thread.join(timeout)


@pytest.fixture
def client(env):
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


# ── Dispatcher routing ──────────────────────────────────────────────────────


class TestDispatchRouting:
    def test_a_create_is_native(self, env):
        record = dispatch.create_deployment("qwen3-8b", "n", {})

        assert record["runtime"] == "native"
        assert record["engine"] == "vllm"
        assert record["container_name"].startswith("spark-pulse-")

    def test_native_takes_a_cluster_request(self, env):
        """A node list does not change which runtime handles the create.

        A cluster is a deployment of size N, so it goes down the same path;
        what it meets there is the native runtime's own limit, raised from the
        runtime rather than from routing.
        """
        with patch.object(
            tools.native_runtime, "create_deployment", return_value={"id": "n"}
        ) as native:
            dispatch.create_deployment("qwen3-8b", "n", {}, nodes=["a", "b"])

        assert native.call_args.kwargs["nodes"] == ["a", "b"]

    def test_list_merges_legacy_and_native(self, env):
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
        native = dispatch.create_deployment("qwen3-8b", "n", {})

        listed = dispatch.list_deployments()
        assert [d["id"] for d in listed] == ["legacy", native["id"]]

    def test_plan_starts_nothing(self, env):
        plan = dispatch.plan_deployment("qwen3-8b")

        assert plan["runtime"] == "native"
        assert plan["engine"] == "vllm"
        assert env["docker"].list_managed_containers() == []


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
    def test_create_starts_a_container(self, client, env):
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
        with patch.object(
            tools.recipes,
            "get_recipe",
            side_effect=lambda rid, *a, **kw: _TP2_RECIPE,
        ):
            response = client.post(
                "/api/deployments",
                json={"recipe_id": "qwen3-8b", "name": "x", "params": {}},
            )

        assert response.status_code == 400
        assert "one GPU per node" in response.json()["detail"]

    def test_an_explicit_tensor_parallel_reaches_the_engine(self, client, env):
        """The caller's own params still override the recipe's defaults."""
        with patch.object(
            tools.recipes,
            "get_recipe",
            side_effect=lambda rid, *a, **kw: _TP2_RECIPE,
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

    def test_a_node_list_sizes_the_deployment(self, client):
        """N nodes is a deployment, not a second runtime.

        The dispatcher used to refuse any node list under the native runtime,
        so a multi-node request could only be served by the legacy runner.
        Now it is planned and started by the one path, and the record carries
        the size it was asked for. The addresses are the simulated registry's
        own, because a peer is deployed to by its registry record.
        """
        with patch.object(
            tools.recipes,
            "get_recipe",
            side_effect=lambda rid, *a, **kw: _TP2_RECIPE,
        ):
            response = client.post(
                "/api/deployments",
                json={
                    "recipe_id": "qwen3-8b",
                    "name": "x",
                    "nodes": ["192.168.1.100", "10.0.0.11"],
                },
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["node_count"] == 2
        assert [r["rank"] for r in body["ranks"]] == [0, 1]

    def test_an_unregistered_node_is_refused_by_name(self, client):
        """A peer is reached through its registry record, or not at all."""
        with patch.object(
            tools.recipes,
            "get_recipe",
            side_effect=lambda rid, *a, **kw: _TP2_RECIPE,
        ):
            response = client.post(
                "/api/deployments",
                json={
                    "recipe_id": "qwen3-8b",
                    "name": "x",
                    "nodes": ["192.168.1.100", "10.9.9.9"],
                },
            )

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "10.9.9.9" in detail
        assert "not in the node registry" in detail

    def test_create_of_an_unknown_recipe_is_404(self, client):
        response = client.post("/api/deployments", json={"recipe_id": "nope"})
        assert response.status_code == 404


class TestLifecycleEndpoints:
    def _create(self, client):
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


class TestLegacyRecords:
    """A deployment made by the removed upstream runner must not become a ghost.

    The runner is gone and nothing can create one of these again. What must
    survive the upgrade is the operator's ability to *see* and *end* one that
    was still serving: a record they cannot stop is a GPU held by a process the
    control plane no longer admits exists.
    """

    # Recent, so retention purging is not what the test is measuring.
    RECENTLY = datetime.now(timezone.utc).isoformat()

    LEGACY = {
        "id": "legacy",
        "name": "pre-upgrade",
        "recipe_id": "qwen3-8b",
        "status": "running",
        "pid": 4242,
        "port": 9000,
        "created_at": "2020-01-01T00:00:00+00:00",
        "log_path": None,
    }

    def _seed(self, env, **overrides):
        env["records"].write_text(json.dumps([{**self.LEGACY, **overrides}]))

    def test_a_legacy_record_is_still_listed(self, client, env):
        self._seed(env)
        with patch.object(records, "_pid_is_alive", return_value=True):
            listed = client.get("/api/deployments").json()

        assert [d["id"] for d in listed] == ["legacy"]
        assert listed[0]["status"] == "running"

    def test_stopping_one_signals_its_process_group(self, client, env):
        self._seed(env)
        with (
            patch.object(records, "_pid_is_alive", return_value=True),
            patch.object(records.os, "getpgid", return_value=4242) as getpgid,
            patch.object(records.os, "killpg") as killpg,
        ):
            response = client.delete("/api/deployments/legacy")

        assert response.status_code == 200
        assert response.json()["status"] == "stopped"
        getpgid.assert_called_once_with(4242)
        assert killpg.call_args[0] == (4242, signal.SIGTERM)
        # Through the store: state lives in the database now, and the seeded
        # JSON file is only its migration source.
        assert tools.deployment_records.load()[0]["status"] == "stopped"

    def test_a_process_that_is_already_gone_still_marks_the_record(self, client, env):
        self._seed(env)
        with (
            patch.object(records, "_pid_is_alive", return_value=True),
            patch.object(records.os, "getpgid", side_effect=ProcessLookupError),
        ):
            response = client.delete("/api/deployments/legacy")

        assert response.json()["status"] == "stopped"

    def test_a_dead_process_reconciles_the_record_to_stopped(self, client, env):
        self._seed(env)
        with patch.object(records, "_pid_is_alive", return_value=False):
            listed = client.get("/api/deployments").json()

        assert listed[0]["status"] == "stopped"

    def test_deleting_a_stopped_one_drops_the_record(self, client, env):
        self._seed(env, status="stopped", stopped_at=self.RECENTLY)
        response = client.delete("/api/deployments/legacy")

        assert response.json() == {"deleted": True, "id": "legacy"}
        assert tools.deployment_records.load() == []

    def test_deleting_one_stops_it_first(self, env):
        """Forgetting a deployment is not the same as ending it."""
        self._seed(env)
        with (
            patch.object(records, "_pid_is_alive", return_value=True),
            patch.object(records.os, "getpgid", return_value=4242),
            patch.object(records.os, "killpg") as killpg,
        ):
            assert dispatch.delete_deployment("legacy") is True

        assert killpg.called
        assert records.load() == []

    def test_its_own_log_file_is_still_readable(self, client, env, tmp_path):
        log = tmp_path / "legacy.log"
        log.write_text("line one\nline two\n")
        self._seed(env, log_path=str(log))

        response = client.get("/api/deployments/legacy/logs")
        assert "line two" in response.json()["logs"]

    def test_startup_names_a_legacy_deployment_that_is_still_running(self, env, capfd):
        self._seed(env)
        with patch.object(records, "_pid_is_alive", return_value=True):
            with TestClient(create_app()):
                pass

        out = capfd.readouterr().out
        assert "legacy" in out
        assert "upstream runtime" in out

    def test_startup_is_quiet_when_nothing_legacy_is_running(self, env, capfd):
        self._seed(env, status="stopped", stopped_at=self.RECENTLY)
        with TestClient(create_app()):
            pass

        assert "upstream runtime" not in capfd.readouterr().out


class TestConfigExposesRuntime:
    def test_api_config_reports_the_runtime(self, client):
        assert client.get("/api/config").json()["runtime"] == "native"

    def test_settings_report_the_runtime(self, client):
        data = client.get("/api/settings").json()
        assert data["runtime"] == "native"
        assert data["deploy_ready_timeout_seconds"] == 900


class TestRuntimeFlagValidation:
    """``native`` is the only runtime, so nothing else can be selected.

    The fallback used to be ``upstream``, on the reasoning that a typo must
    never silently switch the deploy path. There is one path now, and an
    operator upgrading with ``runtime: upstream`` still in their settings.json
    must land on it rather than on a runtime that was deleted.
    """

    def test_an_unknown_runtime_falls_back_to_native(self):
        with patch.dict("os.environ", {"SPARK_PULSE_RUNTIME": "nonsense"}):
            assert config.runtime == "native"

    def test_a_stale_upstream_setting_resolves_to_native(self):
        with patch.dict("os.environ", {"SPARK_PULSE_RUNTIME": "upstream"}):
            assert config.runtime == "native"

    def test_the_env_var_selects_native(self):
        with patch.dict("os.environ", {"SPARK_PULSE_RUNTIME": "native"}):
            assert config.runtime == "native"


class TestUnreadableRecordFile:
    """A damaged deployments.json must never read as "nothing is deployed".

    See tests/test_state_durability.py for the store's own guarantees.
    """

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
        response = client.post(
            "/api/deployments",
            json={"recipe_id": "qwen3-8b", "name": "x", "engine": "vllm"},
        )

        assert response.status_code == 200
        assert tools.deployment_records.load()[0]["id"] == response.json()["id"]
        leftovers = [
            p for p in env["records"].parent.iterdir() if p.name.endswith(".tmp")
        ]
        assert leftovers == []
