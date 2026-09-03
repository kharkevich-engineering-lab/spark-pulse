"""Engine image API — catalogue, pull jobs, deletion and node distribution.

An image ref carries slashes and colons, so it never travels as a path
parameter here: the catalogue is a flat list and every ref-taking route takes
it in the body or as a query parameter.
"""

from fastapi import APIRouter, Body, HTTPException, Query

from spark_pulse import tools

router = APIRouter(prefix="/api/images", tags=["images"])


# ── Pull jobs (registered before the catalogue routes) ───────────────────────


@router.post("/pull")
def start_pull(req: dict):
    ref = str(req.get("ref") or "").strip()
    if not ref:
        raise HTTPException(status_code=400, detail="ref is required")
    try:
        return tools.images.start_pull(ref)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/pulls")
def list_pulls():
    return {"jobs": tools.images.list_pulls()}


@router.get("/pulls/{job_id}")
def get_pull(job_id: str):
    job = tools.images.get_pull(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No such pull job: {job_id}")
    return job


@router.post("/pulls/{job_id}/cancel")
def cancel_pull(job_id: str):
    job = tools.images.cancel_pull(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No such pull job: {job_id}")
    return job


# ── Distribution ─────────────────────────────────────────────────────────────


@router.post("/sync")
def sync_image(req: dict):
    ref = str(req.get("ref") or "").strip()
    nodes = req.get("nodes")
    if not ref:
        raise HTTPException(status_code=400, detail="ref is required")
    if not isinstance(nodes, list) or not nodes:
        raise HTTPException(status_code=400, detail="nodes must be a non-empty list")
    try:
        return tools.images.sync_to_nodes(
            ref, [str(n) for n in nodes], req.get("ssh_user")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/presence")
def image_presence(
    ref: str = Query(..., description="Image reference"),
    nodes: str = Query("", description="Comma-separated node list"),
    ssh_user: str | None = Query(None),
):
    node_list = [n.strip() for n in nodes.split(",") if n.strip()]
    return tools.images.presence(ref, node_list, ssh_user)


# ── Catalogue ────────────────────────────────────────────────────────────────


@router.get("")
def list_images():
    return {"images": tools.images.list_images()}


@router.delete("")
def delete_image(
    ref: str = Query("", description="Image reference"),
    req: dict | None = Body(None),
):
    """Delete a local image. The ref may come as a query param or in the body."""
    target = (ref or str((req or {}).get("ref") or "")).strip()
    if not target:
        raise HTTPException(status_code=400, detail="ref is required")
    try:
        return tools.images.delete_image(target)
    except ValueError as exc:
        message = str(exc)
        status = 409 if "in use" in message else 404
        raise HTTPException(status_code=status, detail=message) from exc
