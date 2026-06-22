"""Mock system tools — delegates to real system parsing for test compatibility.

In simulation mode, system stats are returned as-is. The parsing functions
delegate to the real implementation so tests that mock subprocess.getoutput
work correctly.
"""

from __future__ import annotations

from typing import Any

# Import real system module for test compatibility
from spark_pulse.tools.system import (
    get_gpu_stats,
    get_gpu_process_stats,
    get_cpu_stats,
    get_disk_stats,
    kill_gpu_process,
    get_all_memory,
    enrich_gpu_process_tracking,
)

# Pre-canned mock data (used when subprocess.getoutput is NOT mocked)
_GPU_STATS = [
    {
        "index": 0,
        "gpu": "GPU 0",
        "uuid": "GPU-00000000-0000-0000-0000-000000000000",
        "name": "NVIDIA GB10",
        "memory_total": 131072,
        "memory_used": 89234,
        "memory_free": 41838,
        "temperature": 72,
        "utilization": 45,
        "power_draw": 10,
        "power_limit": None,
    }
]

_GPU_PROCESSES = [
    {
        "gpu_uuid": "GPU-00000000-0000-0000-0000-000000000000",
        "pid": 98251,
        "process_name": "VLLM::EngineCore",
        "used_memory": 83421,
    },
]

_CPU_STATS = {
    "total": 131072,
    "used": 43520,
    "free": 87552,
    "available": 92160,
    "usage_percent": 33.2,
}

_DISK_STATS = [
    {
        "mount": "/",
        "total": 1290277824000,
        "used": 837702287360,
        "free": 452575536640,
        "usage_percent": 64.9,
    },
]
