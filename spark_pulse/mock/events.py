"""Mock event broadcaster for simulation mode.

Mirrors the real events.py API exactly for testing without
real deployment event broadcasting.
"""

from __future__ import annotations

from typing import Any


class MockEventBroadcaster:
    """Mock EventBroadcaster for simulation mode.

    Simulates event broadcasting without actual SSE clients.
    """

    def __init__(self):
        self._emitted_events: list[dict[str, Any]] = []

    def subscribe(self) -> Any:
        """Mock subscribe - returns a dummy queue."""
        return []

    def unsubscribe(self, queue: list) -> None:
        """Mock unsubscribe."""
        pass

    def emit(self, event: dict[str, Any]) -> None:
        """Mock emit - stores event for inspection."""
        self._emitted_events.append(event)

    def emit_cluster_event(
        self,
        event_type: str,
        cluster_name: str,
        message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mock cluster event emission."""
        self._emitted_events.append(
            {
                "type": event_type,
                "cluster": cluster_name,
                "message": message,
                "metadata": metadata or {},
            }
        )

    def emit_deployment_event(
        self,
        event_type: str,
        deployment_id: str,
        message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mock deployment event emission."""
        self._emitted_events.append(
            {
                "type": event_type,
                "deployment": deployment_id,
                "message": message,
                "metadata": metadata or {},
            }
        )

    @property
    def emitted_count(self) -> int:
        """Number of events emitted."""
        return len(self._emitted_events)

    @property
    def emitted_events(self) -> list[dict[str, Any]]:
        """All emitted events."""
        return self._emitted_events.copy()
