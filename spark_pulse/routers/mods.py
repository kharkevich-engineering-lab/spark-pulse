"""Mods API.

Provides endpoints for listing mods, validating mod content,
and applying mods to cluster nodes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from spark_pulse.tools import mods

router = APIRouter(prefix="/api/mods", tags=["mods"])


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
    cluster_state = req.get("cluster_state")

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

    try:
        from spark_pulse.tools.mods import ModDeployment

        deployment = ModDeployment(
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
    cluster_state = req.get("cluster_state")

    if not mod_name or not mod_path:
        raise HTTPException(
            status_code=400,
            detail="mod_name and mod_path are required",
        )

    try:
        from spark_pulse.tools.mods import ModDeployment

        deployment = ModDeployment(
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
