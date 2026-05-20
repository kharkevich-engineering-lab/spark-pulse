"""Real system tools — nvidia-smi, free, df parsing."""

import subprocess
from typing import Any


def get_gpu_stats() -> list[dict[str, Any]]:
    """Parse nvidia-smi output for GPU memory, temperature, utilization."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,memory.total,memory.used,memory.free,temperature.gpu,utilization.gpu,name",
             "--format=csv,nounits,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        gpus = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                gpus.append({
                    "gpu": f"GPU {parts[0]}",
                    "memory_total": int(parts[1]),
                    "memory_used": int(parts[2]),
                    "memory_free": int(parts[3]),
                    "temperature": int(parts[4]) if parts[4] else None,
                    "utilization": int(parts[5]) if parts[5] else None,
                })
        return gpus
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        return []


def get_cpu_stats() -> dict[str, Any]:
    """Parse free -m output for CPU memory stats."""
    try:
        result = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=10)
        for line in result.stdout.strip().split("\n"):
            if line.startswith("Mem:"):
                parts = line.split()
                total, used, free = int(parts[1]), int(parts[2]), int(parts[3])
                available = int(parts[6]) if len(parts) > 6 else free
                return {"total": total, "used": used, "free": free,
                        "available": available,
                        "usage_percent": round(used / total * 100, 1) if total > 0 else 0}
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return {"total": 0, "used": 0, "free": 0, "available": 0, "usage_percent": 0}


def get_disk_stats() -> list[dict[str, Any]]:
    """Parse df output for disk usage."""
    try:
        result = subprocess.run(
            ["df", "-B1", "/home", "/root", "/"],
            capture_output=True, text=True, timeout=10,
        )
        disks = []
        for line in result.stdout.strip().split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 6:
                total, used, free = int(parts[1]), int(parts[2]), int(parts[3])
                usage_pct = int(parts[4].rstrip("%"))
                disks.append({
                    "mount": parts[5], "total": total, "used": used,
                    "free": free, "usage_percent": float(usage_pct),
                })
        return disks
    except (subprocess.SubprocessError, FileNotFoundError):
        return []


def get_all_memory() -> dict[str, Any]:
    """Get all memory stats in one dict."""
    return {
        "gpu": get_gpu_stats(),
        "cpu": get_cpu_stats(),
        "disk": get_disk_stats(),
    }
