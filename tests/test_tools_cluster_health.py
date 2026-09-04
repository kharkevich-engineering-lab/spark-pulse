"""Tests for cluster health validation.

Each check now resolves the node it is about and asks that node's own Docker
daemon. Before, all five passed an empty host, so every check ran against the
control node: a worker's container status was really the head's, and the NCCL
consistency check compared one node with itself and could not report a
disagreement even in principle. The fakes below are therefore *per node* — the
suite could not previously express the difference.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from spark_pulse.tools.cluster_health import (
    ValidationResult,
    validate_cluster,
)
from spark_pulse.tools.cluster_models import ClusterNode, ClusterState
from spark_pulse.tools.docker import ExecResult
from spark_pulse.tools.node_service import Node

HEAD_IP = "10.0.0.1"
WORKER_IP = "10.0.0.2"


class NodeFakes:
    """One fake container service per node, each with its own answers."""

    def __init__(self, **per_node):
        """Args are ``address -> {running, ray, gpu, nccl}`` overrides."""
        self.settings = {
            address: {
                "running": True,
                "ray": "OK",
                "gpu": "1",
                "nccl": "eth0",
                **overrides,
            }
            for address, overrides in per_node.items()
        }
        self.asked: list[tuple[str, str]] = []
        self._services: dict[str, MagicMock] = {}

    def __call__(self, node: Node) -> MagicMock:
        address = node.address or node.id
        service = self._services.get(address)
        if service is None:
            service = self._build(address)
            self._services[address] = service
        return service

    def _build(self, address: str) -> MagicMock:
        settings = self.settings.get(address, {})
        service = MagicMock(name=f"NodeService({address})")

        def _status(name, _address=address):
            self.asked.append((_address, f"status:{name}"))
            return {"running": bool(settings.get("running", True))}

        def _exec(container, command, detach=False, timeout=None, _address=address):
            self.asked.append((_address, f"exec:{container}:{command[0]}"))
            if command[:2] == ["ray", "status"]:
                ray = settings.get("ray")
                if ray is None:
                    return ExecResult(1, "", "no ray")
                return ExecResult(0, ray)
            if command[0] == "nvidia-smi":
                gpu = settings.get("gpu")
                if gpu is None:
                    return ExecResult(1, "", "nvidia-smi not found")
                return ExecResult(0, gpu)
            if command == ["env"]:
                return ExecResult(0, f"NCCL_SOCKET_IFNAME={settings.get('nccl', '')}")
            return ExecResult(0, "")

        service.get_container_status.side_effect = _status
        service.exec_in_container.side_effect = _exec
        return service

    def nodes_asked(self) -> list[str]:
        """Every node address that was asked anything, in order."""
        seen: list[str] = []
        for address, _ in self.asked:
            if address not in seen:
                seen.append(address)
        return seen

    def containers_asked_on(self, address: str) -> set[str]:
        """Container names this node's daemon was asked about."""
        return {
            question.split(":")[1] for node, question in self.asked if node == address
        }


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
        with pytest.raises(Exception):
            result.healthy = False


class TestValidateCluster:
    """Tests for validate_cluster: one node, one daemon, one answer."""

    def _make_state(self, head_status="running", worker_statuses=None):
        """Create a test ClusterState."""
        head = ClusterNode(
            ip=HEAD_IP,
            role="head",
            container_name="cluster-head",
            status=head_status,
            gpu_count=8,
        )
        workers = []
        for i, status in enumerate(worker_statuses or []):
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
        state = self._make_state(worker_statuses=["running"])
        services = NodeFakes(**{HEAD_IP: {}, WORKER_IP: {}})

        result = validate_cluster(state, services)

        assert result.healthy is True

    def test_every_node_is_asked_on_its_own_daemon(self):
        """The whole point: two nodes, two daemons, neither read twice."""
        state = self._make_state(worker_statuses=["running"])
        services = NodeFakes(**{HEAD_IP: {}, WORKER_IP: {}})

        validate_cluster(state, services)

        assert services.nodes_asked() == [HEAD_IP, WORKER_IP]
        assert services.containers_asked_on(HEAD_IP) == {"cluster-head"}
        assert services.containers_asked_on(WORKER_IP) == {"cluster-worker-0"}

    def test_head_not_running(self):
        state = self._make_state(head_status="stopped")
        services = NodeFakes(**{HEAD_IP: {"running": False}})

        result = validate_cluster(state, services)

        assert any("not running" in e for e in result.errors)

    def test_worker_not_running_is_read_from_the_worker(self):
        """A stopped worker beside a running head. The old code saw the head."""
        state = self._make_state(worker_statuses=["stopped"])
        services = NodeFakes(**{HEAD_IP: {}, WORKER_IP: {"running": False}})

        result = validate_cluster(state, services)

        assert any("cluster-worker-0 is not running" in e for e in result.errors)
        assert not any("cluster-head is not running" in e for e in result.errors)

    def test_ray_not_healthy(self):
        state = self._make_state()
        services = NodeFakes(**{HEAD_IP: {"ray": "Ray not ready"}})

        result = validate_cluster(state, services)

        assert any("Ray" in e for e in result.errors)

    def test_a_worker_with_no_ray_is_reported_against_that_worker(self):
        state = self._make_state(worker_statuses=["running"])
        services = NodeFakes(**{HEAD_IP: {}, WORKER_IP: {"ray": None}})

        result = validate_cluster(state, services)

        assert any(f"Ray worker on {WORKER_IP}" in e for e in result.errors)

    def test_gpu_check_warning(self):
        state = self._make_state()
        services = NodeFakes(**{HEAD_IP: {"gpu": None}})

        result = validate_cluster(state, services)

        assert any("GPU" in w for w in result.warnings)

    def test_nccl_inconsistency_is_a_real_comparison_now(self):
        """Two nodes, two interfaces. This could not be expressed before."""
        state = self._make_state(worker_statuses=["running"])
        services = NodeFakes(**{HEAD_IP: {"nccl": "eth0"}, WORKER_IP: {"nccl": "ib0"}})

        result = validate_cluster(state, services)

        warning = next(w for w in result.warnings if "NCCL" in w)
        assert HEAD_IP in warning and WORKER_IP in warning
        assert "eth0" in warning and "ib0" in warning

    def test_matching_nccl_across_nodes_is_not_a_warning(self):
        state = self._make_state(worker_statuses=["running"])
        services = NodeFakes(**{HEAD_IP: {"nccl": "ib0"}, WORKER_IP: {"nccl": "ib0"}})

        result = validate_cluster(state, services)

        assert not any("NCCL" in w for w in result.warnings)

    def test_a_solo_cluster_cannot_be_inconsistent_with_itself(self):
        """One node reads its own interface once, and that is consistent."""
        state = self._make_state()
        services = NodeFakes(**{HEAD_IP: {"nccl": "eth0"}})

        result = validate_cluster(state, services)

        assert not any("NCCL" in w for w in result.warnings)
        assert services.nodes_asked() == [HEAD_IP]

    def test_validate_cluster_needs_a_resolver(self):
        """There is no node-less call to fall back onto."""
        state = self._make_state()
        with pytest.raises(TypeError):
            validate_cluster(state)
