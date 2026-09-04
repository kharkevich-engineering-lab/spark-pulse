"""Cluster health validation for multi-node deployments.

Mirrors the Phase 2A discovery validation pattern — provides
comprehensive health checks to prevent silent failures.

Every check resolves the node it is about and asks *that* node's Docker
daemon. Before, all five checks passed an empty host and so ran against the
control node's own daemon: a worker's container status was the head's, the
per-worker Ray probe re-read the head, and the NCCL consistency check compared
the control node with itself and could never report an inconsistency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from spark_pulse.tools.cluster_models import ClusterState
from spark_pulse.tools.node_service import Node, NodeService, node_for


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Cluster health check results."""

    healthy: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def ok(cls) -> "ValidationResult":
        """Return a healthy result with no warnings or errors."""
        return cls(healthy=True)

    @classmethod
    def with_errors(cls, errors: list[str]) -> "ValidationResult":
        """Return an unhealthy result with the given errors."""
        return cls(healthy=False, errors=errors)

    @classmethod
    def with_warnings(cls, warnings: list[str]) -> "ValidationResult":
        """Return a healthy result with warnings (non-critical)."""
        return cls(healthy=True, warnings=warnings)

    def add_error(self, error: str) -> "ValidationResult":
        """Return a new ValidationResult with an additional error."""
        return ValidationResult(
            healthy=False,
            warnings=self.warnings,
            errors=self.errors + [error],
        )

    def add_warning(self, warning: str) -> "ValidationResult":
        """Return a new ValidationResult with an additional warning."""
        return ValidationResult(
            healthy=self.healthy,
            warnings=self.warnings + [warning],
            errors=self.errors,
        )


def validate_cluster(
    cluster_state: ClusterState,
    services: Callable[[Node], NodeService],
) -> ValidationResult:
    """Comprehensive cluster health check.

    Checks:
    - All containers running, each on its own node
    - Ray healthy on the head and on every worker, each asked on its own node
    - GPU accessible inside every node's container
    - NCCL socket interface consistent across nodes

    Args:
        cluster_state: Current cluster state.
        services: Resolver from node to the container service bound to it.

    Returns:
        ValidationResult with health status, warnings, and errors.
    """
    result = ValidationResult.ok()
    all_nodes = [cluster_state.head, *cluster_state.workers]

    def _service_for_node(node: object) -> NodeService:
        return services(node_for(getattr(node, "ip", "")))

    # Every container is checked on the machine it actually runs on.
    for node in all_nodes:
        status = _service_for_node(node).get_container_status(node.container_name)
        if not status.get("running", False):
            result = result.add_error(
                f"Node {node.ip} ({node.role}) container {node.container_name} is not running"
            )

    if cluster_state.ray_enabled:
        head_status = _service_for_node(cluster_state.head).exec_in_container(
            cluster_state.head.container_name,
            ["ray", "status"],
            timeout=10,
        )
        if not head_status.ok or "OK" not in head_status.stdout:
            result = result.add_error("Ray head is not healthy")

        for worker in cluster_state.workers:
            worker_status = _service_for_node(worker).exec_in_container(
                worker.container_name,
                ["ray", "status"],
                timeout=10,
            )
            if not worker_status.ok:
                result = result.add_error(
                    f"Ray worker on {worker.ip} is not responding"
                )

    for node in all_nodes:
        gpu_check = _service_for_node(node).exec_in_container(
            node.container_name,
            ["nvidia-smi", "--query-gpu=count", "--format=csv,noheader"],
            timeout=10,
        )
        if not gpu_check.ok:
            result = result.add_warning(
                f"Cannot verify GPU on node {node.ip}: {gpu_check.stderr}"
            )

    # NCCL consistency: one reading per node, so nodes that disagree are seen.
    nccl_vars: dict[str, str] = {}
    for node in all_nodes:
        nccl_check = _service_for_node(node).exec_in_container(
            node.container_name,
            ["env"],
            timeout=10,
        )
        if nccl_check.ok:
            for line in nccl_check.stdout.split("\n"):
                if "NCCL_SOCKET_IFNAME" in line:
                    nccl_vars[node.ip] = line.split("=", 1)[1] if "=" in line else ""

    if nccl_vars:
        unique_nccl = set(nccl_vars.values())
        if len(unique_nccl) > 1:
            result = result.add_warning(
                f"Inconsistent NCCL_SOCKET_IFNAME across nodes: {nccl_vars}"
            )

    return result
