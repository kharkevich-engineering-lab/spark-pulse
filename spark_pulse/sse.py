"""SSE (Server-Sent Events) endpoints for real-time data streaming."""

import asyncio
import json
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter
from starlette.responses import StreamingResponse

# Use factory to get correct tools (real or mock) based on SIMULATION_MODE
from spark_pulse.tools import system
from spark_pulse.tools.events import EventBroadcaster
from spark_pulse import tools

router = APIRouter(prefix="/sse", tags=["sse"])


async def metrics_generator() -> AsyncGenerator[str, None]:
    """Generate SSE events with memory metrics every 5 seconds."""
    from spark_pulse.tools.deployments import list_deployments

    while True:
        try:
            data = system.get_all_memory()
            running = [
                d
                for d in list_deployments()
                if d.get("status") in ("running", "pending")
            ]
            system.enrich_gpu_process_tracking(data.get("processes", []), running)
            yield f"event: metrics\ndata: {json.dumps(data)}\n\n"
        except Exception as e:
            yield f'event: error\ndata: {{"message": "{e}"}}\n\n'
        await asyncio.sleep(5)


@router.get("/metrics")
async def sse_metrics():
    """Stream memory metrics via SSE."""
    return StreamingResponse(
        metrics_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


async def log_generator(deployment_id: str) -> AsyncGenerator[str, None]:
    """Emit existing log lines then tail for new ones until deployment stops."""
    dep = next(
        (
            d
            for d in tools.deployments.list_deployments()
            if d.get("id") == deployment_id
        ),
        None,
    )
    if not dep:
        yield f"event: error\ndata: {json.dumps({'message': 'Deployment not found'})}\n\n"
        return

    log_path = dep.get("log_path")
    if not log_path:
        yield f"event: error\ndata: {json.dumps({'message': 'No log file for this deployment'})}\n\n"
        return

    path = Path(log_path)
    pos = 0

    # Emit existing lines first
    if path.exists():
        with open(path, errors="replace") as f:
            for line in f:
                text = line.rstrip("\n")
                if text:
                    yield f"event: log\ndata: {json.dumps({'text': text})}\n\n"
        pos = path.stat().st_size

    last_status = dep.get("status")

    # Tail for new content
    while True:
        await asyncio.sleep(0.5)

        # Stream any new lines
        if path.exists():
            size = path.stat().st_size
            if size > pos:
                with open(path, errors="replace") as f:
                    f.seek(pos)
                    for line in f:
                        text = line.rstrip("\n")
                        if text:
                            yield f"event: log\ndata: {json.dumps({'text': text})}\n\n"
                pos = size

        # Check for status changes
        dep = next(
            (
                d
                for d in tools.deployments.list_deployments()
                if d.get("id") == deployment_id
            ),
            None,
        )
        if not dep:
            break
        new_status = dep.get("status")
        if new_status != last_status:
            last_status = new_status
            yield f"event: status\ndata: {json.dumps({'status': new_status})}\n\n"
        if new_status in ("stopped", "error"):
            break


@router.get("/logs/{deployment_id}")
async def sse_logs(deployment_id: str):
    """Stream deployment logs via SSE."""
    return StreamingResponse(
        log_generator(deployment_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def models_generator() -> AsyncGenerator[str, None]:
    """Stream model catalogue events (download progress, deletions) via SSE.

    Subscribes to the shared EventBroadcaster and forwards only ``model``
    events. The running loop is registered with the models tool so download
    jobs, which run on worker threads, can emit onto it.
    """
    from spark_pulse import tools

    tools.models.register_event_loop(asyncio.get_running_loop())
    broadcaster = _get_event_broadcaster()
    queue = await broadcaster.subscribe()
    try:
        while True:
            event_data = await queue.get()
            if event_data.get("resource_type") != "model":
                continue
            yield f"data: {json.dumps(event_data)}\n\n"
    except asyncio.CancelledError:
        await broadcaster.unsubscribe(queue)


@router.get("/models")
async def sse_models():
    """Stream model download progress via SSE."""
    return StreamingResponse(
        models_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Deployment events SSE ────────────────────────────────────────────────────

# Module-level event broadcaster singleton
_event_broadcaster: EventBroadcaster | None = None


def _get_event_broadcaster() -> EventBroadcaster:
    """Get or create the default event broadcaster."""
    global _event_broadcaster
    if _event_broadcaster is None:
        _event_broadcaster = EventBroadcaster()
    return _event_broadcaster


async def deployment_events_generator() -> AsyncGenerator[str, None]:
    """Stream deployment events via SSE.

    Subscribes to the EventBroadcaster and yields events as they arrive.
    """
    broadcaster = _get_event_broadcaster()
    queue = await broadcaster.subscribe()
    try:
        while True:
            event_data = await queue.get()
            yield f"data: {json.dumps(event_data)}\n\n"
    except asyncio.CancelledError:
        await broadcaster.unsubscribe(queue)


@router.get("/events/deployments")
async def sse_deployment_events():
    """Stream deployment lifecycle events via SSE.

    Events include cluster starting, container started, Ray ready,
    health check results, and deployment completion.
    """
    return StreamingResponse(
        deployment_events_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Health SSE ───────────────────────────────────────────────────────────────


async def health_events_generator() -> AsyncGenerator[str, None]:
    """Stream health check events via SSE."""
    from spark_pulse.tools.health import get_health_monitor

    while True:
        try:
            monitor = get_health_monitor()
            deployments = monitor.get_all_health()
            yield f"event: health_update\ndata: {json.dumps(deployments)}\n\n"
        except Exception as e:
            yield f'event: error\ndata: {{"message": "{e}"}}\n\n'
        await asyncio.sleep(30)


@router.get("/health")
async def sse_health():
    """Stream health check events via SSE."""
    return StreamingResponse(
        health_events_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Cluster SSE ──────────────────────────────────────────────────────────────


async def cluster_events_generator() -> AsyncGenerator[str, None]:
    """Stream cluster lifecycle events via SSE."""
    from spark_pulse.tools.cluster import list_clusters

    last_clusters = {}
    while True:
        try:
            clusters = list_clusters()
            current_clusters = {
                c.name if hasattr(c, "name") else c.get("name", ""): c for c in clusters
            }

            # Emit events for changes
            for name, cluster in current_clusters.items():
                if name not in last_clusters:
                    # New cluster
                    yield f"event: cluster_started\ndata: {json.dumps({'name': name, 'status': 'started'})}\n\n"
                else:
                    # Check for status changes
                    old_healthy = (
                        last_clusters[name].get("healthy", False)
                        if isinstance(last_clusters[name], dict)
                        else getattr(last_clusters[name], "healthy", False)
                    )
                    new_healthy = (
                        cluster.healthy
                        if hasattr(cluster, "healthy")
                        else cluster.get("healthy", False)
                    )
                    if old_healthy != new_healthy:
                        yield f"event: cluster_health_changed\ndata: {json.dumps({'name': name, 'healthy': new_healthy})}\n\n"

            last_clusters = current_clusters
        except Exception as e:
            yield f'event: error\ndata: {{"message": "{e}"}}\n\n'
        await asyncio.sleep(15)


@router.get("/cluster")
async def sse_cluster():
    """Stream cluster lifecycle events via SSE."""
    return StreamingResponse(
        cluster_events_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
