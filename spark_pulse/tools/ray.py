"""Ray cluster management for multi-node vLLM deployments.

Provides idempotent Ray head/worker startup and status checking.
Deployment retries must not break existing Ray clusters.

Every operation names the node it runs on, and the container service is
resolved from that node. Before, ``ensure_ray_worker`` was handed a worker IP
and then ran ``ray start`` against the control node's own Docker daemon,
because the host argument defaulted to the empty string. A worker's Ray
process was never started on the worker.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from spark_pulse.tools.node_service import Node, NodeService, NodeServices

logger = logging.getLogger(__name__)


class RayManager:
    """Manages Ray cluster lifecycle on vLLM containers.

    Holds a node resolver rather than a container service, because it acts on
    several nodes: the head's Ray process runs on the head, and each worker's
    on that worker. All operations are idempotent — retries are safe.
    """

    def __init__(self, services: Callable[[Node], NodeService] | None = None):
        """Initialize Ray manager.

        Args:
            services: Resolver from node to the container service bound to it.
                Defaults to :class:`~spark_pulse.tools.node_service.NodeServices`.
        """
        self._services = services or NodeServices()

    def _service(self, node_ip: str) -> NodeService:
        """The container service for whichever machine ``node_ip`` names."""
        from spark_pulse.tools.node_service import node_for

        return self._services(node_for(node_ip))

    def ensure_ray_head(
        self,
        container: str,
        node_ip: str,
        port: int = 29501,
        timeout: int = 120,
        poll_interval: float = 2,
    ) -> bool:
        """Ensure Ray head is running on ``node_ip`` (idempotent).

        1. Check ray status in the container, on that node
        2. If Ray already running and healthy → return True
        3. Otherwise → start Ray head there
        4. Wait for ready, return result

        Args:
            container: Container name or ID.
            node_ip: IP address of the head node.
            port: Ray head port (default 29501).
            timeout: Seconds to wait for Ray to become ready.
            poll_interval: Seconds between status checks.

        Returns:
            True if Ray is ready (was already running or just started).
        """
        if self._is_ray_ready(container, node_ip, timeout=poll_interval):
            logger.info("Ray head already ready in %s on %s", container, node_ip)
            return True

        logger.info("Starting Ray head in %s at %s:%d", container, node_ip, port)
        result = self._service(node_ip).exec_in_container(
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

        return self.wait_for_cluster_ready(
            container, node_ip, timeout=timeout, poll_interval=poll_interval
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
        """Ensure the Ray worker on ``worker_ip`` is connected (idempotent).

        Args:
            container: Container name or ID.
            worker_ip: IP address of this worker node. This is also the node
                the command runs on.
            head_ip: IP address of the Ray head node, used only to build the
                rendezvous address.
            head_port: Ray head port (default 29501).
            timeout: Seconds to wait for Ray to become connected.
            poll_interval: Seconds between status checks.

        Returns:
            True if Ray worker is connected.
        """
        if self._is_ray_connected(container, worker_ip, timeout=poll_interval):
            logger.info("Ray worker already connected in %s", container)
            return True

        address = f"{head_ip}:{head_port}"
        logger.info(
            "Starting Ray worker in %s on %s connecting to %s",
            container,
            worker_ip,
            address,
        )
        result = self._service(worker_ip).exec_in_container(
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

        return self.wait_for_cluster_ready(
            container, worker_ip, timeout=timeout, poll_interval=poll_interval
        )

    def wait_for_cluster_ready(
        self,
        container: str,
        node_ip: str,
        timeout: int = 60,
        poll_interval: float = 2,
    ) -> bool:
        """Poll ray status on ``node_ip`` until responsive.

        Args:
            container: Container name or ID.
            node_ip: The node the container runs on.
            timeout: Maximum seconds to wait.
            poll_interval: Seconds between checks.

        Returns:
            True if Ray cluster is ready within timeout.
        """
        start = time.time()
        while time.time() - start < timeout:
            status = self.get_ray_status(container, node_ip)
            if "OK" in status or "cluster is ready" in status.lower():
                return True
            time.sleep(poll_interval)

        logger.warning("Ray cluster not ready after %ds", timeout)
        return False

    def get_ray_status(self, container: str, node_ip: str) -> str:
        """Return ``ray status`` output from the container on ``node_ip``.

        Args:
            container: Container name or ID.
            node_ip: The node the container runs on.

        Returns:
            Ray status output string.
        """
        result = self._service(node_ip).exec_in_container(
            container,
            ["ray", "status"],
            timeout=10,
        )
        return result.stdout if result.ok else f"error: {result.stderr}"

    def _is_ray_ready(
        self,
        container: str,
        node_ip: str,
        timeout: int = 5,
    ) -> bool:
        """Whether the Ray head in ``container`` on ``node_ip`` reports ready."""
        return self._ray_reports(container, node_ip, timeout, "ready")

    def _is_ray_connected(
        self,
        container: str,
        node_ip: str,
        timeout: int = 5,
    ) -> bool:
        """Whether the Ray worker in ``container`` on ``node_ip`` is connected."""
        return self._ray_reports(container, node_ip, timeout, "connected")

    def _ray_reports(
        self,
        container: str,
        node_ip: str,
        timeout: int,
        wanted: str,
    ) -> bool:
        """Run ``ray status`` on the node and look for a healthy answer."""
        try:
            result = self._service(node_ip).exec_in_container(
                container,
                ["ray", "status"],
                timeout=timeout,
            )
            if not result.ok:
                return False
            output = result.stdout.lower()
            return "ok" in output or wanted in output or "cluster" in output
        except Exception:
            return False
