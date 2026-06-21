"""Deployment event system for structured lifecycle tracking.

Provides typed events and an SSE-compatible broadcaster for real-time
deployment progress tracking in the frontend.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """Structured deployment event types."""

    # Cluster lifecycle
    CLUSTER_STARTING = "cluster_starting"
    HEAD_CONTAINER_STARTED = "head_container_started"
    WORKER_CONTAINER_STARTED = "worker_container_started"
    RAY_HEAD_READY = "ray_head_ready"
    RAY_WORKER_READY = "ray_worker_ready"
    MOD_APPLIED = "mod_applied"
    LAUNCH_SCRIPT_DEPLOYED = "launch_script_deployed"
    CLUSTER_HEALTHY = "cluster_healthy"
    CLUSTER_START_COMPLETE = "cluster_start_complete"

    CLUSTER_STOPPING = "cluster_stopping"
    CLUSTER_STOP_COMPLETE = "cluster_stop_complete"

    CLUSTER_ROLLBACK_STARTED = "cluster_rollback_started"
    CLUSTER_ROLLBACK_COMPLETED = "cluster_rollback_completed"

    # Deployment lifecycle
    DEPLOYMENT_STARTING = "deployment_starting"
    DEPLOYMENT_STARTED = "deployment_started"
    DEPLOYMENT_STOPPED = "deployment_stopped"
    DEPLOYMENT_ERROR = "deployment_error"

    # Health
    HEALTH_CHECK_FAILED = "health_check_failed"
    HEALTH_CHECK_RECOVERED = "health_check_recovered"


@dataclass(frozen=True, slots=True)
class DeploymentEvent:
    """Structured deployment event."""

    event_type: EventType
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    resource: str = ""  # cluster name or deployment id
    resource_type: str = ""  # "cluster" or "deployment"
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for SSE broadcast."""
        return {
            "type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "resource": self.resource,
            "resource_type": self.resource_type,
            "message": self.message,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeploymentEvent:
        """Deserialize from dict."""
        return cls(
            event_type=EventType(data["type"]),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            resource=data.get("resource", ""),
            resource_type=data.get("resource_type", ""),
            message=data.get("message", ""),
            metadata=data.get("metadata", {}),
        )


class EventBroadcaster:
    """Broadcasts deployment events to SSE clients."""

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        """Subscribe to events. Returns a queue to read from."""
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._subscribers.append(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Unsubscribe from events."""
        async with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    async def emit(self, event: DeploymentEvent) -> None:
        """Broadcast event to all subscribers."""
        data = event.to_dict()
        async with self._lock:
            for queue in self._subscribers:
                try:
                    queue.put_nowait(data)
                except asyncio.QueueFull:
                    logger.warning("Event queue full, dropping event")

    async def emit_cluster_event(
        self,
        event_type: EventType,
        cluster_name: str,
        message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Convenience method for cluster events."""
        await self.emit(DeploymentEvent(
            event_type=event_type,
            resource=cluster_name,
            resource_type="cluster",
            message=message,
            metadata=metadata or {},
        ))

    async def emit_deployment_event(
        self,
        event_type: EventType,
        deployment_id: str,
        message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Convenience method for deployment events."""
        await self.emit(DeploymentEvent(
            event_type=event_type,
            resource=deployment_id,
            resource_type="deployment",
            message=message,
            metadata=metadata or {},
        ))

    @property
    def subscriber_count(self) -> int:
        """Number of active subscribers."""
        return len(self._subscribers)
