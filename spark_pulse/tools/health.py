"""Health monitoring for deployments and clusters.

Provides deployment health checks, background monitoring, and status reporting.
Includes structured logging, retry logic, and persistent tracking state.
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
    """Persist tracked clusters/deployments to disk.

    Format: {"deployments": [...], "clusters": [...]}
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
    """Load tracked clusters/deployments from disk."""
    if not _HEALTH_TRACKING_FILE.exists():
        return {"deployments": [], "clusters": []}
    try:
        with open(_HEALTH_TRACKING_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load health tracking: {e}")
        return {"deployments": [], "clusters": []}


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
    ray_status: str = "unknown"  # ready, not_ready, error, n/a
    process_status: str = "unknown"  # alive, dead, unknown
    error: str | None = None
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass(frozen=True)
class ClusterHealth:
    """Health status for a cluster deployment."""

    cluster_name: str
    healthy: bool = False
    head_status: str = "unknown"
    worker_statuses: list[str] = field(default_factory=list)
    ray_ready: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class HealthMonitor:
    """Background health monitor for deployments and clusters.

    Polls deployment/cluster health at configurable intervals and
    broadcasts updates via SSE.
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

    def track_cluster(self, cluster_name: str, cluster_info: dict[str, Any]) -> None:
        """Add a cluster to health monitoring."""
        with self._lock:
            self._tracked[cluster_name] = {
                "type": "cluster",
                "info": cluster_info,
            }
        self._persist_state()

    def untrack(self, identifier: str) -> None:
        """Remove a deployment or cluster from monitoring."""
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
            "clusters": [
                {"name": k, **v}
                for k, v in self._tracked.items()
                if v.get("type") == "cluster"
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
        """Check health of all tracked deployments and clusters."""
        with self._lock:
            tracked = dict(self._tracked)

        for identifier, data in tracked.items():
            try:
                if data["type"] == "deployment":
                    health = self._check_deployment(identifier, data["info"])
                else:
                    health = self._check_cluster(identifier, data["info"])

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
        """Check health of a solo deployment."""
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

            # Check Ray status if applicable
            ray_status = "n/a"
            if deployment_info.get("ray_enabled"):
                ray_status = "not_ready"  # Simplified for solo mode

            return DeploymentHealth(
                deployment_id=deployment_id,
                container_status=container_status,
                ray_status=ray_status,
                process_status="alive" if container_status == "running" else "dead",
            )
        except Exception as e:
            logger.error("Deployment health check failed for %s: %s", deployment_id, e)
            return DeploymentHealth(
                deployment_id=deployment_id,
                container_status="error",
                error=str(e),
            )

    def _check_cluster(
        self,
        cluster_name: str,
        cluster_info: dict[str, Any],
    ) -> ClusterHealth:
        """Check health of a cluster deployment."""
        from spark_pulse import tools

        try:
            orchestrator = tools.cluster.ClusterOrchestrator()
            state = orchestrator.get_cluster_status(cluster_name)
            if state is None:
                return ClusterHealth(
                    cluster_name=cluster_name,
                    healthy=False,
                    errors=[f"Cluster state not found: {cluster_name}"],
                )

            head_status = state.head.status
            worker_statuses = [w.status for w in state.workers]
            ray_ready = state.ray_ready
            healthy = state.healthy

            warnings: list[str] = []
            errors: list[str] = []

            if not healthy:
                for w in state.workers:
                    if w.status != "running":
                        errors.append(f"Worker {w.ip} is {w.status}")

            return ClusterHealth(
                cluster_name=cluster_name,
                healthy=healthy,
                head_status=head_status,
                worker_statuses=worker_statuses,
                ray_ready=ray_ready,
                warnings=warnings,
                errors=errors,
            )
        except Exception as e:
            logger.error("Cluster health check failed for %s: %s", cluster_name, e)
            return ClusterHealth(
                cluster_name=cluster_name,
                healthy=False,
                errors=[str(e)],
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
