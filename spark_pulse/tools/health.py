"""Health monitoring for deployments.

Provides deployment health checks, background monitoring, and status reporting.
Includes structured logging, retry logic, and persistent tracking state.

A deployment is the only thing tracked. What used to be a second, cluster-shaped
kind of health went with the cluster orchestrator: a cluster is a deployment of
size N, so its health is that deployment's health.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds
RETRY_MAX_DELAY = 30.0  # seconds

# Health tracking persistence
_HEALTH_TRACKING_FILE = Path.home() / ".config" / "spark-pulse" / "health_tracking.json"


def save_health_tracking(tracked: dict[str, dict]) -> None:
    """Persist tracked deployments to disk.

    Format: {"deployments": [...]}
    """
    _HEALTH_TRACKING_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _HEALTH_TRACKING_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(tracked, f, indent=2, default=str)
        tmp.rename(_HEALTH_TRACKING_FILE)
    except OSError as e:
        logger.warning(f"Failed to save health tracking: {e}")


def load_health_tracking() -> dict[str, list]:
    """Load tracked deployments from disk.

    A file written by an older build also carries a ``clusters`` list. It is
    returned as it is found and simply not read: nothing tracks clusters now.
    """
    if not _HEALTH_TRACKING_FILE.exists():
        return {"deployments": []}
    try:
        with open(_HEALTH_TRACKING_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load health tracking: {e}")
        return {"deployments": []}


def _retry_with_backoff(
    func: Any, *args: Any, max_retries: int = MAX_RETRIES, **kwargs: Any
) -> Any:
    """Execute a function with exponential backoff retry logic.

    Args:
        func: Function to execute
        *args: Positional arguments for the function
        max_retries: Maximum number of retry attempts
        **kwargs: Keyword arguments for the function

    Returns:
        Result from the function

    Raises:
        Exception: The last exception if all retries fail
    """
    last_exception: Exception | None = None

    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            if attempt < max_retries - 1:
                delay = min(RETRY_BASE_DELAY * (2**attempt), RETRY_MAX_DELAY)
                logger.warning(
                    "Health check attempt %d/%d failed for %s: %s. Retrying in %.1fs...",
                    attempt + 1,
                    max_retries,
                    func.__name__,
                    e,
                    delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "Health check failed after %d attempts for %s: %s",
                    max_retries,
                    func.__name__,
                    e,
                )

    if last_exception:
        raise last_exception
    raise RuntimeError("Unexpected retry failure")


@dataclass(frozen=True)
class DeploymentHealth:
    """Health status for a single deployment."""

    deployment_id: str
    container_status: str = "unknown"  # running, stopped, starting, error
    process_status: str = "unknown"  # alive, dead, unknown
    error: str | None = None
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class HealthMonitor:
    """Background health monitor for deployments.

    Polls deployment health at configurable intervals and broadcasts updates
    via SSE.
    """

    def __init__(
        self,
        check_interval: float = 30.0,
        sse_broadcast: Any = None,
    ):
        self._check_interval = check_interval
        self._sse_broadcast = sse_broadcast
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._tracked: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        """Start the background health monitor."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._thread.start()
            logger.info("Health monitor started (interval=%.fs)", self._check_interval)

    def stop(self) -> None:
        """Stop the background health monitor."""
        with self._lock:
            self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Health monitor stopped")

    def track_deployment(
        self, deployment_id: str, deployment_info: dict[str, Any]
    ) -> None:
        """Add a deployment to health monitoring."""
        with self._lock:
            self._tracked[deployment_id] = {
                "type": "deployment",
                "info": deployment_info,
            }
        self._persist_state()

    def untrack(self, identifier: str) -> None:
        """Remove a deployment from monitoring."""
        with self._lock:
            self._tracked.pop(identifier, None)
        self._persist_state()

    def _persist_state(self) -> None:
        """Convert in-memory tracking to persisted format and save."""
        tracked = self._persist_state_dict()
        save_health_tracking(tracked)

    def _persist_state_dict(self) -> dict[str, list]:
        """Convert in-memory tracking to persisted format."""
        return {
            "deployments": [
                {"id": k, **v}
                for k, v in self._tracked.items()
                if v.get("type") == "deployment"
            ],
        }

    @classmethod
    def restore_from_persistence(cls) -> dict[str, list]:
        """Load and return persisted tracking state."""
        return load_health_tracking()

    def _monitor_loop(self) -> None:
        """Background loop that polls health and broadcasts updates."""
        while self._running:
            try:
                self._check_all()
            except Exception as e:
                logger.error("Health monitor check failed: %s", e)
            time.sleep(self._check_interval)

    def _check_all(self) -> None:
        """Check health of every tracked deployment."""
        with self._lock:
            tracked = dict(self._tracked)

        for identifier, data in tracked.items():
            try:
                health = self._check_deployment(identifier, data["info"])

                if self._sse_broadcast:
                    self._sse_broadcast(
                        event="health",
                        data={
                            "type": data["type"],
                            "id": identifier,
                            "health": (
                                health.__dict__
                                if hasattr(health, "__dict__")
                                else health
                            ),
                        },
                    )
            except Exception as e:
                logger.error("Health check failed for %s: %s", identifier, e)

    def _check_deployment(
        self,
        deployment_id: str,
        deployment_info: dict[str, Any],
    ) -> DeploymentHealth:
        """Check health of a deployment."""
        from spark_pulse.tools import docker

        try:
            docker_service = docker.DockerService()
            container_name = deployment_info.get("container_name", "")

            if container_name:
                status = docker_service.get_container_status(
                    name=container_name,
                )
                container_status = (
                    status.get("status", "unknown") if status else "unknown"
                )
            else:
                container_status = "unknown"

            return DeploymentHealth(
                deployment_id=deployment_id,
                container_status=container_status,
                process_status="alive" if container_status == "running" else "dead",
            )
        except Exception as e:
            logger.error("Deployment health check failed for %s: %s", deployment_id, e)
            return DeploymentHealth(
                deployment_id=deployment_id,
                container_status="error",
                error=str(e),
            )


# Module-level singleton
_monitor: HealthMonitor | None = None
_monitor_lock = threading.Lock()


def get_health_monitor() -> HealthMonitor:
    """Get or create the global health monitor singleton."""
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = HealthMonitor()
        return _monitor


def start_health_monitor() -> HealthMonitor:
    """Start and return the global health monitor."""
    monitor = get_health_monitor()
    monitor.start()
    return monitor


def stop_health_monitor() -> None:
    """Stop the global health monitor."""
    global _monitor
    with _monitor_lock:
        if _monitor is not None:
            _monitor.stop()
            _monitor = None
