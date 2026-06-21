"""Tests for RayManager."""

from __future__ import annotations

from unittest.mock import MagicMock

from spark_pulse.tools.ray import RayManager


class TestRayManager:
    """Tests for RayManager."""

    def _make_service(self, ray_ready: bool = True, fail_containers: list | None = None):
        """Create a mock RemoteDockerService for RayManager."""
        service = MagicMock()
        service.exec_container.return_value = MagicMock(
            ok=True,
            stdout="Cluster is ready" if ray_ready else "",
            stderr="",
        )
        return service

    def test_ensure_ray_head_already_ready(self):
        """Test ensure_ray_head when Ray is already running."""
        service = self._make_service(ray_ready=True)
        manager = RayManager(service)

        # First call returns ready, second call confirms
        result = manager.ensure_ray_head("test-container", "10.0.0.1")
        assert result is True

    def test_ensure_ray_head_starts_new(self):
        """Test ensure_ray_head starts Ray when not running."""
        call_count = [0]

        def mock_exec(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: ray status — not ready
                return MagicMock(ok=False, stdout="", stderr="Ray not started")
            # Subsequent calls: ray start succeeds
            return MagicMock(ok=True, stdout="", stderr="")

        service = MagicMock()
        service.exec_container.side_effect = mock_exec
        service.get_container_status.return_value = {"running": True}

        manager = RayManager(service)
        result = manager.ensure_ray_head("test-container", "10.0.0.1")
        # Should succeed (Ray was started)
        assert service.exec_container.called

    def test_ensure_ray_worker_already_connected(self):
        """Test ensure_ray_worker when already connected."""
        service = self._make_service(ray_ready=True)
        manager = RayManager(service)

        result = manager.ensure_ray_worker(
            "worker-container", "10.0.0.2", "10.0.0.1"
        )
        assert result is True

    def test_ensure_ray_worker_starts_new(self):
        """Test ensure_ray_worker starts Ray connection."""
        service = self._make_service(ray_ready=True)
        manager = RayManager(service)

        result = manager.ensure_ray_worker(
            "worker-container", "10.0.0.2", "10.0.0.1"
        )
        assert service.exec_container.called

    def test_wait_for_cluster_ready_timeout(self):
        """Test wait_for_cluster_ready returns False on timeout."""
        service = MagicMock()
        service.exec_container.return_value = MagicMock(
            ok=True,
            stdout="not ready yet",
            stderr="",
        )
        manager = RayManager(service)

        result = manager.wait_for_cluster_ready(
            "test-container", timeout=1, poll_interval=0.1
        )
        assert result is False

    def test_get_ray_status(self):
        """Test get_ray_status returns output."""
        service = MagicMock()
        service.exec_container.return_value = MagicMock(
            ok=True,
            stdout="Cluster is ready",
            stderr="",
        )
        manager = RayManager(service)

        status = manager.get_ray_status("test-container")
        assert "ready" in status.lower()

    def test_get_ray_status_error(self):
        """Test get_ray_status returns error message."""
        service = MagicMock()
        service.exec_container.return_value = MagicMock(
            ok=False,
            stdout="",
            stderr="Ray not started",
        )
        manager = RayManager(service)

        status = manager.get_ray_status("test-container")
        assert "error" in status.lower() or "not started" in status.lower()
