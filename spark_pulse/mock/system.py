"""Mock system tools — realistic DGX Spark 128GB HBM data."""

from __future__ import annotations

from typing import Any

# Single NVIDIA Grace (GB10), 128GB unified HBM
_GPU_STATS = {
    "gpu": [
        {
            "index": 0,
            "gpu": "GPU 0",
            "uuid": "GPU-00000000-0000-0000-0000-000000000000",
            "name": "NVIDIA GB10",
            "memory_total": 131072,  # 128 GB in MB
            "memory_used": 89234,  # ~68% used
            "memory_free": 41838,
            "temperature": 72,
            "utilization": 45,
            "power_draw": 10,
            "power_limit": None,
        }
    ],
}

_GPU_PROCESSES = [
    {
        "gpu_uuid": "GPU-00000000-0000-0000-0000-000000000000",
        "pid": 98251,
        "process_name": "VLLM::EngineCore",
        "used_memory": 83421,
    },
]

_CPU_STATS = {
    "total": 131072,  # 128 GB in MB
    "used": 43520,  # ~33% used
    "free": 87552,
    "available": 92160,
    "usage_percent": 33.2,
}

_DISK_STATS = [
    {
        "mount": "/",
        "total": 1290277824000,  # ~1.2 TB
        "used": 837702287360,  # ~780 GB used
        "free": 452575536640,  # ~420 GB free
        "usage_percent": 64.9,
    },
]


def enrich_gpu_process_tracking(
    processes: list[dict[str, Any]],
    running_deployments: list[dict[str, Any]],
) -> None:
    """Mark mock GPU processes as tracked when they belong to running deployments.

    Simulation mode uses synthetic processes, so we fall back to matching by PID.
    """
    tracked_pids = {int(dep["pid"]) for dep in running_deployments if dep.get("pid")}
    for process in processes:
        process["is_tracked"] = int(process.get("pid", -1)) in tracked_pids


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
        "processes": list(_GPU_PROCESSES),
    }
