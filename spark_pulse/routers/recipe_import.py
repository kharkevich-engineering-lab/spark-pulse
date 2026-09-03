"""Recipe import API — pull recipes and mods out of an upstream checkout.

Mounted before the recipes router so ``/api/recipes/import`` is not swallowed
by the ``/{recipe_id:path}`` catch-all there.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from spark_pulse import tools

router = APIRouter(prefix="/api/recipes/import", tags=["recipes"])


class ImportRequest(BaseModel):
    """Either a local ``path`` or a git ``url`` (optionally at ``ref``)."""

    path: str | None = None
    url: str | None = None
    ref: str | None = None


@router.post("")
def import_recipes(body: ImportRequest):
    if not body.path and not body.url:
        raise HTTPException(status_code=400, detail="Provide either 'path' or 'url'")
    if body.path and body.url:
        raise HTTPException(
            status_code=400, detail="Provide only one of 'path' or 'url'"
        )

    try:
        if body.url:
            return tools.recipe_import.import_from_git(body.url, body.ref)
        return tools.recipe_import.import_from_path(body.path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/status")
def import_status():
    return tools.recipe_import.get_import_status()
