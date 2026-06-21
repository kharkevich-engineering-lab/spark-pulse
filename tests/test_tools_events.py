"""Tests for the deployment event system."""

from __future__ import annotations

import pytest

from spark_pulse.tools.events import (
    DeploymentEvent,
    EventBroadcaster,
    EventType,
)


class TestDeploymentEvent:
    def test_default_values(self):
        event = DeploymentEvent(event_type=EventType.CLUSTER_STARTING)
        assert event.resource == ""
        assert event.resource_type == ""
        assert event.message == ""
        assert event.metadata == {}

    def test_full_values(self):
        event = DeploymentEvent(
            event_type=EventType.CLUSTER_START_COMPLETE,
            resource="test-cluster",
            resource_type="cluster",
            message="Cluster is ready",
            metadata={"nodes": 4},
        )
        assert event.resource == "test-cluster"
        assert event.resource_type == "cluster"
        assert event.message == "Cluster is ready"
        assert event.metadata == {"nodes": 4}

    def test_to_dict(self):
        event = DeploymentEvent(
            event_type=EventType.DEPLOYMENT_STARTED,
            resource="dep-1",
            resource_type="deployment",
            message="Started",
        )
        d = event.to_dict()
        assert d["type"] == "deployment_started"
        assert d["resource"] == "dep-1"
        assert d["resource_type"] == "deployment"
        assert d["message"] == "Started"

    def test_from_dict(self):
        d = {
            "type": "cluster_healthy",
            "timestamp": "2024-01-01T00:00:00+00:00",
            "resource": "test-cluster",
            "resource_type": "cluster",
            "message": "All nodes healthy",
            "metadata": {},
        }
        event = DeploymentEvent.from_dict(d)
        assert event.event_type == EventType.CLUSTER_HEALTHY
        assert event.resource == "test-cluster"
        assert event.message == "All nodes healthy"


class TestEventBroadcaster:
    @pytest.fixture
    def broadcaster(self):
        return EventBroadcaster()

    @pytest.mark.asyncio
    async def test_subscribe_unsubscribe(self, broadcaster):
        queue = await broadcaster.subscribe()
        assert broadcaster.subscriber_count == 1
        await broadcaster.unsubscribe(queue)
        assert broadcaster.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_emit_to_subscriber(self, broadcaster):
        queue = await broadcaster.subscribe()
        event = DeploymentEvent(
            event_type=EventType.CLUSTER_STARTING,
            resource="test-cluster",
            message="Starting",
        )
        await broadcaster.emit(event)
        data = await queue.get()
        assert data["type"] == "cluster_starting"
        assert data["resource"] == "test-cluster"

    @pytest.mark.asyncio
    async def test_emit_cluster_event(self, broadcaster):
        queue = await broadcaster.subscribe()
        await broadcaster.emit_cluster_event(
            EventType.HEAD_CONTAINER_STARTED,
            "my-cluster",
            "Head started",
        )
        data = await queue.get()
        assert data["type"] == "head_container_started"
        assert data["resource"] == "my-cluster"
        assert data["resource_type"] == "cluster"

    @pytest.mark.asyncio
    async def test_emit_deployment_event(self, broadcaster):
        queue = await broadcaster.subscribe()
        await broadcaster.emit_deployment_event(
            EventType.DEPLOYMENT_STARTED,
            "dep-1",
            "Deployment started",
        )
        data = await queue.get()
        assert data["type"] == "deployment_started"
        assert data["resource"] == "dep-1"
        assert data["resource_type"] == "deployment"

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self, broadcaster):
        queue1 = await broadcaster.subscribe()
        queue2 = await broadcaster.subscribe()

        event = DeploymentEvent(
            event_type=EventType.CLUSTER_HEALTHY,
            resource="test-cluster",
        )
        await broadcaster.emit(event)

        data1 = await queue1.get()
        data2 = await queue2.get()
        assert data1["type"] == data2["type"] == "cluster_healthy"
