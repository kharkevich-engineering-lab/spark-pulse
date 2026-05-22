"""Mods API."""

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
