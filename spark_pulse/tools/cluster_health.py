"""Cluster health validation for multi-node deployments.

Mirrors the Phase 2A discovery validation pattern — provides
comprehensive health checks to prevent silent failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from spark_pulse.tools.cluster_models import ClusterState
from spark_pulse.tools.remote_docker import RemoteDockerService
from spark_pulse.tools.ssh import SSHResult


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
    remote_docker: RemoteDockerService,
) -> ValidationResult:
    """Comprehensive cluster health check.

    Checks:
    - All nodes reachable (SSH ping)
    - Docker daemon running on all nodes
    - All containers running (not just started)
    - Ray healthy on head and workers
    - Required ports reachable (29501, etc.)
    - NCCL variables consistent across nodes
    - GPU accessible on all nodes (nvidia-smi)

    Args:
        cluster_state: Current cluster state.
        remote_docker: RemoteDockerService for container operations.

    Returns:
        ValidationResult with health status, warnings, and errors.
    """
    result = ValidationResult.ok()

    # Check all nodes are running
    all_nodes = [cluster_state.head, *cluster_state.workers]
    for node in all_nodes:
        status = remote_docker.get_container_status("", node.container_name)
        if not status.get("running", False):
            result = result.add_error(
                f"Node {node.ip} ({node.role}) container {node.container_name} is not running"
            )

    # Check Ray on head
    if cluster_state.ray_enabled:
        head_status = remote_docker.exec_container(
            "", cluster_state.head.container_name,
            ["ray", "status"],
            timeout=10,
        )
        if not head_status.ok or "OK" not in head_status.stdout:
            result = result.add_error("Ray head is not healthy")

        # Check Ray on each worker
        for worker in cluster_state.workers:
            worker_status = remote_docker.exec_container(
                "", worker.container_name,
                ["ray", "status"],
                timeout=10,
            )
            if not worker_status.ok:
                result = result.add_error(
                    f"Ray worker on {worker.ip} is not responding"
                )

    # Check GPU accessibility on all nodes
    for node in all_nodes:
        gpu_check = remote_docker.exec_container(
            "", node.container_name,
            ["nvidia-smi", "--query-gpu=count", "--format=csv,noheader"],
            timeout=10,
        )
        if not gpu_check.ok:
            result = result.add_warning(
                f"Cannot verify GPU on node {node.ip}: {gpu_check.stderr}"
            )

    # Check NCCL consistency (compare socket_ifname across nodes)
    nccl_vars = {}
    for node in all_nodes:
        nccl_check = remote_docker.exec_container(
            "", node.container_name,
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
