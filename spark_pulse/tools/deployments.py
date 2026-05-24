"""Real deployment tools — launches vLLM via run-recipe.sh and tracks PIDs."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from spark_pulse.config import config

_DEPLOYMENTS_FILE = Path.home() / ".config" / "spark-pulse" / "deployments.json"
_LOG_DIR = Path.home() / ".config" / "spark-pulse" / "logs"

# Matches -e KEY=VALUE or -e KEY=VALUE; redacts the value portion
_SENSITIVE_ENV_RE = re.compile(r"(-e\s+\w+=)\S+")


def _redact_cmd(cmd_str: str) -> str:
    """Replace values in -e KEY=VALUE arguments with [REDACTED]."""
    return _SENSITIVE_ENV_RE.sub(r"\1[REDACTED]", cmd_str)


# ── Persistence helpers ───────────────────────────────────────────────────────


def _load() -> list[dict[str, Any]]:
    if not _DEPLOYMENTS_FILE.exists():
        return []
    try:
        with open(_DEPLOYMENTS_FILE) as f:
            data = json.load(f)
        # Sanitize any previously stored unredacted launch commands
        changed = False
        for dep in data:
            lc = dep.get("launch_command")
            if lc and _SENSITIVE_ENV_RE.search(lc):
                dep["launch_command"] = _redact_cmd(lc)
                changed = True
        if changed:
            _save(data)
        return data
    except (json.JSONDecodeError, OSError):
        return []


def _save(data: list[dict[str, Any]]) -> None:
    _DEPLOYMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_DEPLOYMENTS_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _update_status(deployment_id: str, **fields: Any) -> None:
    saved = _load()
    for dep in saved:
        if dep.get("id") == deployment_id:
            dep.update(fields)
            _save(saved)
            return


# ── Process helpers ───────────────────────────────────────────────────────────


def _is_alive(pid: int) -> bool:
    """Return True if the process is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _reconcile(deployments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reconcile job statuses against live PIDs on startup / every list call."""
    changed = False
    now = datetime.now(timezone.utc).isoformat()
    for dep in deployments:
        status = dep.get("status")
        if status == "running":
            pid = dep.get("pid")
            if pid and not _is_alive(int(pid)):
                dep["status"] = "stopped"
                dep.setdefault("stopped_at", now)
                changed = True
        elif status == "pending":
            pid = dep.get("pid")
            if not pid:
                # Server restarted before the process was ever launched
                dep["status"] = "error"
                dep["error_message"] = (
                    "Interrupted: server restarted before process launched"
                )
                dep["stopped_at"] = now
                changed = True
            elif not _is_alive(int(pid)):
                dep["status"] = "stopped"
                dep["stopped_at"] = now
                changed = True
            else:
                # Process is alive but status never advanced — recover it
                dep["status"] = "running"
                changed = True
    if changed:
        _save(deployments)
    return deployments


def _monitor(dep_id: str, proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
    """Background thread: wait for process, then update status."""
    rc = proc.wait()
    now = datetime.now(timezone.utc).isoformat()
    if rc == 0:
        _update_status(dep_id, status="stopped", stopped_at=now)
    else:
        _update_status(
            dep_id,
            status="error",
            stopped_at=now,
            error_message=f"Process exited with code {rc}",
        )


# ── Command builder ───────────────────────────────────────────────────────────


def _build_cmd(
    recipe_id: str, params: dict[str, Any], nodes: list[str] | None
) -> list[str]:
    """Build the run-recipe.sh invocation from deployment params.

    Note: HF_TOKEN is passed via -e to Docker. It will appear in the process
    list on the host (same as running ./run-recipe.sh ... -e HF_TOKEN=xxx
    manually). This matches the documented usage pattern.
    """
    spark_path = Path(config.spark_vllm_path)
    run_script = spark_path / "run-recipe.sh"
    # recipe_id is relative to recipes/ (may include subdir like 3x-spark-cluster/foo)
    recipe_arg = f"recipes/{recipe_id}.yaml"

    cmd = [str(run_script), recipe_arg]

    if params.get("port"):
        cmd += ["--port", str(params["port"])]
    if params.get("host") and params["host"] != "0.0.0.0":
        cmd += ["--host", str(params["host"])]

    solo = not nodes
    tp = params.get("tensor_parallel")
    if tp and not solo:
        # Only pass --tensor-parallel for cluster mode; in solo mode the recipe handles it
        cmd += ["--tensor-parallel", str(tp)]

    if params.get("gpu_memory_utilization"):
        cmd += ["--gpu-memory-utilization", str(params["gpu_memory_utilization"])]
    if params.get("max_model_len"):
        cmd += ["--max-model-len", str(params["max_model_len"])]

    if nodes:
        cmd += ["--nodes", ",".join(nodes)]
    else:
        cmd += ["--solo"]

    # Forward HF token to the Docker container
    hf_token = config.hf_token
    if hf_token:
        cmd += ["-e", f"HF_TOKEN={hf_token}"]

    return cmd


def _purge_expired(deployments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove stopped/error jobs older than job_retention_days."""
    retention = config.job_retention_days
    if retention <= 0:
        return deployments
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
    terminal = {"stopped", "error"}
    kept = []
    changed = False
    for dep in deployments:
        if dep.get("status") in terminal:
            stopped_at = dep.get("stopped_at")
            if stopped_at:
                try:
                    ts = datetime.fromisoformat(stopped_at)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        changed = True
                        continue  # drop expired entry
                except ValueError:
                    pass
        kept.append(dep)
    if changed:
        _save(kept)
    return kept


def list_deployments() -> list[dict[str, Any]]:
    return _purge_expired(_reconcile(_load()))


def create_deployment(
    recipe_id: str,
    name: str,
    params: dict[str, Any],
    nodes: list[str] | None = None,
    launch_command: str = "",
) -> dict[str, Any]:
    dep_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()

    cmd = _build_cmd(recipe_id, params, nodes)
    cmd_str = _redact_cmd(" ".join(cmd))

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = str(_LOG_DIR / f"{dep_id}.log")

    deployment: dict[str, Any] = {
        "id": dep_id,
        "recipe_id": recipe_id,
        "name": name,
        "params": params,
        "nodes": nodes,
        "status": "pending",
        "created_at": now,
        "started_at": None,
        "stopped_at": None,
        "error_message": None,
        "pid": None,
        "port": params.get("port", 8000),
        "launch_command": cmd_str,
        "log_path": log_path,
    }

    saved = _load()
    saved.append(deployment)
    _save(saved)

    spark_path = Path(config.spark_vllm_path)
    try:
        with open(log_path, "w") as log_fh:
            proc = subprocess.Popen(
                cmd,
                cwd=str(spark_path),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # independent process group
            )
        pid = proc.pid
        started = datetime.now(timezone.utc).isoformat()
        _update_status(dep_id, status="running", pid=pid, started_at=started)
        deployment["status"] = "running"
        deployment["pid"] = pid
        deployment["started_at"] = started
        threading.Thread(target=_monitor, args=(dep_id, proc), daemon=True).start()
    except (OSError, subprocess.SubprocessError) as exc:
        err = str(exc)
        stopped = datetime.now(timezone.utc).isoformat()
        _update_status(dep_id, status="error", error_message=err, stopped_at=stopped)
        deployment["status"] = "error"
        deployment["error_message"] = err

    return deployment


def stop_deployment(deployment_id: str) -> dict[str, Any] | None:
    saved = _load()
    for dep in saved:
        if dep.get("id") == deployment_id:
            pid = dep.get("pid")
            if pid:
                try:
                    os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            dep["status"] = "stopped"
            dep["stopped_at"] = datetime.now(timezone.utc).isoformat()
            _save(saved)
            return dep
    return None


def delete_deployment(deployment_id: str) -> bool:
    """Permanently remove a deployment record (for stopped/error jobs)."""
    saved = _load()
    new = [d for d in saved if d.get("id") != deployment_id]
    if len(new) == len(saved):
        return False
    _save(new)
    return True


def get_logs(deployment_id: str, lines: int = 200) -> str:
    dep = next((d for d in _load() if d.get("id") == deployment_id), None)
    if not dep:
        return "Deployment not found"
    log_path = dep.get("log_path")
    if not log_path or not Path(log_path).exists():
        return f"No log file for deployment {deployment_id}"
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), log_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout or "(empty log)"
    except (OSError, subprocess.TimeoutExpired):
        return "Failed to read log file"
