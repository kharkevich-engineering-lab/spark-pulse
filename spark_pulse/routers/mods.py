"""Mods API.

Provides endpoints for listing mods, validating mod content,
and applying mods to cluster nodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from spark_pulse.tools import mods

router = APIRouter(prefix="/api/mods", tags=["mods"])


# ── The cluster a request names ──────────────────────────────────────────────
#
# The orchestrator walks `cluster_state.head` and `cluster_state.workers` and
# reads `.ip`/`.container_name` off each node. A JSON body arrives as a dict,
# which has none of those, so the payload is converted here rather than handed
# straight through.


@dataclass(frozen=True)
class _Node:
    ip: str
    container_name: str


@dataclass(frozen=True)
class _ClusterState:
    head: _Node
    workers: list[_Node] = field(default_factory=list)


def _node(raw: Any, where: str) -> _Node:
    if not isinstance(raw, dict) or not raw.get("ip"):
        raise HTTPException(
            status_code=400,
            detail=f"cluster_state.{where} needs an 'ip'",
        )
    return _Node(
        ip=str(raw["ip"]),
        container_name=str(raw.get("container_name", "")),
    )


def _cluster_state(raw: Any) -> _ClusterState:
    """The cluster named by the request body, or a 400 saying what is missing."""
    if not isinstance(raw, dict) or "head" not in raw:
        raise HTTPException(
            status_code=400,
            detail="cluster_state with a 'head' node is required",
        )
    workers = raw.get("workers") or []
    if not isinstance(workers, list):
        raise HTTPException(
            status_code=400,
            detail="cluster_state.workers must be a list",
        )
    return _ClusterState(
        head=_node(raw["head"], "head"),
        workers=[_node(w, f"workers[{i}]") for i, w in enumerate(workers)],
    )


@router.get("")
def list_mods():
    return mods.list_mods()


@router.get("/{mod_id}")
def get_mod(mod_id: str):
    result = mods.get_mod(mod_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Mod '{mod_id}' not found")
    return result


@router.post("/validate")
def validate_mod(req: dict[str, Any]) -> dict[str, Any]:
    """Validate mod: security checks, content scanning.

    Returns ValidationResult as dict.
    """
    path = req.get("path", "")
    if not path:
        raise HTTPException(status_code=400, detail="path is required")

    try:
        mod_path = Path(path)
        validation = mods.validate_mod_content(mod_path)

        return {
            "healthy": validation.healthy,
            "warnings": validation.warnings,
            "errors": validation.errors,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/apply")
def apply_mod(req: dict[str, Any]) -> dict[str, Any]:
    """Apply mod to cluster with tracking.

    Returns ModDeployment with completed_nodes.
    """
    mod_name = req.get("mod_name", "")
    mod_path = req.get("mod_path", "")
    target = req.get("target", "all")

    if not mod_name or not mod_path:
        raise HTTPException(
            status_code=400,
            detail="mod_name and mod_path are required",
        )
    if target not in ("head", "workers", "all"):
        raise HTTPException(
            status_code=400,
            detail="target must be 'head', 'workers', or 'all'",
        )
    cluster_state = _cluster_state(req.get("cluster_state"))

    try:
        deployment = mods.ModDeployment(
            mod_name=mod_name,
            mod_path=Path(mod_path),
            target=target,
        )

        orchestrator = mods.ModOrchestrator()
        result = orchestrator.apply_mod_cluster(deployment, cluster_state)

        return {
            "mod_name": result.mod_name,
            "target": result.target,
            "completed_nodes": result.completed_nodes,
            "failed_nodes": result.failed_nodes,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rollback")
def rollback_mod(req: dict[str, Any]) -> dict[str, Any]:
    """Rollback a mod on completed nodes.

    Returns list of nodes where rollback succeeded.
    """
    mod_name = req.get("mod_name", "")
    mod_path = req.get("mod_path", "")
    target = req.get("target", "all")
    completed_nodes = req.get("completed_nodes", [])

    if not mod_name or not mod_path:
        raise HTTPException(
            status_code=400,
            detail="mod_name and mod_path are required",
        )
    if target not in ("head", "workers", "all"):
        raise HTTPException(
            status_code=400,
            detail="target must be 'head', 'workers', or 'all'",
        )
    cluster_state = _cluster_state(req.get("cluster_state"))

    try:
        deployment = mods.ModDeployment(
            mod_name=mod_name,
            mod_path=Path(mod_path),
            target=target,
            completed_nodes=completed_nodes,
        )

        orchestrator = mods.ModOrchestrator()
        rolled_back = orchestrator.rollback_mod(deployment, cluster_state)

        return {
            "rolled_back_nodes": rolled_back,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
