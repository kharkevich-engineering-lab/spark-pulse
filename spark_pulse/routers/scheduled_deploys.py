"""Deployments queued behind a model download.

The path an operator takes: they deploy a recipe, the model is not here, and
instead of a 400 telling them to go and download it they are offered the
download. Accepting posts the same body here, which starts the fetch and
records the deployment to run when it lands.

Its own prefix rather than a sub-path of ``/api/deployments``, because that
router ends in ``GET /{deployment_id}`` — a catch-all that would swallow
``/scheduled`` and answer 404 for a deployment by that name.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from spark_pulse import tools
from spark_pulse.tools.native_runtime import NativeRuntimeError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scheduled-deploys", tags=["deployments"])


def _with_run(entry: dict[str, Any]) -> dict[str, Any]:
    """The entry, plus what the run it started is doing now.

    ``done`` means the deployment was *created*. Creating one returns before
    the image is pulled and the engine is up, so a run that then failed —
    an image with no build for the node's platform, an engine that died —
    left the Models page saying "Deployed" over a run in ``error``. The
    record is read as stored (``deployment_records``, no probe of the nodes),
    because this list is polled and the Runs page is where a run is watched.
    ``run`` is ``None`` before a deployment exists and when its record has
    since been removed.
    """
    run = None
    deployment_id = str(entry.get("deployment_id") or "")
    if deployment_id:
        record = tools.deployment_records.get(deployment_id)
        if record is not None:
            run = {
                "id": deployment_id,
                "status": str(record.get("status") or ""),
                "error": str(record.get("error_message") or ""),
            }
    return {**entry, "run": run}


@router.get("")
def list_scheduled(active_only: bool = False) -> list[dict[str, Any]]:
    return [
        _with_run(entry)
        for entry in tools.scheduled_deploys.listing(active_only=active_only)
    ]


@router.get("/{entry_id}")
def get_scheduled(entry_id: str) -> dict[str, Any]:
    """One entry by id, so a caller holding one can follow it without the list.

    Declared before the POST purely for reading order; the two never collide,
    since a path parameter on GET cannot swallow the collection's own POST.
    """
    entry = tools.scheduled_deploys.get(entry_id)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"Scheduled deploy '{entry_id}' not found"
        )
    return _with_run(entry)


@router.post("")
def schedule_deploy(req: dict) -> dict[str, Any]:
    """Start the model download, and deploy this request when it finishes.

    The body is a create body — the same one that just came back with a
    missing model — so the client re-sends what it already had rather than
    assembling a second, subtly different request.
    """
    recipe_id = req.get("recipe_id", "")
    if tools.recipes.get_recipe(recipe_id) is None:
        raise HTTPException(status_code=404, detail=f"Recipe '{recipe_id}' not found")

    # Which model, asked of the planner rather than of the request: the recipe
    # supplies it when the caller does not, and only the planner knows the
    # resolution rules. ``allow_missing_model`` is what makes it answer at all
    # for the case we are here about.
    try:
        plan = tools.deploy_dispatch.plan_deployment(
            recipe_id=recipe_id,
            engine=req.get("engine"),
            variant=req.get("variant"),
            model=req.get("model"),
            params=req.get("params") or {},
            extra_args=req.get("extra_args") or [],
            nodes=req.get("nodes") or None,
            allow_missing_model=True,
        )
    except NativeRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    model = str(plan.get("model") or "")
    if not model:
        raise HTTPException(
            status_code=400,
            detail="this recipe does not name a model, so there is nothing to "
            "download; deploy it directly",
        )
    if plan.get("model_present", True):
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"model '{model}' is already here; deploy it directly",
                "model": model,
            },
        )

    # Only the files this run reads: a GGUF repository holds every packing,
    # and the recipe names one. Empty patterns are the whole repository.
    patterns = list(plan.get("model_patterns") or []) or None
    try:
        job = tools.models.start_download(
            model,
            source=req.get("source"),
            revision=req.get("revision"),
            allow_patterns=patterns,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — disk, network, hub all land here
        logger.exception("could not start the download of %s", model)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    entry = tools.scheduled_deploys.schedule(
        model=model,
        download_job_id=str(job.get("id") or ""),
        request={
            "recipe_id": recipe_id,
            "name": req.get("name") or recipe_id,
            "params": req.get("params") or {},
            "nodes": req.get("nodes"),
            "engine": req.get("engine"),
            "variant": req.get("variant"),
            "model": model,
            "extra_args": req.get("extra_args") or [],
        },
    )
    # The download is part of the answer: the client that just asked for this
    # needs the job id to follow progress without a second round trip.
    entry["download"] = job
    return entry


@router.delete("/{entry_id}")
def cancel_scheduled(entry_id: str, cancel_download: bool = True) -> dict[str, Any]:
    """Call it off. By default the download goes too — see ``tools``."""
    entry = tools.scheduled_deploys.cancel(entry_id, cancel_download=cancel_download)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"Scheduled deploy '{entry_id}' not found"
        )
    return entry
