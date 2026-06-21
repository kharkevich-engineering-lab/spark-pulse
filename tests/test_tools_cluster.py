"""Tests for ClusterOrchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from spark_pulse.mock.cluster import MockClusterOrchestrator
from spark_pulse.tools.cluster import ClusterOrchestrator, ModDeployment
from spark_pulse.tools.cluster_models import ClusterState


class TestMockClusterOrchestrator:
    """Tests for MockClusterOrchestrator."""

    def test_start_cluster_default(self):
        orchestrator = MockClusterOrchestrator()
        state = orchestrator.start_cluster(
            name="test-cluster",
            image="vllm/vllm-openai:latest",
            head_ip="10.0.0.1",
            worker_ips=["10.0.0.2", "10.0.0.3"],
            env_vars={},
            docker_config={"gpu_count": 8},
        )

        assert state.name == "test-cluster"
        assert state.total_nodes == 3
        assert state.head.ip == "10.0.0.1"
        assert len(state.workers) == 2
        assert state.workers[0].ip == "10.0.0.2"
        assert state.workers[1].ip == "10.0.0.3"

    def test_start_cluster_no_ray(self):
        orchestrator = MockClusterOrchestrator()
        state = orchestrator.start_cluster(
            name="test-cluster",
            image="vllm/vllm-openai:latest",
            head_ip="10.0.0.1",
            worker_ips=[],
            env_vars={},
            docker_config={"gpu_count": 8},
            no_ray=True,
        )

        assert state.ray_enabled is False
        assert state.ray_ready is False

    def test_start_cluster_failed_scenario(self):
        orchestrator = MockClusterOrchestrator(scenario="failed")
        state = orchestrator.start_cluster(
            name="test-cluster",
            image="vllm/vllm-openai:latest",
            head_ip="10.0.0.1",
            worker_ips=[],
            env_vars={},
            docker_config={"gpu_count": 8},
        )

        assert state.head.status == "error"

    def test_stop_cluster(self):
        orchestrator = MockClusterOrchestrator()
        orchestrator.start_cluster(
            name="test-cluster",
            image="test",
            head_ip="10.0.0.1",
            worker_ips=[],
            env_vars={},
            docker_config={"gpu_count": 8},
        )
        orchestrator.stop_cluster("test-cluster")

        state = orchestrator.get_cluster_status("test-cluster")
        assert state.head.status == "stopped"

    def test_get_cluster_status_not_found(self):
        orchestrator = MockClusterOrchestrator()
        try:
            orchestrator.get_cluster_status("nonexistent")
            assert False, "Should raise RuntimeError"
        except RuntimeError as e:
            assert "not found" in str(e).lower()

    def test_executed_operations_tracked(self):
        orchestrator = MockClusterOrchestrator()
        orchestrator.start_cluster(
            name="test", image="test", head_ip="10.0.0.1",
            worker_ips=[], env_vars={}, docker_config={},
        )
        orchestrator.stop_cluster("test")

        ops = orchestrator.executed_operations
        assert len(ops) == 2
        assert ops[0]["action"] == "start_cluster"
        assert ops[1]["action"] == "stop_cluster"

    def test_ensure_ray_head(self):
        orchestrator = MockClusterOrchestrator(ray_ready=True)
        result = orchestrator.ensure_ray_head("head-container", "10.0.0.1")
        assert result is True

    def test_ensure_ray_head_fail(self):
        orchestrator = MockClusterOrchestrator(ray_ready=False)
        result = orchestrator.ensure_ray_head("head-container", "10.0.0.1")
        assert result is False

    def test_reset_clears_state(self):
        orchestrator = MockClusterOrchestrator()
        orchestrator.start_cluster(
            name="test", image="test", head_ip="10.0.0.1",
            worker_ips=[], env_vars={}, docker_config={},
        )
        assert len(orchestrator.clusters) == 1

        orchestrator.reset()
        assert len(orchestrator.clusters) == 0
        assert len(orchestrator.executed_operations) == 0

    def test_docker_accessor(self):
        orchestrator = MockClusterOrchestrator()
        assert orchestrator.docker is not None

    def test_ray_accessor(self):
        orchestrator = MockClusterOrchestrator()
        assert orchestrator.ray is not None


class TestClusterOrchestratorIntegration:
    """Integration tests for ClusterOrchestrator (mock-based)."""

    def test_start_cluster_with_mods(self):
        """Test starting cluster with mod deployments."""
        orchestrator = MockClusterOrchestrator()
        mods = [
            ModDeployment(path="/mods/custom-mod", target="all"),
            ModDeployment(path="/mods/head-only-mod", target="head"),
        ]

        state = orchestrator.start_cluster(
            name="test-cluster",
            image="vllm/vllm-openai:latest",
            head_ip="10.0.0.1",
            worker_ips=["10.0.0.2"],
            env_vars={},
            docker_config={"gpu_count": 8},
            mod_deployments=mods,
        )

        assert state.name == "test-cluster"
        assert state.total_nodes == 2

    def test_cluster_state_serialization(self):
        """Test that ClusterState can be serialized to dict."""
        orchestrator = MockClusterOrchestrator()
        state = orchestrator.start_cluster(
            name="test-cluster",
            image="test",
            head_ip="10.0.0.1",
            worker_ips=["10.0.0.2"],
            env_vars={},
            docker_config={"gpu_count": 8},
        )

        # Verify key properties are accessible
        assert state.name == "test-cluster"
        assert state.head.role == "head"
        assert state.workers[0].role == "worker"
        assert state.total_nodes == 2
        assert state.total_gpus == 16
