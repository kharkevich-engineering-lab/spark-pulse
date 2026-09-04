"""Mock health tools — deployment health simulation.

Returns deterministic results without accessing Docker. Mirrors the real
health.py API exactly.
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
    process_status: str = "alive"
    error: str | None = None
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )


class MockHealthMonitor:
    """Mock health monitor for simulation mode.

    Scenario-driven simulation:
    - "healthy": every deployment healthy
    - "degraded": the container runs but the deployment reports an error
    - "critical": the container crashed
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

    def track_deployment(
        self, deployment_id: str, deployment_info: dict[str, Any]
    ) -> None:
        """Track a deployment."""
        self._tracked[deployment_id] = {"type": "deployment", "info": deployment_info}

    def untrack(self, identifier: str) -> None:
        """Untrack an identifier."""
        self._tracked.pop(identifier, None)

    def check_deployment(
        self, deployment_id: str, deployment_info: dict[str, Any]
    ) -> MockDeploymentHealth:
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
                error="Engine stopped answering its readiness probe",
            )
        else:
            return MockDeploymentHealth(
                deployment_id=deployment_id,
                container_status="error",
                process_status="dead",
                error="Container crashed",
            )

    @property
    def tracked(self) -> dict[str, dict[str, Any]]:
        """Return tracked items."""
        return dict(self._tracked)


def mock_check_deployment(deployment_id: str) -> MockDeploymentHealth:
    """Mock deployment health check."""
    monitor = MockHealthMonitor()
    return monitor.check_deployment(deployment_id, {})
