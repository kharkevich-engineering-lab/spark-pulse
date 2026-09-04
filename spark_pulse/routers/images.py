"""Engine image API — catalogue, pull jobs, deletion and node distribution.

An image ref carries slashes and colons, so it never travels as a path
parameter here: the catalogue is a flat list and every ref-taking route takes
it in the body or as a query parameter.

Distribution is fetch-once: the control node seeds its own registry with the
digest intact, and the other nodes pull from it without a credential of any
kind. ``/registry`` describes that arrangement, ``/sync`` performs it.
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


# ── The control node's registry ──────────────────────────────────────────────
#
# Nodes pull from this registry **anonymously**. The upstream credential lives
# in the control node's secrets and is used only for the control node's own
# fetch, so nothing here ever returns or forwards it — ``nodes_need_credentials``
# is part of the payload because that is the property the mode exists for.


@router.get("/registry")
def registry_status():
    """Where nodes pull from, in which mode, and whether it is up."""
    return tools.registry.status()


@router.post("/registry/start")
def registry_start():
    """Start the registry on the control node. Idempotent."""
    try:
        return tools.registry.start()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/registry/stop")
def registry_stop():
    """Stop the registry on the control node. Idempotent."""
    try:
        return tools.registry.stop()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/registry/seed")
def registry_seed(req: dict):
    """Copy an image into the local registry, digest preserved and verified."""
    ref = str(req.get("ref") or "").strip()
    if not ref:
        raise HTTPException(status_code=400, detail="ref is required")
    try:
        return tools.registry.seed(ref, str(req.get("digest") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        # A digest mismatch is a 502: the registry answered, with the wrong
        # content. Failing loudly here is the whole point of the check.
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
            ref,
            [str(n) for n in nodes],
            req.get("ssh_user"),
            digest=str(req.get("digest") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
