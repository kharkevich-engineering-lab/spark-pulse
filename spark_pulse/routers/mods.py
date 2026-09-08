"""Mods API: what mods there are, and what is in one.

Applying a mod is not here. A recipe names its mods and the native runtime
copies them into each rank's container at deploy time, through that rank's
node service — one implementation, on the path that actually runs.

There were ``/apply`` and ``/rollback`` endpoints taking a ``cluster_state``
of head and workers. That shape came from the removed cluster orchestrator,
nothing had produced one since it was deleted, and the only caller in the UI
never sent one — so the endpoints answered 400 to the one request anybody
made. Both are gone.
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
