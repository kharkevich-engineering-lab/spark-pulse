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
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/sse", tags=["sse"])


async def metrics_generator() -> AsyncGenerator[str, None]:
    """Generate SSE events with memory metrics every 5 seconds."""

    def _collect() -> dict:
        # nvidia-smi and the deployment store are both blocking; on the loop
        # they stall every other stream in the process for their duration.
        data = system.get_all_memory()
        running = [
            d
            for d in tools.deployment_records.load()
            if d.get("status") in ("running", "pending")
        ]
        system.enrich_gpu_process_tracking(data.get("processes", []), running)
        return data

    while True:
        try:
            data = await run_in_threadpool(_collect)
            yield f"event: metrics\ndata: {json.dumps(data)}\n\n"
        except Exception as e:
            # Interpolating the message straight into the frame produced
            # unparseable JSON the moment it held a quote, and split the frame
            # in two the moment it held a newline — which docker and nvidia-smi
            # messages routinely do.
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
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


async def _native_log_generator(deployment_id: str) -> AsyncGenerator[str, None]:
    """Poll ``docker logs`` for a native deployment and emit the new lines.

    A native deployment writes to its container's stdout, not to a file, so the
    tail is a diff against what was already sent rather than a file offset.
    """
    seen = 0
    last_status: str | None = None
    while True:
        # Both calls below reach docker, and over SSH for a remote node.
        text = await run_in_threadpool(
            tools.deploy_dispatch.get_logs, deployment_id, 1000
        )
        lines = [line for line in text.splitlines() if line]
        for line in lines[seen:]:
            yield f"event: log\ndata: {json.dumps({'text': line})}\n\n"
        seen = max(seen, len(lines))

        dep = await run_in_threadpool(
            tools.deploy_dispatch.get_deployment, deployment_id
        )
        if not dep:
            break
        status = dep.get("status")
        if status != last_status:
            last_status = status
            yield f"event: status\ndata: {json.dumps({'status': status})}\n\n"
        if status in ("stopped", "error"):
            break
        await asyncio.sleep(2)


async def log_generator(deployment_id: str) -> AsyncGenerator[str, None]:
    """Emit existing log lines then tail for new ones until deployment stops."""
    dep = next(
        (
            d
            for d in tools.deploy_dispatch.list_deployments()
            if d.get("id") == deployment_id
        ),
        None,
    )
    if not dep:
        yield f"event: error\ndata: {json.dumps({'message': 'Deployment not found'})}\n\n"
        return

    if dep.get("runtime") == "native":
        async for chunk in _native_log_generator(deployment_id):
            yield chunk
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
                for d in tools.deploy_dispatch.list_deployments()
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


async def images_generator() -> AsyncGenerator[str, None]:
    """Stream engine image events (pull progress, deletions, syncs) via SSE.

    Same shape as ``/sse/models``: subscribe to the shared broadcaster, forward
    only ``image`` events, and hand the running loop to the images tool so pull
    jobs on worker threads can emit onto it.

    Image pulls that happen *inside* a deploy are deployment-scoped and reach
    ``/sse/events/deployments`` instead — they belong to that deployment's
    timeline, not to the catalogue.
    """
    from spark_pulse import tools

    tools.images.register_event_loop(asyncio.get_running_loop())
    broadcaster = _get_event_broadcaster()
    queue = await broadcaster.subscribe()
    try:
        while True:
            event_data = await queue.get()
            if event_data.get("resource_type") != "image":
                continue
            yield f"data: {json.dumps(event_data)}\n\n"
    except asyncio.CancelledError:
        await broadcaster.unsubscribe(queue)


@router.get("/images")
async def sse_images():
    """Stream engine image pull progress via SSE."""
    return StreamingResponse(
        images_generator(),
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
    # Native deploys run on request and worker threads; hand them this loop so
    # their lifecycle events reach the broadcaster.
    tools.native_runtime.register_event_loop(asyncio.get_running_loop())
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

    Events include the plan, the image pull, each container starting, mods
    applied, readiness and completion or failure.
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
