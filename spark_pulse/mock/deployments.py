"""Mock deployment tools — in-memory tracker with disk persistence."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Crash-safety and the missing-versus-unreadable distinction are pure
# filesystem behaviour with nothing to simulate, so the mock uses the real
# helper — the failure semantics under SIMULATION_MODE are the production ones.
from spark_pulse.tools.atomic_json import (
    StateFileError as StateFileError,
    read_state_file,
    write_json_atomic,
)

_DEPLOYMENTS_FILE = Path(__file__).resolve().parent.parent / "data" / "deployments.json"


def _load() -> list[dict[str, Any]]:
    """Return persisted deployments; ``[]`` only when the file is absent.

    A file that exists but cannot be read or parsed raises ``StateFileError``.
    """
    data = read_state_file(_DEPLOYMENTS_FILE, expect=list)
    return [] if data is None else data


def check_state_file() -> None:
    """Raise ``StateFileError`` if the deployment state file is unreadable."""
    read_state_file(_DEPLOYMENTS_FILE, expect=list)


def _save(data: list[dict[str, Any]]) -> None:
    write_json_atomic(_DEPLOYMENTS_FILE, data, indent=2, default=str)


def list_deployments() -> list[dict[str, Any]]:
    """List all deployments."""
    return _load()


def create_deployment(
    recipe_id: str,
    name: str,
    params: dict[str, Any],
    nodes: list[str] | None = None,
    launch_command: str = "",
) -> dict[str, Any]:
    """Create a new deployment (simulation — no real process is started)."""
    dep_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    deployment = {
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
        "launch_command": launch_command or f"# Simulation: vllm serve for {recipe_id}",
    }
    saved = _load()
    saved = [d for d in saved if d.get("id") != dep_id]
    saved.append(deployment)
    _save(saved)
    return deployment


def stop_deployment(deployment_id: str) -> dict[str, Any] | None:
    """Stop a running deployment."""
    saved = _load()
    for dep in saved:
        if dep.get("id") == deployment_id:
            dep["status"] = "stopped"
            dep["stopped_at"] = datetime.now(timezone.utc).isoformat()
            _save(saved)
            return dep
    return None


def delete_deployment(deployment_id: str) -> bool:
    """Permanently remove a deployment record (for stopped/error jobs)."""
    saved = _load()
    remaining = [d for d in saved if d.get("id") != deployment_id]
    if len(remaining) == len(saved):
        return False
    _save(remaining)
    return True


def get_logs(deployment_id: str, lines: int = 200) -> str:
    """Return mock log output for a deployment."""
    dep = next((d for d in _load() if d.get("id") == deployment_id), None)
    if not dep:
        return "Deployment not found"
    status = dep.get("status", "unknown")
    recipe_id = dep.get("recipe_id", "?")
    return (
        f"[{datetime.now().isoformat()}] Starting deployment: {dep.get('name', '?')}\n"
        f"[{datetime.now().isoformat()}] Recipe: {recipe_id}\n"
        f"[{datetime.now().isoformat()}] Status: {status}\n"
        f"[{datetime.now().isoformat()}] PID: mock-{deployment_id[:6]}\n"
        f"[{datetime.now().isoformat()}] Port: {dep.get('port', '?')}\n"
        f"[{datetime.now().isoformat()}] vLLM serving mock for {recipe_id}...\n"
        f"[{datetime.now().isoformat()}] (Simulation mode — no real GPU access)\n"
    )
