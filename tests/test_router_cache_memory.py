"""Tests for the /api/cache and /api/memory routers.

Both are thin, both were untested, and both are what the Monitoring and Cache
pages poll. The interesting parts are the shapes they wrap around the tools
(`{"entries": ...}`, `{"gpus": ...}`, `{"disks": ...}`) and the two failure
codes the kill endpoint turns a tool result into.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spark_pulse import tools
from spark_pulse.app import create_app
from spark_pulse.mock import system as mock_system


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


# ── Cache ────────────────────────────────────────────────────────────────────


class TestCacheRouter:
    def test_the_listing_is_wrapped_in_entries(self, client, monkeypatch):
        entry = {"name": "HF Model Cache", "path": "/c/hf", "size_bytes": 7}
        monkeypatch.setattr(tools.cache, "list_cache", lambda: [entry])

        assert client.get("/api/cache").json() == {"entries": [entry]}

    def test_cleaning_reports_a_result_per_target(self, client, monkeypatch):
        asked: list[list[str]] = []
        monkeypatch.setattr(
            tools.cache,
            "clean_cache",
            lambda targets: asked.append(targets) or {t: "Cleaned" for t in targets},
        )

        response = client.post("/api/cache/clean", json={"targets": ["CCache"]})

        assert response.json() == {"results": {"CCache": "Cleaned"}}
        assert asked == [["CCache"]]

    def test_cleaning_nothing_deletes_nothing(self, client, monkeypatch):
        monkeypatch.setattr(
            tools.cache,
            "clean_cache",
            lambda targets: pytest.fail("must not clean without a target"),
        )

        assert client.post("/api/cache/clean", json={}).json() == {
            "error": "No targets specified"
        }
        assert client.post("/api/cache/clean", json={"targets": []}).json() == {
            "error": "No targets specified"
        }


# ── Memory ───────────────────────────────────────────────────────────────────


class TestMemoryRouter:
    def test_gpu_stats_are_wrapped_in_gpus(self, client, monkeypatch):
        gpu = {"index": 0, "name": "NVIDIA GB10", "memory_total": 131072}
        monkeypatch.setattr(tools.system, "get_gpu_stats", lambda: [gpu])

        assert client.get("/api/memory/gpu").json() == {"gpus": [gpu]}

    def test_cpu_stats_are_returned_as_they_come(self, client, monkeypatch):
        stats = {"total": 131072, "used": 43520, "usage_percent": 33.2}
        monkeypatch.setattr(tools.system, "get_cpu_stats", lambda: stats)

        assert client.get("/api/memory/cpu").json() == stats

    def test_disk_stats_are_wrapped_in_disks(self, client, monkeypatch):
        disk = {"mount": "/", "total": 1, "used": 1, "free": 0, "usage_percent": 100.0}
        monkeypatch.setattr(tools.system, "get_disk_stats", lambda: [disk])

        assert client.get("/api/memory/disk").json() == {"disks": [disk]}

    def test_processes_belonging_to_a_running_deployment_are_marked_tracked(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            tools.system,
            "get_all_memory",
            lambda: {"processes": [{"pid": 4242}, {"pid": 99}]},
        )
        monkeypatch.setattr(
            tools.deployment_records,
            "load",
            lambda: [
                {"id": "a", "status": "running", "pid": 4242},
                {"id": "b", "status": "stopped", "pid": 99},
            ],
        )

        processes = client.get("/api/memory").json()["processes"]

        assert [p["is_tracked"] for p in processes] == [True, False]

    def test_a_pending_deployment_counts_as_running_for_tracking(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            tools.system, "get_all_memory", lambda: {"processes": [{"pid": 7}]}
        )
        monkeypatch.setattr(
            tools.deployment_records,
            "load",
            lambda: [{"id": "a", "status": "pending", "pid": 7}],
        )

        assert client.get("/api/memory").json()["processes"][0]["is_tracked"] is True

    def test_a_deployment_with_no_pid_yet_marks_nothing(self, client, monkeypatch):
        """A record carries ``pid: None`` until its container reports one."""
        monkeypatch.setattr(
            tools.system, "get_all_memory", lambda: {"processes": [{"pid": 7}]}
        )
        monkeypatch.setattr(
            tools.deployment_records,
            "load",
            lambda: [{"id": "a", "status": "running", "pid": None}, {"id": "b"}],
        )

        assert client.get("/api/memory").json()["processes"][0]["is_tracked"] is False


class TestKillGpuProcess:
    def test_a_killed_process_reports_the_tools_result(self, client, monkeypatch):
        monkeypatch.setattr(
            tools.system, "kill_gpu_process", lambda pid: {"killed": True, "pid": pid}
        )

        assert client.delete("/api/memory/processes/4242").json() == {
            "killed": True,
            "pid": 4242,
        }

    def test_a_process_that_is_gone_is_a_404(self, client, monkeypatch):
        monkeypatch.setattr(
            tools.system,
            "kill_gpu_process",
            lambda pid: {"killed": False, "error": "Process not found"},
        )

        response = client.delete("/api/memory/processes/4242")

        assert response.status_code == 404
        assert response.json()["detail"] == "Process 4242 not found"

    def test_a_process_this_user_may_not_kill_is_a_403(self, client, monkeypatch):
        monkeypatch.setattr(
            tools.system,
            "kill_gpu_process",
            lambda pid: {"killed": False, "error": "Permission denied"},
        )

        response = client.delete("/api/memory/processes/1")

        assert response.status_code == 403
        assert response.json()["detail"] == "Permission denied to kill process 1"

    def test_any_other_failure_is_reported_rather_than_raised(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            tools.system,
            "kill_gpu_process",
            lambda pid: {"killed": False, "error": "docker stop failed"},
        )

        response = client.delete("/api/memory/processes/1")

        assert response.status_code == 200
        assert response.json() == {"killed": False, "error": "docker stop failed"}


# ── The simulated tracker ────────────────────────────────────────────────────


class TestMockEnrichGpuProcessTracking:
    """``mock/system.py`` marks processes without walking /proc.

    Addressed by module rather than through ``tools.system``: the switch's
    attribute is rebound to the real module by any test that imports it, and
    these assertions are about the simulation twin specifically.
    """

    def test_a_matching_pid_is_tracked(self):
        processes = [{"pid": 1}, {"pid": 2}]

        mock_system.enrich_gpu_process_tracking(
            processes, [{"id": "a", "pid": 2, "status": "running"}]
        )

        assert [p["is_tracked"] for p in processes] == [False, True]

    def test_a_record_without_a_pid_key_does_not_raise(self):
        processes = [{"pid": 1}]

        mock_system.enrich_gpu_process_tracking(processes, [{"id": "a"}])

        assert processes[0]["is_tracked"] is False

    def test_a_process_without_a_pid_is_untracked_rather_than_fatal(self):
        processes = [{"process_name": "orphan"}]

        mock_system.enrich_gpu_process_tracking(processes, [{"id": "a", "pid": None}])

        assert processes[0]["is_tracked"] is False
