"""The /api/docker endpoints, over the wire.

``tests/test_docker_client_hygiene.py`` proves the router holds one
``DockerService`` for the process. This file is about what each endpoint
actually answers: which arguments reach the container service, which status
code each failure produces, and what the JSON body looks like — including the
bodies the frontend parses on the error paths.

Nothing here touches a Docker daemon. The router binds the container
primitives into its own namespace at import time, so that is where they are
replaced; the shared service is reached through ``_docker_service``, so the
real-mode tests swap that.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spark_pulse.app import create_app
from spark_pulse.config import config
from spark_pulse.routers import docker as docker_router
from spark_pulse.tools.docker import ContainerInfo, ContainerMetadata, ExecResult

IMAGE = "ghcr.io/example/engine:0.1.0"


@pytest.fixture
def client():
    """The app without lifespan — these endpoints need no startup work."""
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def real_mode(monkeypatch):
    """Take the endpoints off their simulation short-circuits."""
    monkeypatch.setattr(docker_router, "is_simulation", lambda: False)


class FakeService:
    """A stand-in for the shared ``DockerService``."""

    def __init__(self, exec_result=None, status=None, logs=b""):
        self.exec_result = exec_result
        self.status = status or {"status": "running", "running": True}
        self.logs = logs
        self.exec_calls: list[tuple[str, str]] = []
        self.log_calls: list[tuple[str, int]] = []
        self._service = self

    def exec_in_container(self, name, command):
        self.exec_calls.append((name, command))
        if isinstance(self.exec_result, Exception):
            raise self.exec_result
        return self.exec_result

    def get_container_status(self, name):
        return self.status

    @property
    def client(self):
        service = self

        class _Container:
            def logs(self, tail=100):
                service.log_calls.append(("logs", tail))
                if isinstance(service.logs, Exception):
                    raise service.logs
                return service.logs

        class _Containers:
            def get(self, name):
                service.log_calls.append(("get", name))
                return _Container()

        class _Client:
            containers = _Containers()

        return _Client()


@pytest.fixture
def service(monkeypatch):
    """Install a fake shared service behind ``_service()``."""
    fake = FakeService()
    monkeypatch.setattr(docker_router, "_docker_service", lambda: fake)
    return fake


# ── POST /deployments/{name}/run ─────────────────────────────────────────────


class TestRunDeployment:
    """Starting a container: what the router hands the container service."""

    @pytest.fixture
    def captured(self, monkeypatch):
        calls: list[dict] = []

        def _run(**kwargs):
            calls.append(kwargs)
            return ContainerInfo(
                id="c0ffee",
                name=kwargs["name"],
                status="running",
                image=kwargs["image"],
            )

        monkeypatch.setattr(docker_router, "run_container", _run)
        return calls

    def test_a_started_container_comes_back_as_a_summary(self, client, captured):
        resp = client.post(
            "/api/docker/deployments/qwen/run",
            json={"image": IMAGE, "recipe": "qwen-8b"},
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "success",
            "container": {
                "id": "c0ffee",
                "name": "qwen",
                "status": "running",
                "image": IMAGE,
            },
        }

    def test_the_body_reaches_the_container_service_unchanged(self, client, captured):
        resp = client.post(
            "/api/docker/deployments/qwen/run",
            json={
                "image": IMAGE,
                "recipe": "qwen-8b",
                "env": {"VLLM_MODEL": "qwen"},
                "privileged": False,
                "memory_limit_gb": 12.5,
                "shm_size_gb": 8,
                "cache_dirs": ["/mnt/models"],
                "port_mappings": ["8000:8000"],
            },
        )

        assert resp.status_code == 200
        (call,) = captured
        assert call["image"] == IMAGE
        assert call["name"] == "qwen"
        assert call["env_vars"] == {"VLLM_MODEL": "qwen"}
        assert call["privileged"] is False
        assert call["memory_limit_gb"] == 12.5
        assert call["shm_size_gb"] == 8
        assert call["cache_dirs"] == ["/mnt/models"]
        assert call["port_mappings"] == ["8000:8000"]

    def test_the_metadata_describes_the_deployment(self, client, captured):
        client.post(
            "/api/docker/deployments/qwen/run",
            json={
                "image": IMAGE,
                "recipe": "qwen-8b",
                "privileged": False,
                "memory_limit_gb": 12.5,
                "shm_size_gb": 8,
            },
        )

        metadata = captured[0]["metadata"]
        assert isinstance(metadata, ContainerMetadata)
        # The deployment name is the path segment, not anything in the body.
        assert metadata.deployment == "qwen"
        assert metadata.recipe == "qwen-8b"
        assert metadata.image == IMAGE
        assert metadata.mode == "solo"
        assert metadata.privileged is False
        assert metadata.memory_limit_gb == 12.5
        assert metadata.shm_size_gb == 8

    def test_an_absent_field_falls_back_to_configuration(self, client, captured):
        """The operator's config is the default, not a hard-coded literal."""
        client.post(
            "/api/docker/deployments/qwen/run",
            json={"image": IMAGE, "recipe": "qwen-8b"},
        )

        (call,) = captured
        assert call["env_vars"] == {}
        assert call["privileged"] == config.docker_privileged
        assert call["memory_limit_gb"] == config.docker_memory_limit_gb
        assert call["shm_size_gb"] == config.docker_shm_size_gb
        assert call["cache_dirs"] == config.docker_cache_dirs
        assert call["port_mappings"] is None

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"image": IMAGE},
            {"recipe": "qwen-8b"},
            {"image": "", "recipe": "qwen-8b"},
        ],
    )
    def test_an_incomplete_body_is_rejected_before_docker_is_touched(
        self, client, captured, body
    ):
        resp = client.post("/api/docker/deployments/qwen/run", json=body)

        assert resp.status_code == 400
        assert resp.json()["detail"] == "image and recipe are required"
        assert captured == []

    def test_a_docker_failure_is_a_500_carrying_the_reason(self, client, monkeypatch):
        def _boom(**_kwargs):
            raise RuntimeError("no such image")

        monkeypatch.setattr(docker_router, "run_container", _boom)

        resp = client.post(
            "/api/docker/deployments/qwen/run",
            json={"image": IMAGE, "recipe": "qwen-8b"},
        )

        assert resp.status_code == 500
        assert resp.json()["detail"] == "no such image"

    def test_an_unexpected_failure_is_still_a_500(self, client, monkeypatch):
        def _boom(**_kwargs):
            raise ValueError("bad port mapping")

        monkeypatch.setattr(docker_router, "run_container", _boom)

        resp = client.post(
            "/api/docker/deployments/qwen/run",
            json={"image": IMAGE, "recipe": "qwen-8b"},
        )

        assert resp.status_code == 500
        assert resp.json()["detail"] == "bad port mapping"


# ── POST /deployments/{name}/stop ────────────────────────────────────────────


class TestStopDeployment:
    def test_a_stopped_container_reports_success(self, client, monkeypatch):
        stopped: list[str] = []
        monkeypatch.setattr(
            docker_router,
            "stop_container",
            lambda name: stopped.append(name) or True,
        )

        resp = client.post("/api/docker/deployments/qwen/stop")

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "success",
            "message": "Container 'qwen' stopped",
        }
        assert stopped == ["qwen"]

    def test_nothing_to_stop_is_a_404(self, client, monkeypatch):
        monkeypatch.setattr(docker_router, "stop_container", lambda name: False)

        resp = client.post("/api/docker/deployments/ghost/stop")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Container 'ghost' not found or already stopped"

    def test_a_docker_failure_is_a_500(self, client, monkeypatch):
        def _boom(_name):
            raise RuntimeError("daemon unreachable")

        monkeypatch.setattr(docker_router, "stop_container", _boom)

        resp = client.post("/api/docker/deployments/qwen/stop")

        assert resp.status_code == 500
        assert resp.json()["detail"] == "daemon unreachable"


# ── GET /deployments and /containers ─────────────────────────────────────────


def _managed(name: str) -> ContainerInfo:
    return ContainerInfo(
        id=f"id-{name}",
        name=name,
        status="running",
        image=IMAGE,
        metadata=ContainerMetadata(
            deployment=name,
            recipe="qwen-8b",
            image=IMAGE,
            mode="solo",
            created_at="2026-01-01T00:00:00Z",
            privileged=True,
        ),
        labels={"spark-pulse.managed": "true"},
    )


class TestListDeployments:
    def test_each_container_is_flattened_for_the_frontend(self, client, monkeypatch):
        monkeypatch.setattr(
            docker_router, "list_managed_containers", lambda: [_managed("qwen")]
        )

        resp = client.get("/api/docker/deployments")

        assert resp.status_code == 200
        assert resp.json() == [
            {
                "id": "id-qwen",
                "name": "qwen",
                "status": "running",
                "image": IMAGE,
                "metadata": {
                    "deployment": "qwen",
                    "recipe": "qwen-8b",
                    "mode": "solo",
                    "created_at": "2026-01-01T00:00:00Z",
                    "privileged": True,
                },
            }
        ]

    def test_no_containers_is_an_empty_list_not_an_error(self, client, monkeypatch):
        monkeypatch.setattr(docker_router, "list_managed_containers", lambda: [])

        resp = client.get("/api/docker/deployments")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_a_docker_failure_is_a_500(self, client, monkeypatch):
        def _boom():
            raise RuntimeError("daemon unreachable")

        monkeypatch.setattr(docker_router, "list_managed_containers", _boom)

        resp = client.get("/api/docker/deployments")

        assert resp.status_code == 500
        assert resp.json()["detail"] == "daemon unreachable"

    def test_containers_is_an_alias_for_deployments(self, client, monkeypatch):
        monkeypatch.setattr(
            docker_router,
            "list_managed_containers",
            lambda: [_managed("qwen"), _managed("llama")],
        )

        assert (
            client.get("/api/docker/containers").json()
            == client.get("/api/docker/deployments").json()
        )


# ── GET /deployments/{name}/status ───────────────────────────────────────────


class TestDeploymentStatus:
    def test_the_status_dict_is_passed_through(self, client, monkeypatch):
        snapshot = {
            "status": "running",
            "running": True,
            "id": "id-qwen",
            "state": {"Status": "running"},
        }
        monkeypatch.setattr(
            docker_router, "get_container_status", lambda name: snapshot
        )

        resp = client.get("/api/docker/deployments/qwen/status")

        assert resp.status_code == 200
        assert resp.json() == snapshot

    def test_a_missing_container_is_a_404(self, client, monkeypatch):
        monkeypatch.setattr(
            docker_router,
            "get_container_status",
            lambda name: {"status": "missing", "running": False},
        )

        resp = client.get("/api/docker/deployments/ghost/status")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Container 'ghost' not found"

    def test_a_docker_failure_is_a_500(self, client, monkeypatch):
        def _boom(_name):
            raise RuntimeError("daemon unreachable")

        monkeypatch.setattr(docker_router, "get_container_status", _boom)

        resp = client.get("/api/docker/deployments/qwen/status")

        assert resp.status_code == 500
        assert resp.json()["detail"] == "daemon unreachable"


# ── POST /deployments/{name}/exec ────────────────────────────────────────────


class TestExecInDeployment:
    def test_simulation_answers_without_a_container_service(self, client, monkeypatch):
        def _no_service():
            raise AssertionError("simulation must not reach the Docker service")

        monkeypatch.setattr(docker_router, "_docker_service", _no_service)

        resp = client.post(
            "/api/docker/deployments/qwen/exec", json={"command": "nvidia-smi"}
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "success",
            "output": "Simulated exec of: nvidia-smi",
        }

    @pytest.mark.parametrize("body", [{}, {"command": ""}])
    def test_an_empty_command_is_a_400(self, client, service, real_mode, body):
        resp = client.post("/api/docker/deployments/qwen/exec", json=body)

        assert resp.status_code == 400
        assert resp.json()["detail"] == "command is required"
        assert service.exec_calls == []

    def test_the_command_and_container_reach_the_service(
        self, client, service, real_mode
    ):
        service.exec_result = ExecResult(returncode=0, stdout="ok\n")

        resp = client.post(
            "/api/docker/deployments/qwen/exec", json={"command": "echo ok"}
        )

        assert resp.status_code == 200
        assert resp.json() == {"status": "success", "output": "ok\n"}
        assert service.exec_calls == [("qwen", "echo ok")]

    def test_stderr_is_appended_to_stdout(self, client, service, real_mode):
        """A failing command must still show the operator what it said."""
        service.exec_result = ExecResult(
            returncode=1, stdout="partial\n", stderr="boom\n"
        )

        resp = client.post(
            "/api/docker/deployments/qwen/exec", json={"command": "false"}
        )

        assert resp.json()["output"] == "partial\nboom\n"

    def test_stderr_alone_is_the_whole_output(self, client, service, real_mode):
        service.exec_result = ExecResult(returncode=1, stdout="", stderr="boom\n")

        assert (
            client.post(
                "/api/docker/deployments/qwen/exec", json={"command": "false"}
            ).json()["output"]
            == "boom\n"
        )

    def test_a_plain_string_result_is_returned_as_is(self, client, service, real_mode):
        """Remote services hand back a bare string; it must not be dropped."""
        service.exec_result = "legacy output\n"

        assert (
            client.post(
                "/api/docker/deployments/qwen/exec", json={"command": "echo"}
            ).json()["output"]
            == "legacy output\n"
        )

    def test_an_unrecognised_result_becomes_an_empty_output(
        self, client, service, real_mode
    ):
        service.exec_result = object()

        resp = client.post(
            "/api/docker/deployments/qwen/exec", json={"command": "echo"}
        )

        assert resp.status_code == 200
        assert resp.json() == {"status": "success", "output": ""}

    def test_a_docker_failure_is_a_500(self, client, service, real_mode):
        service.exec_result = RuntimeError("container is gone")

        resp = client.post(
            "/api/docker/deployments/qwen/exec", json={"command": "echo"}
        )

        assert resp.status_code == 500
        assert resp.json()["detail"] == "container is gone"


# ── GET /containers/{name}/logs ──────────────────────────────────────────────


class TestContainerLogs:
    def test_simulation_returns_a_capped_canned_tail(self, client):
        resp = client.get("/api/docker/containers/qwen/logs")

        assert resp.status_code == 200
        logs = resp.json()["logs"]
        assert len(logs) == 10
        assert logs[0] == "[sim] Line 0: Container qwen log entry"

    def test_simulation_honours_a_smaller_tail(self, client):
        resp = client.get("/api/docker/containers/qwen/logs?tail=3")

        assert resp.json()["logs"] == [
            f"[sim] Line {idx}: Container qwen log entry" for idx in range(3)
        ]

    def test_a_stopped_container_is_a_400_naming_its_state(
        self, client, service, real_mode
    ):
        service.status = {"status": "exited", "running": False}

        resp = client.get("/api/docker/containers/qwen/logs")

        assert resp.status_code == 400
        assert (
            resp.json()["detail"] == "Container 'qwen' is not running (status: exited)"
        )
        assert service.log_calls == []

    def test_a_running_container_is_split_into_lines(self, client, service, real_mode):
        service.logs = b"first\nsecond\n"

        resp = client.get("/api/docker/containers/qwen/logs?tail=42")

        assert resp.status_code == 200
        assert resp.json() == {"logs": ["first", "second"]}
        assert service.log_calls == [("get", "qwen"), ("logs", 42)]

    def test_a_docker_failure_is_a_500(self, client, service, real_mode):
        service.logs = RuntimeError("stream closed")

        resp = client.get("/api/docker/containers/qwen/logs")

        assert resp.status_code == 500
        assert resp.json()["detail"] == "stream closed"


class TestSharedService:
    def test_the_router_reaches_the_process_wide_service(self, monkeypatch):
        """``_service()`` is a lookup, never a constructor."""
        sentinel = object()
        monkeypatch.setattr(docker_router, "_docker_service", lambda: sentinel)

        assert docker_router._service() is sentinel
