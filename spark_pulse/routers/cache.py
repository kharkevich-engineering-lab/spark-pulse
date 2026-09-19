"""The engine caches, per node.

Thin, like every router here: the shape is ``{"nodes": [...]}`` — the same
shape ``/api/memory`` answers in, because it answers the same question about
the same machines — and every verb names the node it happens on. There is no
"clean everything everywhere": a sweep that crosses machines is a decision an
operator makes one node at a time, and a button that does not say which node it
empties is how a section came to be single-node in the first place.
"""

from fastapi import APIRouter, HTTPException

from spark_pulse import tools

router = APIRouter(prefix="/api/cache", tags=["cache"])


@router.get("")
def list_cache():
    return tools.cache.get_cache_status()


@router.post("/clean")
def clean_cache(req: dict):
    node = str(req.get("node") or "").strip()
    name = str(req.get("name") or "").strip()
    if not node:
        raise HTTPException(status_code=400, detail="No node specified")
    if not name:
        raise HTTPException(status_code=400, detail="No cache specified")
    return tools.cache.clean_cache(node, name)


@router.post("/clean-all")
def clean_all(req: dict):
    node = str(req.get("node") or "").strip()
    if not node:
        raise HTTPException(status_code=400, detail="No node specified")
    return tools.cache.clean_all(node)
