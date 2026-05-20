"""Cache management API — list and clean cache directories."""

from fastapi import APIRouter

from spark_pulse import tools

router = APIRouter(prefix="/api/cache", tags=["cache"])


@router.get("")
def list_cache():
    return {"entries": tools.cache.list_cache()}


@router.post("/clean")
def clean_cache(req: dict):
    targets = req.get("targets", [])
    if not targets:
        return {"error": "No targets specified"}
    results = tools.cache.clean_cache(targets)
    return {"results": results}
