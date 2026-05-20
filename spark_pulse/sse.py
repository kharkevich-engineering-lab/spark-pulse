"""SSE (Server-Sent Events) endpoints for real-time data streaming."""

import asyncio
import json
from typing import AsyncGenerator

from fastapi import APIRouter
from starlette.responses import StreamingResponse

# Use factory to get correct tools (real or mock) based on SIMULATION_MODE
from spark_pulse.tools import system

router = APIRouter(prefix="/sse", tags=["sse"])


async def metrics_generator() -> AsyncGenerator[str, None]:
    """Generate SSE events with memory metrics every 5 seconds."""
    while True:
        try:
            data = system.get_all_memory()
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
