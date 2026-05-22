"""Deployments API — CRUD + launch/stop."""

from fastapi import APIRouter, HTTPException

from spark_pulse import tools

router = APIRouter(prefix="/api/deployments", tags=["deployments"])


@router.get("")
def list_deployments():
    return tools.deployments.list_deployments()


@router.post("")
def create_deployment(req: dict):
    recipe_id = req.get("recipe_id", "")
    name = req.get("name", recipe_id)

    # Look up recipe for defaults
    recipe = tools.recipes.get_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"Recipe '{recipe_id}' not found")

    params = {**recipe.get("defaults", {}), **req.get("params", {})}
    params["port"] = params.get("port", 8000)
    params["host"] = params.get("host", "0.0.0.0")

    # Build the vLLM serve command for display/logging
    launch_cmd = tools.recipes.build_launch_command(recipe, params)

    return tools.deployments.create_deployment(
        recipe_id=recipe_id, name=name, params=params,
        nodes=req.get("nodes"),
        launch_command=launch_cmd,
    )


@router.delete("/{deployment_id}")
def stop_or_delete_deployment(deployment_id: str):
    deps = tools.deployments.list_deployments()
    dep = next((d for d in deps if d.get("id") == deployment_id), None)
    if dep is None:
        raise HTTPException(status_code=404, detail=f"Deployment '{deployment_id}' not found")

    if dep.get("status") in ("stopped", "error"):
        # Terminal state — remove from history
        tools.deployments.delete_deployment(deployment_id)
        return {"deleted": True, "id": deployment_id}

    # Active — stop the process
    result = tools.deployments.stop_deployment(deployment_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Deployment '{deployment_id}' not found")
    return result


@router.get("/{deployment_id}/logs")
def get_logs(deployment_id: str, lines: int = 200):
    logs = tools.deployments.get_logs(deployment_id, lines)
    return {"logs": logs}
