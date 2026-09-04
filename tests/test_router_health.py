"""Tests for the /api/health router and the simulated monitor behind it.

Neither had a test, and between them the whole feature was inert under
SIMULATION_MODE: ``mock/health.py`` shared *no* names with its real twin — no
``get_health_monitor``, no ``start_health_monitor``, no ``stop_health_monitor``
— so every endpoint answered with its own ``AttributeError`` in an ``error``
field, which a caller cannot tell apart from a genuinely sick deployment. The
container lookup had the same shape of fault: it built ``docker.DockerService``
directly, and that name is the *real* class in both packages (the mock
subclasses it), so a simulated health check reached for a real Docker daemon.

Both are fixed; these tests hold them fixed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spark_pulse import tools
from spark_pulse.app import create_app
from spark_pulse.mock import health as mock_health
from spark_pulse.routers import health as health_router


@pytest.fixture(autouse=True)
def fresh_monitor():
    """One simulated machine per test."""
    mock_health.reset()
    yield
    mock_health.reset()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(health_router, "health", mock_health)
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def deployment(monkeypatch):
    """One deployment record, without touching the deployments file."""
    record = {"id": "dep-1", "container_name": "vllm-node"}
    monkeypatch.setattr(
        tools.deployment_records,
        "get",
        lambda dep_id: record if dep_id == record["id"] else None,
    )
    return record


# ── The deployment check ─────────────────────────────────────────────────────


class TestCheckDeploymentHealth:
    def test_a_running_container_reports_a_live_process(
        self, client, deployment, monkeypatch
    ):
        from spark_pulse.tools import docker

        monkeypatch.setattr(
            docker, "get_container_status", lambda name: {"status": "running"}
        )

        body = client.get("/api/health/deployment/dep-1").json()

        assert body == {
            "deployment_id": "dep-1",
            "container_status": "running",
            "process_status": "alive",
        }

    def test_a_stopped_container_reports_a_dead_process(
        self, client, deployment, monkeypatch
    ):
        from spark_pulse.tools import docker

        monkeypatch.setattr(
            docker, "get_container_status", lambda name: {"status": "exited"}
        )

        body = client.get("/api/health/deployment/dep-1").json()

        assert body["container_status"] == "exited"
        assert body["process_status"] == "dead"

    def test_the_container_named_by_the_record_is_the_one_checked(
        self, client, deployment, monkeypatch
    ):
        from spark_pulse.tools import docker

        asked: list[str] = []
        monkeypatch.setattr(
            docker,
            "get_container_status",
            lambda name: asked.append(name) or {"status": "running"},
        )

        client.get("/api/health/deployment/dep-1")

        assert asked == ["vllm-node"]

    def test_a_deployment_with_no_container_yet_is_unknown_not_an_error(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            tools.deployment_records, "get", lambda _id: {"id": "dep-1"}
        )

        body = client.get("/api/health/deployment/dep-1").json()

        assert body["container_status"] == "unknown"
        assert body["process_status"] == "dead"

    def test_a_container_docker_has_never_heard_of_is_unknown(
        self, client, deployment, monkeypatch
    ):
        from spark_pulse.tools import docker

        monkeypatch.setattr(docker, "get_container_status", lambda name: None)

        assert (
            client.get("/api/health/deployment/dep-1").json()["container_status"]
            == "unknown"
        )

    def test_an_unknown_deployment_says_so(self, client, deployment):
        body = client.get("/api/health/deployment/nope").json()

        assert body == {
            "deployment_id": "nope",
            "container_status": "not_found",
            "error": "Deployment not found",
        }

    def test_a_daemon_that_will_not_answer_is_reported_as_an_error(
        self, client, deployment, monkeypatch
    ):
        from spark_pulse.tools import docker

        def boom(_name):
            raise RuntimeError("docker is down")

        monkeypatch.setattr(docker, "get_container_status", boom)

        body = client.get("/api/health/deployment/dep-1").json()

        assert body["container_status"] == "error"
        assert body["error"] == "docker is down"


# ── The monitor ──────────────────────────────────────────────────────────────


class TestMonitorLifecycle:
    def test_starting_the_monitor_starts_it(self, client):
        assert client.post("/api/health/monitor/start").json() == {
            "started": True,
            "message": "Health monitor started",
        }
        assert mock_health.get_health_monitor().running is True

    def test_stopping_the_monitor_stops_it(self, client):
        client.post("/api/health/monitor/start")

        assert client.post("/api/health/monitor/stop").json() == {
            "stopped": True,
            "message": "Health monitor stopped",
        }
        # Stopping drops the singleton; the next one is fresh and idle.
        assert mock_health.get_health_monitor().running is False

    def test_a_monitor_that_will_not_start_reports_why(self, client, monkeypatch):
        def boom():
            raise RuntimeError("no thread available")

        monkeypatch.setattr(mock_health, "start_health_monitor", boom)

        assert client.post("/api/health/monitor/start").json() == {
            "started": False,
            "error": "no thread available",
        }

    def test_a_monitor_that_will_not_stop_reports_why(self, client, monkeypatch):
        def boom():
            raise RuntimeError("thread is wedged")

        monkeypatch.setattr(mock_health, "stop_health_monitor", boom)

        assert client.post("/api/health/monitor/stop").json() == {
            "stopped": False,
            "error": "thread is wedged",
        }


class TestTracking:
    def test_a_tracked_deployment_is_remembered(self, client):
        body = client.post(
            "/api/health/monitor/track/deployment",
            json={"deployment_id": "dep-1", "info": {"container_name": "vllm-node"}},
        ).json()

        assert body == {"tracked": True, "deployment_id": "dep-1"}
        assert mock_health.get_health_monitor().tracked == {
            "dep-1": {"type": "deployment", "info": {"container_name": "vllm-node"}}
        }

    def test_tracking_is_persisted_the_way_a_restart_reads_it_back(self, client):
        client.post(
            "/api/health/monitor/track/deployment",
            json={"deployment_id": "dep-1", "info": {"container_name": "vllm-node"}},
        )

        assert mock_health.load_health_tracking() == {
            "deployments": [
                {
                    "id": "dep-1",
                    "type": "deployment",
                    "info": {"container_name": "vllm-node"},
                }
            ]
        }
        assert mock_health.HealthMonitor.restore_from_persistence() == (
            mock_health.load_health_tracking()
        )

    def test_untracking_forgets_it_again(self, client):
        client.post(
            "/api/health/monitor/track/deployment",
            json={"deployment_id": "dep-1", "info": {}},
        )

        body = client.post(
            "/api/health/monitor/untrack", json={"identifier": "dep-1"}
        ).json()

        assert body == {"untracked": True, "identifier": "dep-1"}
        assert mock_health.get_health_monitor().tracked == {}
        assert mock_health.load_health_tracking() == {"deployments": []}

    def test_untracking_something_never_tracked_is_not_an_error(self, client):
        body = client.post(
            "/api/health/monitor/untrack", json={"identifier": "nope"}
        ).json()

        assert body == {"untracked": True, "identifier": "nope"}

    def test_a_monitor_that_will_not_track_reports_why(self, client, monkeypatch):
        def boom():
            raise RuntimeError("monitor unavailable")

        monkeypatch.setattr(mock_health, "get_health_monitor", boom)

        assert client.post(
            "/api/health/monitor/track/deployment", json={"deployment_id": "d"}
        ).json() == {"tracked": False, "error": "monitor unavailable"}
        assert client.post(
            "/api/health/monitor/untrack", json={"identifier": "d"}
        ).json() == {"untracked": False, "error": "monitor unavailable"}


# ── The simulated monitor itself ─────────────────────────────────────────────


class TestMockHealthModule:
    def test_it_exposes_every_public_name_the_real_module_does(self):
        # See tests/conftest.py: reaching the real submodule takes sys.modules,
        # because the tools package attribute is the mock under SIMULATION_MODE.
        import sys

        import spark_pulse.tools.health  # noqa: F401

        real = sys.modules["spark_pulse.tools.health"]
        expected = {
            name
            for name, value in vars(real).items()
            if not name.startswith("_")
            and getattr(value, "__module__", None) == real.__name__
        }

        assert expected == {
            "DeploymentHealth",
            "HealthMonitor",
            "get_health_monitor",
            "start_health_monitor",
            "stop_health_monitor",
            "load_health_tracking",
            "save_health_tracking",
        }
        assert {n for n in expected if not hasattr(mock_health, n)} == set()

    def test_the_monitor_is_one_per_process_until_it_is_stopped(self):
        first = mock_health.get_health_monitor()

        assert mock_health.get_health_monitor() is first

        mock_health.stop_health_monitor()

        assert mock_health.get_health_monitor() is not first

    def test_stopping_a_monitor_that_was_never_started_is_harmless(self):
        mock_health.stop_health_monitor()

        assert mock_health.load_health_tracking() == {"deployments": []}

    def test_it_takes_the_real_monitors_arguments(self):
        monitor = mock_health.HealthMonitor(check_interval=5.0, sse_broadcast=object())

        assert monitor.running is False

    @pytest.mark.parametrize(
        ("scenario", "container_status", "process_status", "has_error"),
        [
            ("healthy", "running", "alive", False),
            ("degraded", "running", "alive", True),
            ("critical", "error", "dead", True),
        ],
    )
    def test_the_scenario_decides_what_a_check_reports(
        self, scenario, container_status, process_status, has_error
    ):
        health = mock_health.HealthMonitor(scenario=scenario).check_deployment(
            "dep-1", {}
        )

        assert health.deployment_id == "dep-1"
        assert health.container_status == container_status
        assert health.process_status == process_status
        assert bool(health.error) is has_error
        assert health.checked_at

    def test_the_convenience_check_reports_a_healthy_deployment(self):
        assert mock_health.mock_check_deployment("dep-1").process_status == "alive"

    def test_the_tracked_map_is_a_copy_a_caller_cannot_corrupt(self):
        monitor = mock_health.get_health_monitor()
        monitor.track_deployment("dep-1", {})

        monitor.tracked.clear()

        assert list(monitor.tracked) == ["dep-1"]

    def test_only_deployments_are_persisted(self):
        monitor = mock_health.get_health_monitor()
        monitor.track_deployment("dep-1", {})
        monitor._tracked["cluster-1"] = {"type": "cluster", "info": {}}
        monitor.untrack("nothing")  # forces a re-persist

        assert [d["id"] for d in mock_health.load_health_tracking()["deployments"]] == [
            "dep-1"
        ]
