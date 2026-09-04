"""Cluster orchestrator for multi-node vLLM deployments.

Orchestrates container lifecycle, Ray cluster startup, mod deployment,
and health validation across multiple nodes.

Depends on:
- A node resolver (``NodeServices``) handing out the container service bound
  to each node, so an operation aimed at a worker reaches that worker
- SSHClient for remote transport
- RayManager for Ray cluster management

Stop, status and their container listings used to pass an empty host, which
meant the control node's own Docker daemon. Now every operation names a node
and gets that node's service. Where the node set is not knowable without the
node registry — stop and status are given only a cluster name — the control
node is resolved explicitly and the caller may pass the addresses it knows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from spark_pulse.tools.cluster_health import validate_cluster
from spark_pulse.tools.cluster_models import ClusterNode, ClusterState
from spark_pulse.tools.docker import ContainerMetadata
from spark_pulse.tools.events import EventBroadcaster, EventType
from spark_pulse.tools.labels import CLUSTER_LABEL
from spark_pulse.tools.models import worker_env
from spark_pulse.tools.mods import ModDeployment as ModPayload
from spark_pulse.tools.mods import ModOrchestrator
from spark_pulse.tools.parallelism import (
    ClusterCapacity,
    NodeCapacity,
    parse_parallelism,
    validate_cluster_capacity,
)
from spark_pulse.tools.node_service import (
    Node,
    NodeService,
    NodeServices,
    control_node,
    node_for,
    run_kwargs_from_docker_config,
)
from spark_pulse.tools.ray import RayManager
from spark_pulse.tools.ssh import OpenSSHClient, SSHClient

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
        services: Callable[[Node], NodeService] | None = None,
        ssh_client: SSHClient | None = None,
        event_broadcaster: EventBroadcaster | None = None,
    ):
        """Initialize cluster orchestrator.

        Args:
            services: Resolver from node to the container service bound to it.
                Defaults to :class:`NodeServices` over ``ssh_client``.
            ssh_client: SSH transport for remote operations.
            event_broadcaster: EventBroadcaster for emitting lifecycle events.
        """
        self._ssh = ssh_client or OpenSSHClient()
        self._services = services or NodeServices(ssh_client=self._ssh)
        self._ray = RayManager(self._services)
        self._events = event_broadcaster or EventBroadcaster()

    def _service(self, address: str) -> NodeService:
        """The container service for whichever machine ``address`` names."""
        return self._services(node_for(address))

    def _image_for(self, address: str, image: str) -> str:
        """The image reference the machine at ``address`` should pull.

        The control node keeps the upstream reference — it is the node holding
        the registry credential. A worker has none, so it is pointed at the
        seeded copy in the control node's registry: same repository, same
        digest, different host, which is exactly why those three are stored
        apart rather than as one string. When the registry does not hold it,
        the reference is left alone rather than rewritten to a dead end.
        """
        from spark_pulse import tools

        if node_for(address).is_self:
            return image
        return tools.registry.node_reference(image)

    @property
    def services(self) -> Callable[[Node], NodeService]:
        """The node resolver this orchestrator hands out services from."""
        return self._services

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
            # Emit cluster starting event
            self._events.emit_cluster_event(
                EventType.CLUSTER_STARTING,
                name,
                f"Starting cluster {name} on {head_ip} + {len(worker_ips)} workers",
            )

            # 1. Validate capacity
            logger.info("Validating cluster capacity for %s", name)
            # Parse parallelism from env vars or docker config
            command = env_vars.get("COMMAND", "")
            parallelism = parse_parallelism(command)

            # Build cluster capacity from node count and GPU config
            gpu_count = docker_config.get("gpu_count", 8)
            nodes = [NodeCapacity(gpu_count=gpu_count)] * (1 + len(worker_ips))
            is_valid, message = validate_cluster_capacity(
                parallelism, ClusterCapacity(nodes=nodes)
            )
            if not is_valid:
                raise RuntimeError(f"Cluster capacity validation failed: {message}")

            # 2. Start head node
            head_name = f"{name}-head"
            head_image = self._image_for(head_ip, image)
            head_metadata = self._build_metadata(
                name, head_image, "head", no_ray=no_ray
            )
            logger.info("Starting head node %s at %s", head_name, head_ip)
            self._service(head_ip).run_container(
                image=head_image,
                name=head_name,
                env_vars=env_vars,
                metadata=head_metadata,
                **run_kwargs_from_docker_config(docker_config),
            )
            started_containers.append((head_ip, head_name))

            # Emit head container started event
            self._events.emit_cluster_event(
                EventType.HEAD_CONTAINER_STARTED,
                name,
                f"Head container started on {head_ip}",
            )

            # 3. Start worker nodes.
            #
            # A worker runs on weights that were replicated to it, so it needs
            # no hub credential — and must not be given one. Stripping the
            # token and pinning HF_HUB_OFFLINE turns "a node quietly
            # re-downloaded 400 GB over the uplink" into an immediate, legible
            # failure, and keeps the token on the control node where the
            # fetch-once design puts it.
            worker_env_vars = worker_env(env_vars)
            worker_nodes: list[ClusterNode] = []
            for i, worker_ip in enumerate(worker_ips):
                worker_name = f"{name}-worker-{i}"
                worker_image = self._image_for(worker_ip, image)
                worker_metadata = self._build_metadata(
                    name,
                    worker_image,
                    "worker",
                    head_ip=head_ip,
                    node_rank=i + 1,
                    no_ray=no_ray,
                )
                logger.info("Starting worker node %s at %s", worker_name, worker_ip)
                self._service(worker_ip).run_container(
                    image=worker_image,
                    name=worker_name,
                    env_vars=worker_env_vars,
                    metadata=worker_metadata,
                    **run_kwargs_from_docker_config(docker_config),
                )
                started_containers.append((worker_ip, worker_name))
                worker_nodes.append(
                    ClusterNode(
                        ip=worker_ip,
                        role="worker",
                        container_name=worker_name,
                        status="running",
                        gpu_count=gpu_count,
                    )
                )

                # Emit worker container started event
                self._events.emit_cluster_event(
                    EventType.WORKER_CONTAINER_STARTED,
                    name,
                    f"Worker {i} started on {worker_ip}",
                )

            # Update head node
            head_node = ClusterNode(
                ip=head_ip,
                role="head",
                container_name=head_name,
                status="running",
                gpu_count=gpu_count,
            )

            # 4. Apply mods
            applied_mods: list[str] = []
            if mod_deployments:
                logger.info("Applying %d mod deployments", len(mod_deployments))
                pending_state = ClusterState(
                    name=name,
                    head=head_node,
                    workers=worker_nodes,
                    ray_enabled=not no_ray,
                )
                applied_mods = self._apply_mods(mod_deployments, pending_state)

            # 5. Ensure Ray head
            ray_ready = False
            if not no_ray:
                ray_ready = self._ray.ensure_ray_head(
                    head_name, head_ip, port=env_vars.get("RAY_PORT", 29501)
                )
                if ray_ready:
                    self._events.emit_cluster_event(
                        EventType.RAY_HEAD_READY,
                        name,
                        "Ray head is ready",
                    )

            # 6. Ensure Ray workers
            if not no_ray:
                for i, worker_ip in enumerate(worker_ips):
                    worker_name = f"{name}-worker-{i}"
                    self._ray.ensure_ray_worker(
                        worker_name,
                        worker_ip,
                        head_ip,
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
                launch_script=launch_script,
                applied_mods=applied_mods,
            )

            validation = validate_cluster(cluster_state, self._services)
            if validation.healthy:
                self._events.emit_cluster_event(
                    EventType.CLUSTER_HEALTHY,
                    name,
                    "Cluster health check passed",
                )
            else:
                logger.warning(
                    "Cluster validation warnings/errors: %s",
                    validation.errors or validation.warnings,
                )

            # Emit cluster start complete event
            self._events.emit_cluster_event(
                EventType.CLUSTER_START_COMPLETE,
                name,
                f"Cluster {name} is ready ({cluster_state.total_nodes} nodes)",
            )

            logger.info(
                "Cluster %s started successfully (%d nodes)",
                name,
                cluster_state.total_nodes,
            )
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
            self._service(head_ip).stop_container(f"{name}-head")
        except Exception as e:
            logger.warning("Failed to stop head during rollback: %s", e)

        # Stop workers
        for i, worker_ip in enumerate(worker_ips):
            try:
                self._service(worker_ip).stop_container(f"{name}-worker-{i}")
            except Exception as e:
                logger.warning("Failed to stop worker %d during rollback: %s", i, e)

        logger.info("Cluster %s rollback complete", name)

    def stop_cluster(self, name: str, node_addresses: list[str] | None = None) -> None:
        """Stop every container of the cluster, each on its own node.

        A container is stopped on the node it was listed from, so the listing
        and the stop can no longer disagree — which is exactly what the empty
        host used to make possible.

        Args:
            name: Cluster name.
            node_addresses: Nodes to sweep. Without the node registry the
                cluster's members cannot be enumerated from a name alone, so
                the default is the control node only, resolved explicitly.
        """
        logger.info("Stopping cluster %s...", name)

        for node in self._nodes(node_addresses):
            service = self._services(node)
            for container in service.list_managed_containers({CLUSTER_LABEL: name}):
                logger.info("Stopping container %s on %s", container.name, node.label)
                service.stop_container(container.name)

        logger.info("Cluster %s stopped", name)

    def _nodes(self, node_addresses: list[str] | None) -> list[Node]:
        """Node records for the addresses given, or the control node alone."""
        if not node_addresses:
            return [control_node()]
        return [node_for(address) for address in node_addresses]

    def get_cluster_status(
        self,
        name: str,
        node_addresses: list[str] | None = None,
    ) -> ClusterState:
        """Reconstruct cluster state from Docker labels.

        1. List containers labelled spark-pulse.cluster={name} on each node
        2. Parse labels into ClusterNode objects
        3. Assemble ClusterState
        4. Check Ray status on the head node's own daemon

        Args:
            name: Cluster name.
            node_addresses: Nodes to ask. Defaults to the control node alone,
                resolved explicitly, until the node registry lands.

        Returns:
            ClusterState reconstructed from container labels.
        """
        # Each container is attributed to the node it was listed from: only
        # the head carries an address label, so a worker read off labels alone
        # inherited the head's IP and every follow-up call went to the head.
        found: list[tuple[Node, Any]] = [
            (node, container)
            for node in self._nodes(node_addresses)
            for container in self._services(node).list_managed_containers(
                {CLUSTER_LABEL: name}
            )
        ]
        containers = [container for _, container in found]

        head_node: ClusterNode | None = None
        head_address = ""
        worker_nodes: list[ClusterNode] = []

        for source, container in found:
            role = container.metadata.role
            ip = source.address or container.metadata.head_ip or container.name

            node = ClusterNode(
                ip=ip,
                role=role,
                container_name=container.name,
                container_id=container.id,
                status=(
                    "running"
                    if container.status and "running" in container.status
                    else "stopped"
                ),
            )

            if role == "head":
                head_node = node
                head_address = source.address or container.metadata.head_ip
            elif role == "worker":
                worker_nodes.append(node)

        if head_node is None:
            raise RuntimeError(f"No head node found for cluster {name}")

        # Check Ray status
        ray_ready = False
        ray_enabled = any(c.metadata.ray_enabled for c in containers)

        if ray_enabled:
            try:
                ray_status = self._service(head_address).exec_in_container(
                    head_node.container_name,
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

    def _build_metadata(
        self,
        cluster_name: str,
        image: str,
        role: str,
        head_ip: str | None = None,
        node_rank: int = 0,
        no_ray: bool = False,
    ) -> ContainerMetadata:
        """Build container metadata (i.e. spark-pulse labels) for a node."""
        return ContainerMetadata(
            deployment=cluster_name,
            recipe="",
            image=image,
            mode="cluster",
            cluster=cluster_name,
            role=role,
            node_rank=node_rank,
            head_ip=head_ip or "",
            ray_enabled=not no_ray,
        )

    def _apply_mods(
        self,
        mod_deployments: list[ModDeployment],
        cluster_state: ClusterState,
    ) -> list[str]:
        """Apply mods to the right nodes through the ModOrchestrator.

        Args:
            mod_deployments: Mods to deploy, with their target node set.
            cluster_state: The cluster the mods are applied to.

        Returns:
            Names of the mods that were applied to every target node.

        Raises:
            RuntimeError: If a mod failed on any of its target nodes. The
                caller rolls the cluster back.
        """
        orchestrator = ModOrchestrator(ssh_client=self._ssh, services=self._services)
        applied: list[str] = []

        for mod in mod_deployments:
            mod_path = Path(mod.path)
            mod_name = mod_path.name
            logger.info("Deploying mod %s (target: %s)", mod_name, mod.target)

            result = orchestrator.apply_mod_cluster(
                ModPayload(
                    mod_name=mod_name,
                    mod_path=mod_path,
                    target=mod.target,
                ),
                cluster_state,
            )
            if result.failed_nodes:
                orchestrator.rollback_mod(result, cluster_state)
                raise RuntimeError(
                    f"Mod {mod_name} failed on nodes: "
                    f"{', '.join(result.failed_nodes)}"
                )
            applied.append(mod_name)
            self._events.emit_cluster_event(
                EventType.MOD_APPLIED,
                cluster_state.name,
                f"Mod {mod_name} applied to {len(result.completed_nodes)} node(s)",
            )

        return applied
