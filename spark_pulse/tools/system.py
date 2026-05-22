"""Real system tools — nvidia-smi, free, df parsing."""

import subprocess
from typing import Any


def get_gpu_stats() -> list[dict[str, Any]]:
    """Parse nvidia-smi output for GPU memory, temperature, utilization, and power."""
    commands = [
        ["nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,temperature.gpu,utilization.gpu,power.draw,power.limit", "--format=csv,nounits,noheader"],
        ["nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,temperature.gpu,utilization.gpu", "--format=csv,nounits,noheader"],
    ]

    def parse_number(value: str) -> float | int | None:
        if not value or value in {"N/A", "Not Supported", "NA"}:
            return None
        return float(value) if "." in value else int(value)

    try:
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                continue
            gpus = []
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 8:
                    continue
                gpus.append({
                    "index": int(parts[0]),
                    "gpu": f"GPU {parts[0]}",
                    "uuid": parts[1],
                    "name": parts[2],
                    "memory_total": int(parts[3]),
                    "memory_used": int(parts[4]),
                    "memory_free": int(parts[5]),
                    "temperature": int(parts[6]) if parts[6] else None,
                    "utilization": int(parts[7]) if parts[7] else None,
                    "power_draw": parse_number(parts[8]) if len(parts) > 8 else None,
                    "power_limit": parse_number(parts[9]) if len(parts) > 9 else None,
                })
            if gpus:
                return gpus
        return []
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        return []


def get_gpu_process_stats() -> list[dict[str, Any]]:
    """Parse nvidia-smi compute-apps output for GPU process usage."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory", "--format=csv,nounits,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        processes = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                try:
                    processes.append({
                        "gpu_uuid": parts[0],
                        "pid": int(parts[1]),
                        "process_name": parts[2],
                        "used_memory": int(parts[3].split()[0]),
                    })
                except ValueError:
                    continue
        return processes
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
        seen_mounts = set()
        for line in result.stdout.strip().split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 6:
                mount = parts[5]
                if mount in seen_mounts:
                    continue
                seen_mounts.add(mount)
                total, used, free = int(parts[1]), int(parts[2]), int(parts[3])
                usage_pct = int(parts[4].rstrip("%"))
                disks.append({
                    "mount": mount, "total": total, "used": used,
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
        "processes": get_gpu_process_stats(),
    }
