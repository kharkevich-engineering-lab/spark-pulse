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


@router.get("")
def list_scheduled(active_only: bool = False) -> list[dict[str, Any]]:
    return tools.scheduled_deploys.listing(active_only=active_only)


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

    try:
        job = tools.models.start_download(
            model, source=req.get("source"), revision=req.get("revision")
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
