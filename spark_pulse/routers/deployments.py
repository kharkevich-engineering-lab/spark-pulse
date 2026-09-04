"""Deployments API — plan (dry run), CRUD, launch/stop.

Every call goes through ``tools.deploy_dispatch``. Creating is always the
native runtime; acting on an existing deployment follows the record, so one
made by the removed upstream runner can still be read, stopped and deleted.

A create runs the pre-flight first and refuses a *blocked* verdict, because
every condition the pre-flight blocks on — no docker, no GPU, a port already
taken — is one the deploy would hit anyway, minutes later, with a container
already pulled and a worse error. ``skip_preflight`` bypasses it for an
operator who has judged the report wrong.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from spark_pulse import tools
from spark_pulse.tools.native_runtime import NativeRuntimeError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/deployments", tags=["deployments"])


def _preflight_gate(req: dict, params: dict[str, Any]) -> dict[str, Any] | None:
    """Run the pre-flight for a create, and refuse a blocked verdict.

    Returns the report so it can be attached to the response; raises 409 when
    the verdict blocks. A pre-flight that itself fails is logged and ignored:
    a broken checker must not become a new way for a deploy to fail.
    """
    try:
        report = tools.preflight.run(
            recipe_id=req.get("recipe_id", ""),
            engine=req.get("engine"),
            variant=req.get("variant"),
            model=req.get("model"),
            params=params,
            extra_args=req.get("extra_args") or [],
            nodes=req.get("nodes") or None,
            allow_missing_model=bool(req.get("allow_missing_model", False)),
        )
    except (NativeRuntimeError, ValueError):
        # The plan is bad; let the create raise the real error rather than a
        # second, vaguer one from the checker.
        return None
    except Exception:  # pragma: no cover - defensive
        logger.exception("pre-flight failed; proceeding with the deploy")
        return None

    if not report.get("can_proceed", True):
        raise HTTPException(
            status_code=409,
            detail={
                "message": report.get("summary")
                or "pre-flight found conditions that would stop this deployment",
                "preflight": report,
            },
        )
    return report


class PlanRequest(BaseModel):
    """Dry-run request — same knobs as a create, nothing is started."""

    recipe_id: str
    name: str = ""
    engine: str | None = None
    variant: str | None = None
    model: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    extra_args: list[str] = Field(default_factory=list)
    nodes: list[str] = Field(default_factory=list)
    allow_missing_model: bool = True


@router.get("")
def list_deployments():
    return tools.deploy_dispatch.list_deployments()


@router.post("/plan")
def plan_deployment(req: PlanRequest):
    """Resolve a deployment without starting it: the UI's Deploy preview.

    Returns the rendered command, image ref, model, mods and the full container
    profile so the operator can see exactly what would run.
    """
    try:
        return tools.deploy_dispatch.plan_deployment(
            recipe_id=req.recipe_id,
            engine=req.engine,
            variant=req.variant,
            model=req.model,
            params=req.params,
            extra_args=req.extra_args,
            nodes=req.nodes or None,
            allow_missing_model=req.allow_missing_model,
        )
    except NativeRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("")
def create_deployment(req: dict):
    recipe_id = req.get("recipe_id", "")
    name = req.get("name", recipe_id)

    # Fail fast on an unknown recipe: the runtime would refuse it too, but
    # from further in and with a worse error.
    if tools.recipes.get_recipe(recipe_id) is None:
        raise HTTPException(status_code=404, detail=f"Recipe '{recipe_id}' not found")

    # The runtime is given the caller's own params, never the recipe's
    # defaults merged in: merging makes an explicit request indistinguishable
    # from a default, and the runtime has to tell them apart to decide what to
    # refuse and how to explain it. The pre-flight gets them for the same
    # reason — otherwise it would check a port the deploy is not going to use.
    raw_params = req.get("params") or {}

    preflight = None
    if not req.get("skip_preflight"):
        preflight = _preflight_gate(req, raw_params)

    try:
        created = tools.deploy_dispatch.create_deployment(
            recipe_id=recipe_id,
            name=name,
            params=raw_params,
            nodes=req.get("nodes"),
            engine=req.get("engine"),
            variant=req.get("variant"),
            model=req.get("model"),
            extra_args=req.get("extra_args") or [],
            allow_missing_model=bool(req.get("allow_missing_model", False)),
        )
    except NativeRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # The advisories are worth showing even on a create that went ahead: "the
    # image is not on rank 1 yet" explains the first four minutes of silence.
    if isinstance(created, dict) and preflight is not None:
        created["preflight"] = {
            "verdict": preflight.get("verdict"),
            "summary": preflight.get("summary"),
            "delays": preflight.get("delays"),
            "estimated_transfer_bytes": preflight.get("estimated_transfer_bytes"),
            "delaying": preflight.get("delaying"),
            "advisories": preflight.get("advisories"),
        }
    return created


@router.delete("/{deployment_id}")
def stop_or_delete_deployment(deployment_id: str):
    deps = tools.deploy_dispatch.list_deployments()
    dep = next((d for d in deps if d.get("id") == deployment_id), None)
    if dep is None:
        raise HTTPException(
            status_code=404, detail=f"Deployment '{deployment_id}' not found"
        )

    if dep.get("status") in ("stopped", "error"):
        # Terminal state — remove from history
        tools.deploy_dispatch.delete_deployment(deployment_id)
        return {"deleted": True, "id": deployment_id}

    # Active — stop the process or container
    result = tools.deploy_dispatch.stop_deployment(deployment_id)
    if result is None:
        raise HTTPException(
            status_code=404, detail=f"Deployment '{deployment_id}' not found"
        )
    return result


@router.get("/{deployment_id}")
def get_deployment(deployment_id: str):
    dep = tools.deploy_dispatch.get_deployment(deployment_id)
    if dep is None:
        raise HTTPException(
            status_code=404, detail=f"Deployment '{deployment_id}' not found"
        )
    return dep


@router.get("/{deployment_id}/logs")
def get_logs(deployment_id: str, lines: int = 200):
    logs = tools.deploy_dispatch.get_logs(deployment_id, lines)
    return {"logs": logs}
