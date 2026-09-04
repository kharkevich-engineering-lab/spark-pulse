"""Tests for cluster health validation."""

from __future__ import annotations

from unittest.mock import MagicMock

from spark_pulse.tools.cluster_health import (
    ValidationResult,
    validate_cluster,
)
from spark_pulse.tools.cluster_models import ClusterNode, ClusterState


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_ok(self):
        result = ValidationResult.ok()
        assert result.healthy is True
        assert result.warnings == []
        assert result.errors == []

    def test_with_errors(self):
        result = ValidationResult.with_errors(["error1", "error2"])
        assert result.healthy is False
        assert result.errors == ["error1", "error2"]

    def test_with_warnings(self):
        result = ValidationResult.with_warnings(["warning1"])
        assert result.healthy is True
        assert result.warnings == ["warning1"]

    def test_add_error(self):
        result = ValidationResult.ok().add_error("new error")
        assert result.healthy is False
        assert result.errors == ["new error"]

    def test_add_warning(self):
        result = ValidationResult.ok().add_warning("new warning")
        assert result.healthy is True
        assert result.warnings == ["new warning"]

    def test_add_error_to_unhealthy(self):
        result = ValidationResult.with_errors(["existing"]).add_error("new")
        assert result.errors == ["existing", "new"]

    def test_frozen_dataclass(self):
        result = ValidationResult.ok()
        try:
            result.healthy = False
            assert False, "Should not modify frozen dataclass"
        except Exception:
            pass  # Expected


class TestValidateCluster:
    """Tests for validate_cluster function."""

    def _make_state(self, head_status="running", worker_statuses=None):
        """Create a test ClusterState."""
        head = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="cluster-head",
            status=head_status,
            gpu_count=8,
        )
        workers = []
        if worker_statuses:
            for i, status in enumerate(worker_statuses):
                workers.append(
                    ClusterNode(
                        ip=f"10.0.0.{i + 2}",
                        role="worker",
                        container_name=f"cluster-worker-{i}",
                        status=status,
                        gpu_count=8,
                    )
                )
        return ClusterState(name="test", head=head, workers=workers)

    def test_all_nodes_running(self):
        state = self._make_state(head_status="running", worker_statuses=["running"])
        mock_docker = MagicMock()
        mock_docker.get_container_status.return_value = {"running": True}
        mock_docker.exec_container.return_value = MagicMock(
            ok=True, stdout="OK", stderr=""
        )

        result = validate_cluster(state, mock_docker)
        assert result.healthy is True

    def test_head_not_running(self):
        state = self._make_state(head_status="stopped")
        mock_docker = MagicMock()
        mock_docker.get_container_status.return_value = {"running": False}

        result = validate_cluster(state, mock_docker)
        assert any("not running" in e for e in result.errors)

    def test_worker_not_running(self):
        state = self._make_state(head_status="running", worker_statuses=["stopped"])
        mock_docker = MagicMock()
        mock_docker.get_container_status.side_effect = [
            {"running": True},  # head
            {"running": False},  # worker
        ]

        result = validate_cluster(state, mock_docker)
        assert any("not running" in e for e in result.errors)

    def test_ray_not_healthy(self):
        state = self._make_state(head_status="running")
        mock_docker = MagicMock()
        mock_docker.get_container_status.return_value = {"running": True}
        mock_docker.exec_container.return_value = MagicMock(
            ok=True, stdout="Ray not ready", stderr=""
        )

        result = validate_cluster(state, mock_docker)
        assert any("Ray" in e for e in result.errors)

    def test_gpu_check_warning(self):
        state = self._make_state(head_status="running")
        mock_docker = MagicMock()
        mock_docker.get_container_status.return_value = {"running": True}
        # ray status succeeds, nvidia-smi fails, env succeeds
        mock_docker.exec_container.side_effect = [
            MagicMock(ok=True, stdout="OK", stderr=""),  # ray status on head
            MagicMock(
                ok=False, stdout="", stderr="nvidia-smi not found"
            ),  # nvidia-smi on head
            MagicMock(
                ok=True, stdout="NCCL_SOCKET_IFNAME=eth0", stderr=""
            ),  # env on head
        ]

        result = validate_cluster(state, mock_docker)
        assert any("GPU" in w for w in result.warnings)

    def test_nccl_inconsistency_warning(self):
        state = self._make_state(head_status="running", worker_statuses=["running"])
        mock_docker = MagicMock()
        mock_docker.get_container_status.return_value = {"running": True}
        # Return different NCCL values for head and worker
        # Order: ray head, ray worker, nvidia-smi head, nvidia-smi worker, env head, env worker
        mock_docker.exec_container.side_effect = [
            MagicMock(ok=True, stdout="OK", stderr=""),  # ray status on head
            MagicMock(ok=True, stdout="OK", stderr=""),  # ray status on worker
            MagicMock(ok=True, stdout="1", stderr=""),  # nvidia-smi on head
            MagicMock(ok=True, stdout="1", stderr=""),  # nvidia-smi on worker
            MagicMock(
                ok=True, stdout="NCCL_SOCKET_IFNAME=eth0", stderr=""
            ),  # env on head
            MagicMock(
                ok=True, stdout="NCCL_SOCKET_IFNAME=ib0", stderr=""
            ),  # env on worker
        ]

        result = validate_cluster(state, mock_docker)
        # Should have warnings about NCCL inconsistency
        assert any("NCCL" in w for w in result.warnings)
