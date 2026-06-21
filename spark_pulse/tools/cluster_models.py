"""Cluster state data models for multi-node orchestration.

Provides typed dataclasses representing cluster topology and node state.
All cluster state is derived from Docker labels — no external state database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


@dataclass(frozen=True, slots=True)
class ClusterNode:
    """Represents a single node in the cluster."""

    ip: str
    role: Literal["head", "worker"]
    container_name: str
    container_id: str | None = None
    status: Literal["starting", "running", "stopped", "error"] = "starting"
    ray_ready: bool = False
    gpu_count: int = 0

    @property
    def is_running(self) -> bool:
        """Whether the node is in running state."""
        return self.status == "running"

    @property
    def is_healthy(self) -> bool:
        """Whether the node is running and Ray is ready (if applicable)."""
        if not self.is_running:
            return False
        if self.role == "head" and not self.ray_ready:
            return False
        return True


@dataclass(frozen=True, slots=True)
class ClusterState:
    """Complete cluster state — derived from Docker labels (source of truth)."""

    name: str
    head: ClusterNode
    workers: list[ClusterNode] = field(default_factory=list)
    ray_enabled: bool = True
    ray_ready: bool = False
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @property
    def healthy(self) -> bool:
        """Whether all nodes are running and healthy."""
        if not self.head.is_healthy:
            return False
        return all(w.is_healthy for w in self.workers)

    @property
    def total_nodes(self) -> int:
        """Total number of nodes including head."""
        return 1 + len(self.workers)

    @property
    def total_gpus(self) -> int:
        """Total GPUs across all nodes."""
        return self.head.gpu_count + sum(w.gpu_count for w in self.workers)

    @property
    def is_running(self) -> bool:
        """Whether the entire cluster is running."""
        return all(n.is_running for n in [self.head, *self.workers])

    def node_by_ip(self, ip: str) -> ClusterNode | None:
        """Look up a node by its IP address."""
        if self.head.ip == ip:
            return self.head
        for w in self.workers:
            if w.ip == ip:
                return w
        return None

    def worker_containers(self) -> list[str]:
        """Return list of worker container names."""
        return [w.container_name for w in self.workers]

    def all_containers(self) -> list[str]:
        """Return list of all container names (head + workers)."""
        return [self.head.container_name, *self.worker_containers()]
