"""Tests for ClusterOrchestrator.

``TestMockClusterOrchestrator`` exercises the simulation-mode stand-in, which
fabricates cluster state and reaches no container at all. The real
orchestrator had no coverage whatsoever, which is how thirteen empty-host
calls survived: the tests asserted against a mock that could not have noticed.
``TestRealClusterOrchestrator`` drives the production class against injected
node services and asserts *which node* every operation landed on.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from spark_pulse.mock.cluster import MockClusterOrchestrator
from spark_pulse.tools.cluster import ClusterOrchestrator, ModDeployment
from spark_pulse.tools.docker import ContainerInfo, ExecResult
from spark_pulse.tools.labels import CLUSTER_LABEL


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
            name="test",
            image="test",
            head_ip="10.0.0.1",
            worker_ips=[],
            env_vars={},
            docker_config={},
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
            name="test",
            image="test",
            head_ip="10.0.0.1",
            worker_ips=[],
            env_vars={},
            docker_config={},
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


HEAD_IP = "10.0.0.1"
WORKER_IP = "10.0.0.2"


class FakeNode:
    """One node's container service, recording what it was asked to do."""

    def __init__(self, address: str, log: list[tuple[str, str, tuple]]):
        self.address = address
        self._log = log
        self.containers: dict[str, ContainerInfo] = {}

    def _record(self, action: str, subject: str, *extra) -> None:
        self._log.append((self.address, action, (subject, *extra)))

    def run_container(self, image, name, env_vars, metadata, **kwargs):
        self._record("run", name)
        metadata.image = metadata.image or image
        labels = metadata.to_labels()
        info = ContainerInfo(
            id=f"id-{name}",
            name=name,
            status="running",
            image=image,
            metadata=metadata,
            labels=labels,
        )
        self.containers[name] = info
        return info

    def stop_container(self, name, timeout=30):
        self._record("stop", name)
        return self.containers.pop(name, None) is not None

    def list_managed_containers(self, labels=None):
        self._record("list", str(labels))
        wanted = labels or {}
        return [
            info
            for info in self.containers.values()
            if all(
                key in info.labels and (not value or info.labels[key] == value)
                for key, value in wanted.items()
            )
        ]

    def get_container_status(self, name):
        self._record("status", name)
        return {"running": name in self.containers}

    def exec_in_container(self, container, command, detach=False, timeout=None):
        self._record("exec", container, tuple(command))
        if command[:2] == ["ray", "status"]:
            return ExecResult(0, "Cluster is ready. OK")
        if command == ["env"]:
            return ExecResult(0, "NCCL_SOCKET_IFNAME=eth0")
        return ExecResult(0, "")


class FakeCluster:
    """A node resolver over a fixed set of fake nodes."""

    def __init__(self):
        self.log: list[tuple[str, str, tuple]] = []
        self.nodes: dict[str, FakeNode] = {}

    def __call__(self, node) -> FakeNode:
        address = node.address or "control"
        existing = self.nodes.get(address)
        if existing is None:
            existing = FakeNode(address, self.log)
            self.nodes[address] = existing
        return existing

    def actions_on(self, address: str) -> list[str]:
        """Every verb this node's daemon was asked for, in order."""
        return [action for node, action, _ in self.log if node == address]

    def subjects_on(self, address: str) -> list[str]:
        """Every container name this node's daemon was asked about."""
        return [args[0] for node, _, args in self.log if node == address]


@pytest.fixture
def cluster() -> FakeCluster:
    return FakeCluster()


@pytest.fixture
def orchestrator(cluster) -> ClusterOrchestrator:
    """The real orchestrator, with every node reachable and nothing real."""
    return ClusterOrchestrator(
        services=cluster,
        ssh_client=MagicMock(name="SSHClient"),
        event_broadcaster=MagicMock(name="EventBroadcaster"),
    )


def _start(orchestrator, **overrides):
    kwargs = {
        "name": "c",
        "image": "engine:1",
        "head_ip": HEAD_IP,
        "worker_ips": [WORKER_IP],
        "env_vars": {},
        "docker_config": {"gpu_count": 1},
        "no_ray": True,
    }
    kwargs.update(overrides)
    return orchestrator.start_cluster(**kwargs)


class TestRealClusterOrchestrator:
    """The production orchestrator, and the node each operation reaches."""

    def test_the_worker_container_is_started_on_the_worker(self, orchestrator, cluster):
        state = _start(orchestrator)

        assert state.total_nodes == 2
        assert cluster.subjects_on(HEAD_IP)[0] == "c-head"
        assert "c-worker-0" in cluster.subjects_on(WORKER_IP)
        assert "c-worker-0" not in cluster.subjects_on(HEAD_IP)

    def test_the_docker_config_blob_still_reaches_run_container(self, cluster):
        """The dict used to be passed through; it is now mapped to kwargs."""
        seen: dict = {}

        class _Capturing(FakeNode):
            def run_container(self, image, name, env_vars, metadata, **kwargs):
                seen.update(kwargs)
                return super().run_container(image, name, env_vars, metadata)

        cluster.nodes[HEAD_IP] = _Capturing(HEAD_IP, cluster.log)
        orchestrator = ClusterOrchestrator(
            services=cluster,
            ssh_client=MagicMock(),
            event_broadcaster=MagicMock(),
        )

        _start(
            orchestrator,
            worker_ips=[],
            docker_config={"gpu_count": 1, "shm_size_gb": 32, "privileged": False},
        )

        assert seen["shm_size_gb"] == 32
        assert seen["privileged"] is False

    def test_a_worker_is_given_no_hub_token_and_is_pinned_offline(self, cluster):
        """The token stays on the control node; the worker runs on replicas.

        A worker whose weights were replicated needs no credential, and giving
        it one is how a gated token ends up on every node. Being offline turns
        a missing file into an immediate failure rather than a silent
        re-download of hundreds of gigabytes over the uplink.
        """
        seen: dict[str, dict[str, str]] = {}

        class _Capturing(FakeNode):
            def run_container(self, image, name, env_vars, metadata, **kwargs):
                seen[name] = dict(env_vars)
                return super().run_container(image, name, env_vars, metadata, **kwargs)

        cluster.nodes[HEAD_IP] = _Capturing(HEAD_IP, cluster.log)
        cluster.nodes[WORKER_IP] = _Capturing(WORKER_IP, cluster.log)
        orchestrator = ClusterOrchestrator(
            services=cluster,
            ssh_client=MagicMock(),
            event_broadcaster=MagicMock(),
        )

        _start(
            orchestrator,
            env_vars={"HF_TOKEN": "hf_secret", "NCCL_SOCKET_IFNAME": "eth0"},
        )

        assert "HF_TOKEN" not in seen["c-worker-0"]
        assert seen["c-worker-0"]["HF_HUB_OFFLINE"] == "1"
        assert seen["c-worker-0"]["NCCL_SOCKET_IFNAME"] == "eth0"
        # The head is where the download happened, so it keeps its token.
        assert seen["c-head"]["HF_TOKEN"] == "hf_secret"

    def test_rollback_stops_each_container_on_its_own_node(self, orchestrator, cluster):
        _start(orchestrator)
        cluster.log.clear()

        orchestrator.rollback_cluster("c", HEAD_IP, [WORKER_IP])

        assert cluster.subjects_on(HEAD_IP) == ["c-head"]
        assert cluster.subjects_on(WORKER_IP) == ["c-worker-0"]
        assert cluster.actions_on(WORKER_IP) == ["stop"]

    def test_a_failed_worker_rolls_the_head_back(self, cluster):
        class _Broken(FakeNode):
            def run_container(self, *args, **kwargs):
                raise RuntimeError("worker unreachable")

        cluster.nodes[WORKER_IP] = _Broken(WORKER_IP, cluster.log)
        orchestrator = ClusterOrchestrator(
            services=cluster,
            ssh_client=MagicMock(),
            event_broadcaster=MagicMock(),
        )

        with pytest.raises(RuntimeError, match="Cluster startup failed"):
            _start(orchestrator)

        assert "stop" in cluster.actions_on(HEAD_IP)
        assert cluster.nodes[HEAD_IP].containers == {}

    def test_stop_sweeps_the_nodes_it_is_given(self, orchestrator, cluster):
        """A container is stopped on the node it was listed from."""
        _start(orchestrator)
        cluster.log.clear()

        orchestrator.stop_cluster("c", node_addresses=[HEAD_IP, WORKER_IP])

        assert cluster.actions_on(HEAD_IP) == ["list", "stop"]
        assert cluster.actions_on(WORKER_IP) == ["list", "stop"]
        assert cluster.nodes[WORKER_IP].containers == {}

    def test_stop_without_addresses_asks_the_control_node_explicitly(
        self, orchestrator, cluster
    ):
        """No empty host: the default is a resolved control node."""
        orchestrator.stop_cluster("c")

        assert list(cluster.nodes) == ["control"]
        assert cluster.actions_on("control") == ["list"]

    def test_status_reads_ray_on_the_head_node(self, orchestrator, cluster):
        _start(orchestrator, no_ray=False)
        cluster.log.clear()

        state = orchestrator.get_cluster_status("c", node_addresses=[HEAD_IP])

        assert state.head.container_name == "c-head"
        assert state.ray_enabled is True
        assert state.ray_ready is True
        ray_execs = [
            args
            for node, action, args in cluster.log
            if node == HEAD_IP and action == "exec"
        ]
        assert ray_execs and ray_execs[0][1] == ("ray", "status")

    def test_status_lists_with_the_cluster_label(self, orchestrator, cluster):
        _start(orchestrator)
        cluster.log.clear()

        orchestrator.get_cluster_status("c", node_addresses=[HEAD_IP])

        listings = [args[0] for _, action, args in cluster.log if action == "list"]
        assert listings == [str({CLUSTER_LABEL: "c"})]

    def test_status_raises_when_no_head_is_found(self, orchestrator):
        with pytest.raises(RuntimeError, match="No head node"):
            orchestrator.get_cluster_status("missing")

    def test_health_validation_runs_against_every_node(self, orchestrator, cluster):
        """The health check the empty host had reading one daemon twice."""
        _start(orchestrator, no_ray=False)

        env_reads = [
            node
            for node, action, args in cluster.log
            if action == "exec" and args[1] == ("env",)
        ]
        assert sorted(env_reads) == [HEAD_IP, WORKER_IP]

    def test_the_orchestrator_exposes_its_resolver(self, orchestrator, cluster):
        """The router reaches nodes through this, not a private service."""
        assert orchestrator.services is cluster

    def test_capacity_failure_never_starts_a_container(self, orchestrator, cluster):
        with pytest.raises(RuntimeError, match="Cluster startup failed"):
            _start(
                orchestrator,
                env_vars={"COMMAND": "--tensor-parallel-size 64"},
                docker_config={"gpu_count": 1},
            )

        assert "run" not in cluster.actions_on(HEAD_IP)


class TestPerNodeImageReference:
    """A node with no registry credential is pointed at the control node's copy.

    The digest is the same either way; only the host changes, which is why the
    registry base, the repository and the digest are stored as three fields
    rather than one opaque reference.
    """

    REF = "ghcr.io/acme/engine:1"
    DIGEST = "sha256:" + "a1" * 32

    def _seed(self):
        from spark_pulse.mock import registry as mock_registry

        mock_registry.default_registry().seed_manually(self.REF, self.DIGEST)
        return mock_registry.describe(self.REF, self.DIGEST)["pull_ref"]

    def test_a_worker_is_given_the_seeded_reference(self, orchestrator, cluster):
        expected = self._seed()

        _start(orchestrator, image=self.REF)

        worker = cluster.nodes[WORKER_IP].containers["c-worker-0"]
        assert worker.image == expected
        assert worker.image.endswith(f"@{self.DIGEST}")
        assert worker.image != self.REF

    def test_the_control_node_keeps_the_upstream_reference(self, orchestrator, cluster):
        """It is the node holding the credential; it has no need of the copy."""
        self._seed()

        _start(orchestrator, head_ip="127.0.0.1", image=self.REF)

        assert cluster.nodes["127.0.0.1"].containers["c-head"].image == self.REF

    def test_an_image_the_registry_does_not_hold_is_passed_through(
        self, orchestrator, cluster
    ):
        _start(orchestrator, image=self.REF)

        assert cluster.nodes[WORKER_IP].containers["c-worker-0"].image == self.REF
