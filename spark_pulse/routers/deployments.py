"""Deployments API — plan (dry run), CRUD, launch/stop.

Every call goes through ``tools.deploy_dispatch``, which picks the upstream
(``run-recipe.sh``) or native (Docker from Python) runtime — see that module
for how the choice is made.
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from spark_pulse import tools
from spark_pulse.tools.native_runtime import NativeRuntimeError

router = APIRouter(prefix="/api/deployments", tags=["deployments"])


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

    # Look up recipe for defaults
    recipe = tools.recipes.get_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"Recipe '{recipe_id}' not found")

    # Merged params drive the upstream path and the display command. The
    # native path gets the caller's own params, so that it can tell an explicit
    # request apart from a recipe default when it reports why something was
    # refused.
    raw_params = req.get("params") or {}
    params = {**recipe.get("defaults", {}), **raw_params}
    params.setdefault("port", 8000)
    params.setdefault("host", "0.0.0.0")

    # Build the vLLM serve command for display/logging
    launch_cmd = tools.recipes.build_launch_command(recipe, params)

    try:
        return tools.deploy_dispatch.create_deployment(
            recipe_id=recipe_id,
            name=name,
            params=params,
            raw_params=raw_params,
            nodes=req.get("nodes"),
            launch_command=launch_cmd,
            engine=req.get("engine"),
            variant=req.get("variant"),
            model=req.get("model"),
            extra_args=req.get("extra_args") or [],
            allow_missing_model=bool(req.get("allow_missing_model", False)),
        )
    except NativeRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
