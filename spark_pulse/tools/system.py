"""Real system tools — nvidia-smi, free, df parsing."""

import re
import subprocess
from pathlib import Path
from typing import Any

# ── Docker / process-tracking helpers ────────────────────────────────────────


def _cgroup_container_id(pid: int) -> str | None:
    """Return the short (12-char) Docker container ID from a process's cgroup, or None."""
    try:
        cgroup = Path(f"/proc/{pid}/cgroup").read_text()
        m = re.search(r"docker-([0-9a-f]{12})", cgroup)
        return m.group(1) if m else None
    except OSError:
        return None


def _proc_children() -> dict[int, list[int]]:
    """Scan /proc and return a map of pid → direct child pids."""
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            for line in (entry / "status").read_text().splitlines():
                if line.startswith("PPid:"):
                    ppid = int(line.split()[1])
                    children.setdefault(ppid, []).append(int(entry.name))
                    break
        except OSError:
            pass
    return children


def _descendants(
    root: int, children: dict[int, list[int]], max_depth: int = 6
) -> list[int]:
    result: list[int] = []
    queue = [(root, 0)]
    seen = {root}
    while queue:
        pid, d = queue.pop()
        if d >= max_depth:
            continue
        for c in children.get(pid, []):
            if c not in seen:
                seen.add(c)
                result.append(c)
                queue.append((c, d + 1))
    return result


def _docker_container_name(pid: int) -> str | None:
    """If pid is a `docker exec|run|start` process, return the target container name."""
    try:
        parts = (
            Path(f"/proc/{pid}/cmdline")
            .read_text()
            .replace("\x00", " ")
            .strip()
            .split()
        )
    except OSError:
        return None
    if not parts or "docker" not in parts[0]:
        return None
    for i, p in enumerate(parts):
        if p == "--name" and i + 1 < len(parts):
            return parts[i + 1]
        if p in ("exec", "start", "attach") and i + 1 < len(parts):
            j = i + 1
            while j < len(parts) and parts[j].startswith("-"):
                j += 1
            if j < len(parts):
                return parts[j]
    return None


def _resolve_container_name(name: str) -> str | None:
    """Resolve a Docker container name to a short 12-char container ID."""
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()[:12]
    except Exception:
        pass
    return None


def enrich_gpu_process_tracking(
    processes: list[dict[str, Any]],
    running_deployments: list[dict[str, Any]],
) -> None:
    """Set ``is_tracked`` on each GPU process dict in-place.

    Tracked means the process belongs to a Docker container started/exec'd
    by a known running deployment, or its PID directly matches a deployment.
    """
    dep_pids = {dep["pid"] for dep in running_deployments if dep.get("pid")}
    if not processes:
        return

    tracked_container_ids: set[str] = set()
    try:
        children = _proc_children()
        container_names: set[str] = set()
        for dep in running_deployments:
            pid = dep.get("pid")
            if not pid:
                continue
            for child_pid in _descendants(int(pid), children):
                name = _docker_container_name(child_pid)
                if name:
                    container_names.add(name)
        for name in container_names:
            cid = _resolve_container_name(name)
            if cid:
                tracked_container_ids.add(cid)
    except Exception:
        pass

    for proc in processes:
        cid = _cgroup_container_id(proc["pid"])
        if cid:
            proc["is_tracked"] = cid in tracked_container_ids
        else:
            proc["is_tracked"] = proc["pid"] in dep_pids


def get_gpu_stats() -> list[dict[str, Any]]:
    """Parse nvidia-smi output for GPU memory, temperature, utilization, and power."""
    commands = [
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,temperature.gpu,utilization.gpu,power.draw,power.limit",
            "--format=csv,nounits,noheader",
        ],
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,temperature.gpu,utilization.gpu",
            "--format=csv,nounits,noheader",
        ],
    ]

    def parse_number(value: str) -> float | int | None:
        v = value.strip("[] ") if value else value
        if not v or v in {"N/A", "Not Supported", "NA"}:
            return None
        return float(v) if "." in v else int(v)

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
                gpus.append(
                    {
                        "index": int(parts[0]),
                        "gpu": f"GPU {parts[0]}",
                        "uuid": parts[1],
                        "name": parts[2],
                        "memory_total": int(parse_number(parts[3]) or 0),
                        "memory_used": int(parse_number(parts[4]) or 0),
                        "memory_free": int(parse_number(parts[5]) or 0),
                        "memory_supported": parse_number(parts[3]) is not None,
                        "temperature": (
                            int(t)
                            if (t := parse_number(parts[6])) is not None
                            else None
                        ),
                        "utilization": (
                            int(u)
                            if (u := parse_number(parts[7])) is not None
                            else None
                        ),
                        "power_draw": (
                            parse_number(parts[8]) if len(parts) > 8 else None
                        ),
                        "power_limit": (
                            parse_number(parts[9]) if len(parts) > 9 else None
                        ),
                    }
                )
            if gpus:
                return gpus
        return []
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        return []


def get_gpu_process_stats() -> list[dict[str, Any]]:
    """Parse nvidia-smi compute-apps output for GPU process usage."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                "--format=csv,nounits,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=10,
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
                    processes.append(
                        {
                            "gpu_uuid": parts[0],
                            "pid": int(parts[1]),
                            "process_name": parts[2],
                            "used_memory": int(parts[3].split()[0]),
                        }
                    )
                except ValueError:
                    continue
        return processes
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        return []


def get_cpu_stats() -> dict[str, Any]:
    """Parse free -m output for CPU memory stats."""
    try:
        result = subprocess.run(
            ["free", "-m"], capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.strip().split("\n"):
            if line.startswith("Mem:"):
                parts = line.split()
                total, used, free = int(parts[1]), int(parts[2]), int(parts[3])
                available = int(parts[6]) if len(parts) > 6 else free
                return {
                    "total": total,
                    "used": used,
                    "free": free,
                    "available": available,
                    "usage_percent": round(used / total * 100, 1) if total > 0 else 0,
                }
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return {"total": 0, "used": 0, "free": 0, "available": 0, "usage_percent": 0}


def get_disk_stats() -> list[dict[str, Any]]:
    """Parse df output for disk usage."""
    try:
        result = subprocess.run(
            ["df", "-B1", "/home", "/root", "/"],
            capture_output=True,
            text=True,
            timeout=10,
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
                disks.append(
                    {
                        "mount": mount,
                        "total": total,
                        "used": used,
                        "free": free,
                        "usage_percent": float(usage_pct),
                    }
                )
        return disks
    except (subprocess.SubprocessError, FileNotFoundError):
        return []


def kill_gpu_process(pid: int) -> dict[str, Any]:
    """Stop a GPU process — stops the Docker container it belongs to, or sends SIGTERM directly."""
    import os
    import signal as _signal

    container_id = _cgroup_container_id(pid)
    # cgroup returns short 12-char; also accept full 64-char from a wider search
    if not container_id:
        try:
            cgroup = Path(f"/proc/{pid}/cgroup").read_text()
            m = re.search(r"docker-([0-9a-f]{12,64})\.scope", cgroup)
            if m:
                container_id = m.group(1)
        except OSError:
            pass

    if container_id:
        result = subprocess.run(
            ["docker", "stop", container_id],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return {
                "killed": True,
                "pid": pid,
                "method": "docker_stop",
                "container": container_id[:12],
            }
        return {
            "killed": False,
            "pid": pid,
            "error": result.stderr.strip() or "docker stop failed",
        }

    # Fallback: direct signal
    try:
        os.kill(pid, _signal.SIGTERM)
        return {"killed": True, "pid": pid, "method": "sigterm"}
    except ProcessLookupError:
        return {"killed": False, "pid": pid, "error": "Process not found"}
    except PermissionError:
        return {"killed": False, "pid": pid, "error": "Permission denied"}


def get_all_memory() -> dict[str, Any]:
    """Get all memory stats in one dict."""
    return {
        "gpu": get_gpu_stats(),
        "cpu": get_cpu_stats(),
        "disk": get_disk_stats(),
        "processes": get_gpu_process_stats(),
    }
