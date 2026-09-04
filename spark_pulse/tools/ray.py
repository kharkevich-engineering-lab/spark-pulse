"""Ray cluster management for multi-node vLLM deployments.

Provides idempotent Ray head/worker startup and status checking.
Deployment retries must not break existing Ray clusters.
"""

from __future__ import annotations

import logging
import time

from spark_pulse.tools.remote_docker import RemoteDockerService

logger = logging.getLogger(__name__)


class RayManager:
    """Manages Ray cluster lifecycle on vLLM containers.

    Depends on RemoteDockerService for container operations.
    All operations are idempotent — retries are safe.
    """

    def __init__(self, remote_docker: RemoteDockerService):
        """Initialize Ray manager.

        Args:
            remote_docker: RemoteDockerService for container operations.
        """
        self._docker = remote_docker

    def ensure_ray_head(
        self,
        container: str,
        node_ip: str,
        port: int = 29501,
        timeout: int = 120,
        poll_interval: float = 2,
    ) -> bool:
        """Ensure Ray head is running (idempotent).

        1. Check ray status in container
        2. If Ray already running and healthy → return True
        3. Otherwise → start Ray head
        4. Wait for ready, return result

        Args:
            container: Container name or ID.
            node_ip: IP address of this node.
            port: Ray head port (default 29501).
            timeout: Seconds to wait for Ray to become ready.
            poll_interval: Seconds between status checks.

        Returns:
            True if Ray is ready (was already running or just started).
        """
        # Check if Ray is already running
        if self._is_ray_ready(container, timeout=poll_interval):
            logger.info("Ray head already ready in %s", container)
            return True

        # Start Ray head
        logger.info("Starting Ray head in %s at %s:%d", container, node_ip, port)
        result = self._docker.exec_container(
            "",
            container,
            [
                "ray",
                "start",
                "--block",
                "--head",
                "--node-ip-address",
                node_ip,
                f"--port={port}",
            ],
            timeout=timeout,
        )

        if not result.ok:
            logger.error("Failed to start Ray head: %s", result.stderr)
            return False

        # Wait for Ray to become ready
        return self.wait_for_cluster_ready(
            container, timeout=timeout, poll_interval=poll_interval
        )

    def ensure_ray_worker(
        self,
        container: str,
        worker_ip: str,
        head_ip: str,
        head_port: int = 29501,
        timeout: int = 120,
        poll_interval: float = 2,
    ) -> bool:
        """Ensure Ray worker is connected (idempotent).

        1. Check ray status in container
        2. If already connected to head → return True
        3. Otherwise → start Ray worker connecting to head
        4. Wait for connected, return result

        Args:
            container: Container name or ID.
            worker_ip: IP address of this worker node.
            head_ip: IP address of the Ray head node.
            head_port: Ray head port (default 29501).
            timeout: Seconds to wait for Ray to become connected.
            poll_interval: Seconds between status checks.

        Returns:
            True if Ray worker is connected.
        """
        # Check if Ray is already connected
        if self._is_ray_connected(container, timeout=poll_interval):
            logger.info("Ray worker already connected in %s", container)
            return True

        # Start Ray worker
        address = f"{head_ip}:{head_port}"
        logger.info(
            "Starting Ray worker in %s connecting to %s",
            container,
            address,
        )
        result = self._docker.exec_container(
            "",
            container,
            [
                "ray",
                "start",
                "--block",
                "--address",
                address,
                "--node-ip-address",
                worker_ip,
            ],
            timeout=timeout,
        )

        if not result.ok:
            logger.error("Failed to start Ray worker: %s", result.stderr)
            return False

        # Wait for worker to connect
        return self.wait_for_cluster_ready(
            container, timeout=timeout, poll_interval=poll_interval
        )

    def wait_for_cluster_ready(
        self,
        container: str,
        timeout: int = 60,
        poll_interval: float = 2,
    ) -> bool:
        """Poll ray status until responsive.

        Args:
            container: Container name or ID.
            timeout: Maximum seconds to wait.
            poll_interval: Seconds between checks.

        Returns:
            True if Ray cluster is ready within timeout.
        """
        start = time.time()
        while time.time() - start < timeout:
            status = self.get_ray_status(container)
            if "OK" in status or "cluster is ready" in status.lower():
                return True
            time.sleep(poll_interval)

        logger.warning("Ray cluster not ready after %ds", timeout)
        return False

    def get_ray_status(self, container: str) -> str:
        """Return ray status output.

        Args:
            container: Container name or ID.

        Returns:
            Ray status output string.
        """
        result = self._docker.exec_container(
            "",
            container,
            ["ray", "status"],
            timeout=10,
        )
        return result.stdout if result.ok else f"error: {result.stderr}"

    def _is_ray_ready(
        self,
        container: str,
        timeout: int = 5,
    ) -> bool:
        """Check if Ray head is ready.

        Args:
            container: Container name or ID.
            timeout: Seconds to wait for status check.

        Returns:
            True if Ray head reports ready.
        """
        try:
            result = self._docker.exec_container(
                "",
                container,
                ["ray", "status"],
                timeout=timeout,
            )
            if not result.ok:
                return False
            output = result.stdout.lower()
            return "ok" in output or "ready" in output or "cluster" in output
        except Exception:
            return False

    def _is_ray_connected(
        self,
        container: str,
        timeout: int = 5,
    ) -> bool:
        """Check if Ray worker is connected to head.

        Args:
            container: Container name or ID.
            timeout: Seconds to wait for status check.

        Returns:
            True if Ray worker reports connected.
        """
        try:
            result = self._docker.exec_container(
                "",
                container,
                ["ray", "status"],
                timeout=timeout,
            )
            if not result.ok:
                return False
            output = result.stdout.lower()
            return "ok" in output or "connected" in output or "cluster" in output
        except Exception:
            return False
