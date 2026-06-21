"""Tests for health monitoring tool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from spark_pulse.tools.health import (
    ClusterHealth,
    DeploymentHealth,
    HealthMonitor,
    _retry_with_backoff,
    get_health_monitor,
    start_health_monitor,
    stop_health_monitor,
)


class TestDeploymentHealth:
    """Tests for DeploymentHealth dataclass."""

    def test_default_values(self):
        health = DeploymentHealth(deployment_id="test-123")
        assert health.deployment_id == "test-123"
        assert health.container_status == "unknown"
        assert health.ray_status == "unknown"
        assert health.process_status == "unknown"
        assert health.error is None

    def test_with_values(self):
        health = DeploymentHealth(
            deployment_id="test-123",
            container_status="running",
            ray_status="ready",
            process_status="alive",
            error=None,
        )
        assert health.container_status == "running"
        assert health.ray_status == "ready"
        assert health.process_status == "alive"


class TestClusterHealth:
    """Tests for ClusterHealth dataclass."""

    def test_default_values(self):
        health = ClusterHealth(cluster_name="test-cluster")
        assert health.cluster_name == "test-cluster"
        assert health.healthy is False
        assert health.head_status == "unknown"
        assert health.worker_statuses == []
        assert health.ray_ready is False
        assert health.warnings == []
        assert health.errors == []

    def test_with_values(self):
        health = ClusterHealth(
            cluster_name="test-cluster",
            healthy=True,
            head_status="running",
            worker_statuses=["running", "running"],
            ray_ready=True,
        )
        assert health.healthy is True
        assert health.head_status == "running"
        assert health.worker_statuses == ["running", "running"]
        assert health.ray_ready is True


class TestRetryWithBackoff:
    """Tests for _retry_with_backoff function."""

    def test_success_on_first_try(self):
        call_count = 0

        def success_func():
            nonlocal call_count
            call_count += 1
            return "success"

        result = _retry_with_backoff(success_func)
        assert result == "success"
        assert call_count == 1

    def test_success_after_retries(self):
        call_count = 0

        def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError(f"Attempt {call_count} failed")
            return "success"

        result = _retry_with_backoff(flaky_func, max_retries=3)
        assert result == "success"
        assert call_count == 3

    def test_all_retries_fail(self):
        call_count = 0

        def failing_func():
            nonlocal call_count
            call_count += 1
            raise ValueError(f"Attempt {call_count} failed")

        try:
            _retry_with_backoff(failing_func, max_retries=3)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "Attempt 3 failed" in str(e)
        assert call_count == 3


class TestHealthMonitor:
    """Tests for HealthMonitor class."""

    def test_create_monitor(self):
        monitor = HealthMonitor()
        assert monitor._running is False
        assert monitor._thread is None

    def test_start_stop(self):
        monitor = HealthMonitor(check_interval=0.1)
        monitor.start()
        assert monitor._running is True
        assert monitor._thread is not None
        monitor.stop()
        assert monitor._running is False

    def test_track_deployment(self):
        monitor = HealthMonitor()
        monitor.track_deployment("dep-123", {"container_name": "test"})
        assert "dep-123" in monitor._tracked
        assert monitor._tracked["dep-123"]["type"] == "deployment"

    def test_track_cluster(self):
        monitor = HealthMonitor()
        monitor.track_cluster("cluster-123", {"name": "test"})
        assert "cluster-123" in monitor._tracked
        assert monitor._tracked["cluster-123"]["type"] == "cluster"

    def test_untrack(self):
        monitor = HealthMonitor()
        monitor.track_deployment("dep-123", {})
        monitor.untrack("dep-123")
        assert "dep-123" not in monitor._tracked

    def test_untrack_nonexistent(self):
        monitor = HealthMonitor()
        monitor.untrack("nonexistent")  # Should not raise

    def test_check_interval(self):
        monitor = HealthMonitor(check_interval=5.0)
        assert monitor._check_interval == 5.0


class TestHealthMonitorSingleton:
    """Tests for health monitor singleton functions."""

    def test_get_health_monitor_creates_new(self):
        # Ensure we get a fresh monitor
        stop_health_monitor()
        monitor1 = get_health_monitor()
        assert monitor1 is not None

    def test_get_health_monitor_returns_same(self):
        stop_health_monitor()
        monitor1 = get_health_monitor()
        monitor2 = get_health_monitor()
        assert monitor1 is monitor2

    def test_start_health_monitor(self):
        stop_health_monitor()
        monitor = start_health_monitor()
        assert monitor._running is True
        stop_health_monitor()

    def test_stop_health_monitor(self):
        stop_health_monitor()  # Ensure clean state
        monitor = start_health_monitor()
        stop_health_monitor()
        assert monitor._running is False
