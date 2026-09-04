"""Mock Ray manager for simulation mode.

Mirrors the real ray.py API exactly for testing without real Ray clusters.
"""

from __future__ import annotations

from typing import Any


class MockRayManager:
    """Mock RayManager for simulation mode.

    Simulates Ray head/worker startup and status checking.
    """

    def __init__(
        self,
        ready: bool = True,
        fail_containers: list[str] | None = None,
    ):
        """Initialize mock Ray manager.

        Args:
            ready: Whether Ray operations succeed.
            fail_containers: Containers that should fail Ray operations.
        """
        self._ready = ready
        self._fail_containers = fail_containers or []
        self._executed_operations: list[dict[str, Any]] = []

    def ensure_ray_head(
        self,
        container: str,
        node_ip: str,
        port: int = 29501,
        timeout: int = 120,
        poll_interval: float = 2,
    ) -> bool:
        """Ensure Ray head is running (mocked)."""
        self._executed_operations.append(
            {
                "action": "ensure_ray_head",
                "container": container,
                "node_ip": node_ip,
                "port": port,
            }
        )

        if container in self._fail_containers:
            return False
        return self._ready

    def ensure_ray_worker(
        self,
        container: str,
        worker_ip: str,
        head_ip: str,
        head_port: int = 29501,
        timeout: int = 120,
        poll_interval: float = 2,
    ) -> bool:
        """Ensure Ray worker is connected (mocked)."""
        self._executed_operations.append(
            {
                "action": "ensure_ray_worker",
                "container": container,
                "worker_ip": worker_ip,
                "head_ip": head_ip,
                "head_port": head_port,
            }
        )

        if container in self._fail_containers:
            return False
        return self._ready

    def wait_for_cluster_ready(
        self,
        container: str,
        timeout: int = 60,
        poll_interval: float = 2,
    ) -> bool:
        """Poll ray status until responsive (mocked)."""
        self._executed_operations.append(
            {
                "action": "wait_for_cluster_ready",
                "container": container,
                "timeout": timeout,
            }
        )
        return self._ready

    def get_ray_status(self, container: str) -> str:
        """Return ray status output (mocked)."""
        self._executed_operations.append(
            {
                "action": "get_ray_status",
                "container": container,
            }
        )
        return "Cluster is ready" if self._ready else "Ray not started"

    @property
    def executed_operations(self) -> list[dict[str, Any]]:
        """Return list of all executed operations."""
        return self._executed_operations.copy()

    def reset(self) -> None:
        """Clear executed operations history."""
        self._executed_operations.clear()
