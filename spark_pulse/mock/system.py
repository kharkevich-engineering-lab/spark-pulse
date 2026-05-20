"""Mock system tools — realistic DGX Spark 128GB HBM data."""

from __future__ import annotations

from typing import Any

# Single NVIDIA Grace (GB10), 128GB unified HBM
_GPU_STATS = {
    "gpu": [
        {
            "gpu": "GPU 0",
            "memory_total": 131072,  # 128 GB in MB
            "memory_used": 89234,    # ~68% used
            "memory_free": 41838,
            "temperature": 72,
            "utilization": 45,
        }
    ],
}

_CPU_STATS = {
    "total": 131072,  # 128 GB in MB
    "used": 43520,    # ~33% used
    "free": 87552,
    "available": 92160,
    "usage_percent": 33.2,
}

_DISK_STATS = [
    {
        "mount": "/",
        "total": 1290277824000,  # ~1.2 TB
        "used": 837702287360,    # ~780 GB used
        "free": 452575536640,    # ~420 GB free
        "usage_percent": 64.9,
    },
]


def get_gpu_stats() -> list[dict[str, Any]]:
    """Return realistic DGX Spark GPU stats."""
    return list(_GPU_STATS["gpu"])


def get_cpu_stats() -> dict[str, Any]:
    """Return realistic DGX Spark CPU stats."""
    return dict(_CPU_STATS)


def get_disk_stats() -> list[dict[str, Any]]:
    """Return realistic disk stats."""
    return list(_DISK_STATS)


def get_all_memory() -> dict[str, Any]:
    """Return all memory stats."""
    return {
        "gpu": get_gpu_stats(),
        "cpu": get_cpu_stats(),
        "disk": get_disk_stats(),
    }
