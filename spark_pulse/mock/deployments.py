"""Mock deployment tools — in-memory tracker with disk persistence."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEPLOYMENTS_FILE = Path(__file__).resolve().parent.parent / "data" / "deployments.json"


def _load() -> list[dict[str, Any]]:
    if not _DEPLOYMENTS_FILE.exists():
        return []
    try:
        with open(_DEPLOYMENTS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(data: list[dict[str, Any]]) -> None:
    _DEPLOYMENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_DEPLOYMENTS_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


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
