"""Mock health tools — deployment and cluster health simulation.

Returns deterministic results without accessing Docker or cluster state.
Mirrors the real health.py API exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class MockDeploymentHealth:
    """Mock deployment health status."""

    deployment_id: str
    container_status: str = "running"
    ray_status: str = "n/a"
    process_status: str = "alive"
    error: str | None = None
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )


@dataclass
class MockClusterHealth:
    """Mock cluster health status."""

    cluster_name: str
    healthy: bool = True
    head_status: str = "running"
    worker_statuses: list[str] = field(default_factory=list)
    ray_ready: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )


class MockHealthMonitor:
    """Mock health monitor for simulation mode.

    Scenario-driven simulation:
    - "healthy": all deployments/clusters healthy
    - "degraded": some workers unhealthy
    - "critical": head node unhealthy
    """

    def __init__(self, scenario: str = "healthy"):
        self._scenario = scenario
        self._tracked: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        """Mock start — no-op."""
        pass

    def stop(self) -> None:
        """Mock stop — no-op."""
        pass

    def track_deployment(self, deployment_id: str, deployment_info: dict[str, Any]) -> None:
        """Track a deployment."""
        self._tracked[deployment_id] = {"type": "deployment", "info": deployment_info}

    def track_cluster(self, cluster_name: str, cluster_info: dict[str, Any]) -> None:
        """Track a cluster."""
        self._tracked[cluster_name] = {"type": "cluster", "info": cluster_info}

    def untrack(self, identifier: str) -> None:
        """Untrack an identifier."""
        self._tracked.pop(identifier, None)

    def check_deployment(self, deployment_id: str, deployment_info: dict[str, Any]) -> MockDeploymentHealth:
        """Check mock deployment health."""
        if self._scenario == "healthy":
            return MockDeploymentHealth(
                deployment_id=deployment_id,
                container_status="running",
                process_status="alive",
            )
        elif self._scenario == "degraded":
            return MockDeploymentHealth(
                deployment_id=deployment_id,
                container_status="running",
                process_status="alive",
                error="Ray worker disconnected",
            )
        else:
            return MockDeploymentHealth(
                deployment_id=deployment_id,
                container_status="error",
                process_status="dead",
                error="Container crashed",
            )

    def check_cluster(self, cluster_name: str, cluster_info: dict[str, Any]) -> MockClusterHealth:
        """Check mock cluster health."""
        if self._scenario == "healthy":
            return MockClusterHealth(
                cluster_name=cluster_name,
                healthy=True,
                head_status="running",
                worker_statuses=["running", "running"],
                ray_ready=True,
            )
        elif self._scenario == "degraded":
            return MockClusterHealth(
                cluster_name=cluster_name,
                healthy=False,
                head_status="running",
                worker_statuses=["running", "error"],
                ray_ready=False,
                warnings=["Worker 10.0.0.3 is not running"],
                errors=["Worker 10.0.0.3 status: error"],
            )
        else:
            return MockClusterHealth(
                cluster_name=cluster_name,
                healthy=False,
                head_status="error",
                worker_statuses=["stopped", "stopped"],
                ray_ready=False,
                errors=["Head node is not running"],
            )

    @property
    def tracked(self) -> dict[str, dict[str, Any]]:
        """Return tracked items."""
        return dict(self._tracked)


def mock_check_deployment(deployment_id: str) -> MockDeploymentHealth:
    """Mock deployment health check."""
    monitor = MockHealthMonitor()
    return monitor.check_deployment(deployment_id, {})


def mock_check_cluster(cluster_name: str) -> MockClusterHealth:
    """Mock cluster health check."""
    monitor = MockHealthMonitor()
    return monitor.check_cluster(cluster_name, {})
