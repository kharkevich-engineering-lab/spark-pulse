"""Mock cluster orchestrator for simulation mode.

Mirrors the real cluster.py API exactly for testing without
real Docker, SSH, or Ray access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from spark_pulse.mock.ray import MockRayManager
from spark_pulse.mock.remote_docker import MockRemoteDockerService
from spark_pulse.tools.cluster_models import ClusterNode, ClusterState


@dataclass(frozen=True, slots=True)
class ModDeployment:
    """Mod to deploy with target node specification."""

    path: str
    target: Literal["head", "workers", "all"]


class MockClusterOrchestrator:
    """Mock ClusterOrchestrator for simulation mode.

    Simulates multi-node cluster lifecycle without real infrastructure.
    """

    def __init__(
        self,
        scenario: str = "default",
        ray_ready: bool = True,
        fail_containers: list[str] | None = None,
    ):
        """Initialize mock cluster orchestrator.

        Args:
            scenario: Simulation scenario ("default", "failed", "partial").
            ray_ready: Whether Ray operations succeed.
            fail_containers: Containers that should fail operations.
        """
        self._scenario = scenario
        self._ray_ready = ray_ready
        self._docker = MockRemoteDockerService(scenario=scenario)
        self._ray = MockRayManager(
            ready=ray_ready,
            fail_containers=fail_containers or [],
        )
        self._clusters: dict[str, ClusterState] = {}
        self._executed_operations: list[dict[str, Any]] = []

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
        """Start cluster (mocked)."""
        self._executed_operations.append(
            {
                "action": "start_cluster",
                "name": name,
                "image": image,
                "head_ip": head_ip,
                "worker_ips": worker_ips,
            }
        )

        gpu_count = docker_config.get("gpu_count", 8)
        head_node = ClusterNode(
            ip=head_ip,
            role="head",
            container_name=f"{name}-head",
            status="running" if self._scenario != "failed" else "error",
            gpu_count=gpu_count,
            ray_ready=not no_ray and self._ray_ready,
        )

        worker_nodes = []
        for i, worker_ip in enumerate(worker_ips):
            worker_nodes.append(
                ClusterNode(
                    ip=worker_ip,
                    role="worker",
                    container_name=f"{name}-worker-{i}",
                    status="running" if self._scenario != "failed" else "error",
                    gpu_count=gpu_count,
                    ray_ready=not no_ray and self._ray_ready,
                )
            )

        cluster_state = ClusterState(
            name=name,
            head=head_node,
            workers=worker_nodes,
            ray_enabled=not no_ray,
            ray_ready=not no_ray and self._ray_ready,
            created_at=datetime.now(timezone.utc),
        )

        self._clusters[name] = cluster_state
        return cluster_state

    def rollback_cluster(
        self,
        name: str,
        head_ip: str,
        worker_ips: list[str],
    ) -> None:
        """Rollback cluster (mocked)."""
        self._executed_operations.append(
            {
                "action": "rollback_cluster",
                "name": name,
                "head_ip": head_ip,
                "worker_ips": worker_ips,
            }
        )

    def stop_cluster(self, name: str) -> None:
        """Stop cluster (mocked)."""
        self._executed_operations.append(
            {
                "action": "stop_cluster",
                "name": name,
            }
        )

        if name in self._clusters:
            # Create new state with stopped nodes (ClusterNode is frozen)
            state = self._clusters[name]
            stopped_head = ClusterNode(
                ip=state.head.ip,
                role=state.head.role,
                container_name=state.head.container_name,
                status="stopped",
                ray_ready=state.head.ray_ready,
                gpu_count=state.head.gpu_count,
            )
            stopped_workers = [
                ClusterNode(
                    ip=w.ip,
                    role=w.role,
                    container_name=w.container_name,
                    status="stopped",
                    ray_ready=w.ray_ready,
                    gpu_count=w.gpu_count,
                )
                for w in state.workers
            ]
            self._clusters[name] = ClusterState(
                name=state.name,
                head=stopped_head,
                workers=stopped_workers,
                ray_enabled=state.ray_enabled,
                ray_ready=state.ray_ready,
                created_at=state.created_at,
            )

    def get_cluster_status(self, name: str) -> ClusterState:
        """Get cluster status (mocked)."""
        self._executed_operations.append(
            {
                "action": "get_cluster_status",
                "name": name,
            }
        )

        if name in self._clusters:
            return self._clusters[name]

        raise RuntimeError(f"Cluster {name} not found")

    def ensure_ray_head(
        self,
        container: str,
        node_ip: str,
        port: int = 29501,
    ) -> bool:
        """Ensure Ray head (mocked)."""
        return self._ray.ensure_ray_head(container, node_ip, port)

    def ensure_ray_worker(
        self,
        container: str,
        worker_ip: str,
        head_ip: str,
        head_port: int = 29501,
    ) -> bool:
        """Ensure Ray worker (mocked)."""
        return self._ray.ensure_ray_worker(container, worker_ip, head_ip, head_port)

    @property
    def docker(self) -> MockRemoteDockerService:
        """Return the mock Docker service."""
        return self._docker

    @property
    def ray(self) -> MockRayManager:
        """Return the mock Ray manager."""
        return self._ray

    @property
    def executed_operations(self) -> list[dict[str, Any]]:
        """Return list of all executed operations."""
        return self._executed_operations.copy()

    @property
    def clusters(self) -> dict[str, ClusterState]:
        """Return dict of managed clusters."""
        return self._clusters.copy()

    def reset(self) -> None:
        """Clear all state."""
        self._executed_operations.clear()
        self._clusters.clear()
        self._docker.reset()
        self._ray.reset()
