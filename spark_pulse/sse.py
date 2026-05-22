"""SSE (Server-Sent Events) endpoints for real-time data streaming."""

import asyncio
import json
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter
from starlette.responses import StreamingResponse

# Use factory to get correct tools (real or mock) based on SIMULATION_MODE
from spark_pulse.tools import system
from spark_pulse import tools

router = APIRouter(prefix="/sse", tags=["sse"])


async def metrics_generator() -> AsyncGenerator[str, None]:
    """Generate SSE events with memory metrics every 5 seconds."""
    from spark_pulse.tools.deployments import list_deployments
    while True:
        try:
            data = system.get_all_memory()
            running_pids: set[int] = {
                dep["pid"]
                for dep in list_deployments()
                if dep.get("pid") and dep.get("status") in ("running", "pending")
            }
            for proc in data.get("processes", []):
                proc["is_tracked"] = proc["pid"] in running_pids
            yield f"event: metrics\ndata: {json.dumps(data)}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {{\"message\": \"{e}\"}}\n\n"
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
    dep = next((d for d in tools.deployments.list_deployments() if d.get("id") == deployment_id), None)
    if not dep:
        yield f'event: error\ndata: {json.dumps({"message": "Deployment not found"})}\n\n'
        return

    log_path = dep.get("log_path")
    if not log_path:
        yield f'event: error\ndata: {json.dumps({"message": "No log file for this deployment"})}\n\n'
        return

    path = Path(log_path)
    pos = 0

    # Emit existing lines first
    if path.exists():
        with open(path, errors="replace") as f:
            for line in f:
                text = line.rstrip("\n")
                if text:
                    yield f'event: log\ndata: {json.dumps({"text": text})}\n\n'
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
                            yield f'event: log\ndata: {json.dumps({"text": text})}\n\n'
                pos = size

        # Check for status changes
        dep = next((d for d in tools.deployments.list_deployments() if d.get("id") == deployment_id), None)
        if not dep:
            break
        new_status = dep.get("status")
        if new_status != last_status:
            last_status = new_status
            yield f'event: status\ndata: {json.dumps({"status": new_status})}\n\n'
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
