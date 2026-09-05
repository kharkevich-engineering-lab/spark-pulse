"""Mock system tools — delegates to real system parsing for test compatibility.

In simulation mode, system stats are returned as-is. The parsing functions
delegate to the real implementation so tests that mock subprocess.getoutput
work correctly.
"""

from __future__ import annotations

from typing import Any

# Import real system module for test compatibility
# This allows parsing tests to work when subprocess is mocked

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

# ── Delegation functions ───────────────────────────────────────────────────
# These delegate to the real implementation so that subprocess-mocked tests
# work correctly.  The real module is only imported lazily to avoid importing
# things that are unavailable in the test environment when simulating.


def _resolve_module() -> Any:
    """Return the real system module, loading it lazily."""
    import importlib

    return importlib.import_module("spark_pulse.tools.system")


def get_gpu_stats() -> list[dict[str, Any]]:
    """Return GPU stats by delegating to the real implementation."""
    return _resolve_module().get_gpu_stats()


def get_gpu_process_stats() -> list[dict[str, Any]]:
    """Return GPU process stats by delegating to the real implementation."""
    return _resolve_module().get_gpu_process_stats()


def get_cpu_stats() -> dict[str, Any]:
    """Return CPU stats by delegating to the real implementation."""
    return _resolve_module().get_cpu_stats()


def get_disk_stats() -> list[dict[str, Any]]:
    """Return disk stats by delegating to the real implementation."""
    return _resolve_module().get_disk_stats()


def kill_gpu_process(pid: int) -> dict[str, Any]:
    """Kill a GPU process by delegating to the real implementation."""
    return _resolve_module().kill_gpu_process(pid)


def get_all_memory() -> dict[str, Any]:
    """Return all memory info by delegating to the real implementation."""
    return _resolve_module().get_all_memory()


def enrich_gpu_process_tracking(
    process_list: list[dict[str, Any]],
    running_deployments: list[dict[str, Any]],
) -> None:
    """Mark processes that correspond to running deployments.

    A deployment record carries ``pid: None`` until its container reports one,
    and a record written by an older build may not carry the key at all — the
    real implementation guards for both, so this one does too rather than
    turning the memory endpoint into a 500.
    """
    running_pids = {d["pid"] for d in running_deployments if d.get("pid")}
    for proc in process_list:
        proc["is_tracked"] = proc.get("pid") in running_pids
