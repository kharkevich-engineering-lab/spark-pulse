"""Model catalogue API — catalogue, downloads, distribution and sources."""

from fastapi import APIRouter, HTTPException, Query

from spark_pulse import tools

router = APIRouter(prefix="/api/models", tags=["models"])


# ── Sources (registered before /{id:path} so they are not swallowed) ─────────


@router.get("/sources")
def get_sources():
    return {"sources": tools.models.list_sources()}


@router.put("/sources")
def put_sources(req: dict):
    sources = req.get("sources")
    if not isinstance(sources, list):
        raise HTTPException(status_code=400, detail="sources must be a list")
    try:
        return {"sources": tools.models.save_sources(sources)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── Download jobs ────────────────────────────────────────────────────────────


@router.post("/download")
def start_download(req: dict):
    model = str(req.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    allow_patterns = req.get("allow_patterns")
    if allow_patterns is not None and not isinstance(allow_patterns, list):
        raise HTTPException(status_code=400, detail="allow_patterns must be a list")
    try:
        return tools.models.start_download(
            model=model,
            source=req.get("source"),
            revision=req.get("revision"),
            allow_patterns=allow_patterns,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/downloads")
def list_downloads():
    return {"jobs": tools.models.list_downloads()}


@router.get("/downloads/{job_id}")
def get_download(job_id: str):
    job = tools.models.get_download(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No such download job: {job_id}")
    return job


@router.post("/downloads/{job_id}/cancel")
def cancel_download(job_id: str):
    job = tools.models.cancel_download(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No such download job: {job_id}")
    return job


# ── Catalogue ────────────────────────────────────────────────────────────────


@router.get("")
def list_models():
    return {"models": tools.models.list_models()}


@router.post("/{model_id:path}/sync")
def sync_model(model_id: str, req: dict):
    nodes = req.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise HTTPException(status_code=400, detail="nodes must be a non-empty list")
    try:
        return tools.models.sync_to_nodes(
            model_id, [str(n) for n in nodes], req.get("ssh_user")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{model_id:path}/presence")
def model_presence(
    model_id: str,
    nodes: str = Query("", description="Comma-separated node list"),
    ssh_user: str | None = Query(None),
):
    node_list = [n.strip() for n in nodes.split(",") if n.strip()]
    return tools.models.presence(model_id, node_list, ssh_user)


@router.get("/{model_id:path}")
def get_model(model_id: str):
    model = tools.models.get_model(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Model not found: {model_id}")
    return model


@router.delete("/{model_id:path}")
def delete_model(model_id: str):
    try:
        return tools.models.delete_model(model_id)
    except ValueError as exc:
        message = str(exc)
        status = 409 if "in use" in message else 404
        raise HTTPException(status_code=status, detail=message) from exc
