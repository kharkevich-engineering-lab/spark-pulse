"""Mock health tools — deployment health simulation.

Returns deterministic results without accessing Docker. Mirrors the real
health.py API exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class DeploymentHealth:
    """Mock deployment health status."""

    deployment_id: str
    container_status: str = "running"
    process_status: str = "alive"
    error: str | None = None
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )


class HealthMonitor:
    """Mock health monitor for simulation mode.

    Scenario-driven simulation:
    - "healthy": every deployment healthy
    - "degraded": the container runs but the deployment reports an error
    - "critical": the container crashed
    """

    def __init__(
        self,
        check_interval: float = 30.0,
        sse_broadcast: Any = None,
        scenario: str = "healthy",
    ):
        # ``check_interval`` and ``sse_broadcast`` are the real monitor's
        # arguments, accepted so either class can be constructed the same way.
        # Nothing runs in the background here, so they are ignored.
        self._scenario = scenario
        self._running = False
        self._tracked: dict[str, dict[str, Any]] = {}

    @property
    def running(self) -> bool:
        """Whether the simulated monitor is polling."""
        return self._running

    def start(self) -> None:
        """Mock start — records that the monitor is running."""
        self._running = True

    def stop(self) -> None:
        """Mock stop — records that the monitor is not running."""
        self._running = False

    def track_deployment(
        self, deployment_id: str, deployment_info: dict[str, Any]
    ) -> None:
        """Track a deployment."""
        self._tracked[deployment_id] = {"type": "deployment", "info": deployment_info}
        self._persist_state()

    def untrack(self, identifier: str) -> None:
        """Untrack an identifier."""
        self._tracked.pop(identifier, None)
        self._persist_state()

    def _persist_state(self) -> None:
        save_health_tracking(
            {
                "deployments": [
                    {"id": k, **v}
                    for k, v in self._tracked.items()
                    if v.get("type") == "deployment"
                ]
            }
        )

    @classmethod
    def restore_from_persistence(cls) -> dict[str, list]:
        """Load and return the remembered tracking state."""
        return load_health_tracking()

    def check_deployment(
        self, deployment_id: str, deployment_info: dict[str, Any]
    ) -> DeploymentHealth:
        """Check mock deployment health."""
        if self._scenario == "healthy":
            return DeploymentHealth(
                deployment_id=deployment_id,
                container_status="running",
                process_status="alive",
            )
        elif self._scenario == "degraded":
            return DeploymentHealth(
                deployment_id=deployment_id,
                container_status="running",
                process_status="alive",
                error="Engine stopped answering its readiness probe",
            )
        else:
            return DeploymentHealth(
                deployment_id=deployment_id,
                container_status="error",
                process_status="dead",
                error="Container crashed",
            )

    @property
    def tracked(self) -> dict[str, dict[str, Any]]:
        """Return tracked items."""
        return dict(self._tracked)


def mock_check_deployment(deployment_id: str) -> DeploymentHealth:
    """Mock deployment health check."""
    monitor = HealthMonitor()
    return monitor.check_deployment(deployment_id, {})


# ── Module-level API (mirrors spark_pulse.tools.health) ──────────────────────
#
# ``routers/health.py`` reaches these through the simulation switch. Without
# them every health endpoint answered with an ``AttributeError`` in its error
# field, which is a failure the caller cannot tell from a sick deployment.
# Tracking is held in memory here: the real module persists it to the
# operator's config directory, and a simulator must not write there.

_tracked_state: dict[str, list] = {"deployments": []}
_monitor: HealthMonitor | None = None


def save_health_tracking(tracked: dict[str, dict]) -> None:
    """Remember tracked deployments for this process."""
    global _tracked_state
    _tracked_state = dict(tracked)


def load_health_tracking() -> dict[str, list]:
    """The tracking this process remembers."""
    return dict(_tracked_state)


def get_health_monitor() -> HealthMonitor:
    """Get or create the simulated health monitor singleton."""
    global _monitor
    if _monitor is None:
        _monitor = HealthMonitor()
    return _monitor


def start_health_monitor() -> HealthMonitor:
    """Start and return the simulated health monitor."""
    monitor = get_health_monitor()
    monitor.start()
    return monitor


def stop_health_monitor() -> None:
    """Stop the simulated health monitor."""
    global _monitor
    if _monitor is not None:
        _monitor.stop()
        _monitor = None


def reset() -> None:
    """Forget the monitor and its tracking — one simulated machine per test."""
    global _monitor, _tracked_state
    _monitor = None
    _tracked_state = {"deployments": []}
