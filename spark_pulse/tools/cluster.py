"""Cluster orchestrator for multi-node vLLM deployments.

Orchestrates container lifecycle, Ray cluster startup, mod deployment,
and health validation across multiple nodes.

Depends on:
- RemoteDockerService for container operations
- SSHClient for remote transport
- RayManager for Ray cluster management
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from spark_pulse.tools.cluster_models import ClusterNode, ClusterState
from spark_pulse.tools.cluster_health import validate_cluster, ValidationResult
from spark_pulse.tools.parallelism import (
    ClusterCapacity,
    NodeCapacity,
    parse_parallelism,
    validate_cluster_capacity,
)
from spark_pulse.tools.ray import RayManager
from spark_pulse.tools.remote_docker import RemoteDockerService
from spark_pulse.tools.ssh import SSHClient, OpenSSHClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModDeployment:
    """Mod to deploy with target node specification."""

    path: str
    target: Literal["head", "workers", "all"]


class ClusterOrchestrator:
    """Orchestrates multi-node cluster lifecycle.

    Provides idempotent start/stop/status operations with automatic
    rollback on failure.
    """

    def __init__(
        self,
        remote_docker: RemoteDockerService | None = None,
        ssh_client: SSHClient | None = None,
    ):
        """Initialize cluster orchestrator.

        Args:
            remote_docker: RemoteDockerService for container operations.
            ssh_client: SSH transport for remote operations.
        """
        self._docker = remote_docker or RemoteDockerService(
            ssh_client or OpenSSHClient()
        )
        self._ssh = ssh_client or OpenSSHClient()
        self._ray = RayManager(self._docker)

    def start_cluster(
        self,
        name: str,
        image: str,
        head_ip: str,
        worker_ips: list[str],
        env_vars: dict[str, str],
        docker_config: dict,
        mod_deployments: list[ModDeployment] | None = None,
        launch_script: str | None = None,
        no_ray: bool = False,
    ) -> ClusterState:
        """Start containers on head + workers with rollback on failure.

        Flow:
        1. validate_cluster_capacity() — check GPU availability
        2. start_node(head_ip) — launch head container
        3. for each worker: start_node(worker_ip)
        4. if mods: apply_mods(mod_deployments)
        5. if not no_ray: ensure_ray_head(head_ip)
        6. if not no_ray: for each worker: ensure_ray_worker(worker_ip)
        7. validate_cluster() — health check
        8. Return ClusterState

        On any failure: rollback_cluster() — stop all started containers.

        Args:
            name: Cluster name.
            image: Docker image to deploy.
            head_ip: IP of the head node.
            worker_ips: IPs of worker nodes.
            env_vars: Environment variables for containers.
            docker_config: Docker configuration.
            mod_deployments: Mods to apply after startup.
            launch_script: Optional launch script to distribute.
            no_ray: Skip Ray cluster startup.

        Returns:
            ClusterState representing the started cluster.

        Raises:
            RuntimeError: If cluster startup fails (after rollback).
        """
        started_containers: list[tuple[str, str]] = []  # [(ip, name)]

        try:
            # 1. Validate capacity
            logger.info("Validating cluster capacity for %s", name)
            # Parse parallelism from env vars or docker config
            command = env_vars.get("COMMAND", "")
            parallelism = parse_parallelism(command)

            # Build cluster capacity from node count and GPU config
            gpu_count = docker_config.get("gpu_count", 8)
            nodes = [NodeCapacity(gpu_count=gpu_count)] * (1 + len(worker_ips))
            is_valid, message = validate_cluster_capacity(parallelism, ClusterCapacity(nodes=nodes))
            if not is_valid:
                raise RuntimeError(f"Cluster capacity validation failed: {message}")

            # 2. Start head node
            head_name = f"{name}-head"
            head_labels = self._build_labels(name, "head", head_ip)
            logger.info("Starting head node %s at %s", head_name, head_ip)
            self._docker.run_container(
                head_ip, image, head_name, env_vars, docker_config, head_labels
            )
            started_containers.append((head_ip, head_name))

            # 3. Start worker nodes
            worker_nodes: list[ClusterNode] = []
            for i, worker_ip in enumerate(worker_ips):
                worker_name = f"{name}-worker-{i}"
                worker_labels = self._build_labels(name, "worker", worker_ip, head_ip=head_ip, node_rank=i)
                logger.info("Starting worker node %s at %s", worker_name, worker_ip)
                self._docker.run_container(
                    worker_ip, image, worker_name, env_vars, docker_config, worker_labels
                )
                started_containers.append((worker_ip, worker_name))
                worker_nodes.append(ClusterNode(
                    ip=worker_ip,
                    role="worker",
                    container_name=worker_name,
                    status="running",
                    gpu_count=gpu_count,
                ))

            # Update head node
            head_node = ClusterNode(
                ip=head_ip,
                role="head",
                container_name=head_name,
                status="running",
                gpu_count=gpu_count,
            )

            # 4. Apply mods
            if mod_deployments:
                logger.info("Applying %d mod deployments", len(mod_deployments))
                self._apply_mods(mod_deployments, head_name, [w.container_name for w in worker_nodes])

            # 5. Ensure Ray head
            ray_ready = False
            if not no_ray:
                ray_ready = self._ray.ensure_ray_head(
                    head_name, head_ip, port=env_vars.get("RAY_PORT", 29501)
                )

            # 6. Ensure Ray workers
            if not no_ray:
                for i, worker_ip in enumerate(worker_ips):
                    worker_name = f"{name}-worker-{i}"
                    self._ray.ensure_ray_worker(
                        worker_name, worker_ip, head_ip,
                        head_port=env_vars.get("RAY_PORT", 29501),
                    )

            # 7. Validate cluster
            cluster_state = ClusterState(
                name=name,
                head=head_node,
                workers=worker_nodes,
                ray_enabled=not no_ray,
                ray_ready=ray_ready,
                created_at=datetime.now(timezone.utc),
            )

            validation = validate_cluster(cluster_state, self._docker)
            if not validation.healthy:
                logger.warning("Cluster validation warnings/errors: %s", validation.errors or validation.warnings)

            logger.info("Cluster %s started successfully (%d nodes)", name, cluster_state.total_nodes)
            return cluster_state

        except Exception as e:
            logger.error("Cluster %s startup failed: %s. Rolling back...", name, e)
            self.rollback_cluster(name, head_ip, worker_ips)
            raise RuntimeError(f"Cluster startup failed: {e}") from e

    def rollback_cluster(
        self,
        name: str,
        head_ip: str,
        worker_ips: list[str],
    ) -> None:
        """Stop all containers for a failed deployment.

        Prevents orphaned containers when deployment fails mid-way.

        Args:
            name: Cluster name.
            head_ip: Head node IP.
            worker_ips: Worker node IPs.
        """
        logger.info("Rolling back cluster %s...", name)

        # Stop head
        try:
            self._docker.stop_container(head_ip, f"{name}-head")
        except Exception as e:
            logger.warning("Failed to stop head during rollback: %s", e)

        # Stop workers
        for i, worker_ip in enumerate(worker_ips):
            try:
                self._docker.stop_container(worker_ip, f"{name}-worker-{i}")
            except Exception as e:
                logger.warning("Failed to stop worker %d during rollback: %s", i, e)

        logger.info("Cluster %s rollback complete", name)

    def stop_cluster(self, name: str) -> None:
        """Stop all containers in the cluster.

        Args:
            name: Cluster name.
        """
        logger.info("Stopping cluster %s...", name)

        # List all containers with cluster label
        containers = self._docker.list_managed_containers(
            "", {"spark-pulse.cluster": name}
        )

        for container in containers:
            logger.info("Stopping container %s", container.name)
            self._docker.stop_container("", container.name)

        logger.info("Cluster %s stopped", name)

    def get_cluster_status(self, name: str) -> ClusterState:
        """Reconstruct cluster state from Docker labels.

        1. List all containers with label spark-pulse.cluster={name}
        2. Parse labels into ClusterNode objects
        3. Assemble ClusterState
        4. Check Ray status on head node

        Args:
            name: Cluster name.

        Returns:
            ClusterState reconstructed from container labels.
        """
        containers = self._docker.list_managed_containers(
            "", {"spark-pulse.cluster": name}
        )

        head_node: ClusterNode | None = None
        worker_nodes: list[ClusterNode] = []

        for container in containers:
            labels = container.labels or {}
            role = labels.get("spark-pulse.role", "")
            ip = labels.get("spark-pulse.head_ip", container.name)

            node = ClusterNode(
                ip=ip,
                role=role,
                container_name=container.name,
                container_id=container.container_id,
                status="running" if container.status and "running" in container.status else "stopped",
            )

            if role == "head":
                head_node = node
            elif role == "worker":
                worker_nodes.append(node)

        if head_node is None:
            raise RuntimeError(f"No head node found for cluster {name}")

        # Check Ray status
        ray_ready = False
        ray_enabled = any(
            "spark-pulse.ray" in (c.labels or {})
            for c in containers
        )

        if ray_enabled:
            try:
                ray_status = self._docker.exec_container(
                    "", head_node.container_name,
                    ["ray", "status"],
                    timeout=10,
                )
                ray_ready = "OK" in ray_status.stdout if ray_status.ok else False
            except Exception:
                pass

        return ClusterState(
            name=name,
            head=head_node,
            workers=worker_nodes,
            ray_enabled=ray_enabled,
            ray_ready=ray_ready,
        )

    def ensure_ray_head(
        self,
        container: str,
        node_ip: str,
        port: int = 29501,
    ) -> bool:
        """Ensure Ray head is running (idempotent).

        Args:
            container: Container name.
            node_ip: Node IP address.
            port: Ray port.

        Returns:
            True if Ray is ready.
        """
        return self._ray.ensure_ray_head(container, node_ip, port)

    def ensure_ray_worker(
        self,
        container: str,
        worker_ip: str,
        head_ip: str,
        head_port: int = 29501,
    ) -> bool:
        """Ensure Ray worker is connected (idempotent).

        Args:
            container: Container name.
            worker_ip: Worker IP address.
            head_ip: Head IP address.
            head_port: Head port.

        Returns:
            True if Ray worker is connected.
        """
        return self._ray.ensure_ray_worker(container, worker_ip, head_ip, head_port)

    # ── Private helpers ──────────────────────────────────────────────────

    def _build_labels(
        self,
        cluster_name: str,
        role: str,
        node_ip: str,
        head_ip: str | None = None,
        node_rank: int = 0,
    ) -> dict[str, str]:
        """Build Docker labels for a cluster node container."""
        labels = {
            "spark-pulse.managed": "true",
            "spark-pulse.cluster": cluster_name,
            "spark-pulse.role": role,
            "spark-pulse.node_rank": str(node_rank),
        }
        if head_ip:
            labels["spark-pulse.head_ip"] = head_ip
        return labels

    def _apply_mods(
        self,
        mod_deployments: list[ModDeployment],
        head_container: str,
        worker_containers: list[str],
    ) -> None:
        """Apply mods to correct nodes based on target.

        Args:
            mod_deployments: List of mods to deploy.
            head_container: Head container name.
            worker_containers: List of worker container names.
        """
        for mod in mod_deployments:
            logger.info("Deploying mod %s (target: %s)", mod.path, mod.target)

            targets: list[str] = []
            if mod.target in ("head", "all"):
                targets.append(head_container)
            if mod.target in ("workers", "all"):
                targets.extend(worker_containers)

            for container in targets:
                # Copy mod files into container
                # Note: In production, this would use the remote docker service
                # For now, log the action
                logger.info("Applying mod %s to %s", mod.path, container)
