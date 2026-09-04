"""Tests for cluster state data models."""

from __future__ import annotations


from spark_pulse.tools.cluster_models import ClusterNode, ClusterState


class TestClusterNode:
    """Tests for ClusterNode dataclass."""

    def test_create_head_node(self):
        node = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="cluster-head",
        )
        assert node.ip == "10.0.0.1"
        assert node.role == "head"
        assert node.status == "starting"
        assert not node.ray_ready
        assert node.gpu_count == 0

    def test_create_worker_node(self):
        node = ClusterNode(
            ip="10.0.0.2",
            role="worker",
            container_name="cluster-worker-0",
            status="running",
            ray_ready=True,
            gpu_count=8,
        )
        assert node.ip == "10.0.0.2"
        assert node.role == "worker"
        assert node.status == "running"
        assert node.ray_ready
        assert node.gpu_count == 8

    def test_is_running_running(self):
        node = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="test-head",
            status="running",
        )
        assert node.is_running is True

    def test_is_running_stopped(self):
        node = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="test-head",
            status="stopped",
        )
        assert node.is_running is False

    def test_is_running_error(self):
        node = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="test-head",
            status="error",
        )
        assert node.is_running is False

    def test_is_healthy_running_with_ray(self):
        node = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="test-head",
            status="running",
            ray_ready=True,
        )
        assert node.is_healthy is True

    def test_is_healthy_running_without_ray(self):
        node = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="test-head",
            status="running",
            ray_ready=False,
        )
        assert node.is_healthy is False

    def test_is_healthy_not_running(self):
        node = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="test-head",
            status="stopped",
            ray_ready=True,
        )
        assert node.is_healthy is False

    def test_frozen_dataclass(self):
        node = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="test-head",
        )
        try:
            node.ip = "10.0.0.2"
            assert False, "Should not be able to modify frozen dataclass"
        except Exception:
            pass  # Expected

    def test_container_id_none_by_default(self):
        node = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="test-head",
        )
        assert node.container_id is None


class TestClusterState:
    """Tests for ClusterState dataclass."""

    def test_create_cluster_state(self):
        head = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="cluster-head",
            status="running",
            gpu_count=8,
        )
        workers = [
            ClusterNode(
                ip="10.0.0.2",
                role="worker",
                container_name="cluster-worker-0",
                status="running",
                gpu_count=8,
            ),
        ]
        state = ClusterState(
            name="test-cluster",
            head=head,
            workers=workers,
            ray_enabled=True,
            ray_ready=True,
        )

        assert state.name == "test-cluster"
        assert state.head.ip == "10.0.0.1"
        assert len(state.workers) == 1
        assert state.ray_enabled is True
        assert state.ray_ready is True

    def test_healthy_all_running(self):
        head = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="cluster-head",
            status="running",
            ray_ready=True,
            gpu_count=8,
        )
        workers = [
            ClusterNode(
                ip="10.0.0.2",
                role="worker",
                container_name="cluster-worker-0",
                status="running",
                ray_ready=True,
                gpu_count=8,
            ),
        ]
        state = ClusterState(name="test", head=head, workers=workers)
        assert state.healthy is True

    def test_healthy_head_not_running(self):
        head = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="cluster-head",
            status="stopped",
            gpu_count=8,
        )
        state = ClusterState(name="test", head=head)
        assert state.healthy is False

    def test_healthy_worker_not_running(self):
        head = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="cluster-head",
            status="running",
            ray_ready=True,
            gpu_count=8,
        )
        workers = [
            ClusterNode(
                ip="10.0.0.2",
                role="worker",
                container_name="cluster-worker-0",
                status="stopped",
                gpu_count=8,
            ),
        ]
        state = ClusterState(name="test", head=head, workers=workers)
        assert state.healthy is False

    def test_total_nodes(self):
        head = ClusterNode(
            ip="10.0.0.1",
            role="head",
            container_name="cluster-head",
            gpu_count=8,
        )
        workers = [
            ClusterNode(ip="10.0.0.2", role="worker", container_name="w0", gpu_count=8),
            ClusterNode(ip="10.0.0.3", role="worker", container_name="w1", gpu_count=8),
        ]
        state = ClusterState(name="test", head=head, workers=workers)
        assert state.total_nodes == 3

    def test_total_gpus(self):
        head = ClusterNode(ip="10.0.0.1", role="head", container_name="h", gpu_count=8)
        workers = [
            ClusterNode(ip="10.0.0.2", role="worker", container_name="w0", gpu_count=8),
            ClusterNode(ip="10.0.0.3", role="worker", container_name="w1", gpu_count=4),
        ]
        state = ClusterState(name="test", head=head, workers=workers)
        assert state.total_gpus == 20

    def test_node_by_ip_head(self):
        head = ClusterNode(ip="10.0.0.1", role="head", container_name="h")
        state = ClusterState(name="test", head=head)
        assert state.node_by_ip("10.0.0.1") is head

    def test_node_by_ip_worker(self):
        head = ClusterNode(ip="10.0.0.1", role="head", container_name="h")
        worker = ClusterNode(ip="10.0.0.2", role="worker", container_name="w0")
        state = ClusterState(name="test", head=head, workers=[worker])
        assert state.node_by_ip("10.0.0.2") is worker

    def test_node_by_ip_not_found(self):
        head = ClusterNode(ip="10.0.0.1", role="head", container_name="h")
        state = ClusterState(name="test", head=head)
        assert state.node_by_ip("10.0.0.99") is None

    def test_worker_containers(self):
        head = ClusterNode(ip="10.0.0.1", role="head", container_name="h")
        workers = [
            ClusterNode(ip="10.0.0.2", role="worker", container_name="w0"),
            ClusterNode(ip="10.0.0.3", role="worker", container_name="w1"),
        ]
        state = ClusterState(name="test", head=head, workers=workers)
        assert state.worker_containers() == ["w0", "w1"]

    def test_all_containers(self):
        head = ClusterNode(ip="10.0.0.1", role="head", container_name="h")
        workers = [
            ClusterNode(ip="10.0.0.2", role="worker", container_name="w0"),
        ]
        state = ClusterState(name="test", head=head, workers=workers)
        assert state.all_containers() == ["h", "w0"]

    def test_is_running_all_running(self):
        head = ClusterNode(
            ip="10.0.0.1", role="head", container_name="h", status="running"
        )
        workers = [
            ClusterNode(
                ip="10.0.0.2", role="worker", container_name="w0", status="running"
            ),
        ]
        state = ClusterState(name="test", head=head, workers=workers)
        assert state.is_running is True

    def test_is_running_one_stopped(self):
        head = ClusterNode(
            ip="10.0.0.1", role="head", container_name="h", status="running"
        )
        workers = [
            ClusterNode(
                ip="10.0.0.2", role="worker", container_name="w0", status="stopped"
            ),
        ]
        state = ClusterState(name="test", head=head, workers=workers)
        assert state.is_running is False
