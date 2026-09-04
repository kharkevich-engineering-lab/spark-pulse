"""Custom recipe and mod management API.

Endpoints for uploading, editing, and deleting custom recipes and mods stored
in ~/.config/spark-pulse/. They are read straight from there — by
``recipe_sources`` for recipes and ``tools.mods`` for mods — so a saved file is
live immediately, with no checkout and nothing to sync.
"""

from fastapi import APIRouter, HTTPException, UploadFile, File, Form

from spark_pulse.tools.custom_files import (
    discover_custom_recipes,
    discover_custom_mods,
    get_custom_recipe_content,
    save_custom_recipe,
    delete_custom_recipe,
    upload_custom_recipe,
    get_custom_mod_files,
    save_custom_mod,
    delete_custom_mod,
)

router = APIRouter(prefix="/api/custom-files", tags=["custom-files"])


# ── Validation ──────────────────────────────────────────────────────────────


@router.post("/recipes/validate")
def validate_recipe_endpoint(content: dict):
    """Validate a recipe YAML without saving it.

    Content: {"content": "name: ...\\nmodel: ..."}
    """
    yaml_content = content.get("content", "")
    if not yaml_content.strip():
        raise HTTPException(status_code=400, detail="YAML content cannot be empty")
    import yaml

    try:
        data = yaml.safe_load(yaml_content)
        if not isinstance(data, dict):
            raise ValueError("YAML must be a mapping")

        # Check required fields
        errors = []
        if not data.get("name"):
            errors.append("name is required")
        if not data.get("model"):
            errors.append("model is required")
        if not data.get("container"):
            errors.append("container is required")

        if errors:
            raise ValueError("; ".join(errors))

        return {"valid": True, "name": data.get("name", "")}
    except HTTPException:
        raise
    except (yaml.YAMLError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Custom Recipes ────────────────────────────────────────────────────────


@router.get("/recipes/list")
def list_custom_recipes():
    """List all custom recipes."""
    return discover_custom_recipes()


@router.get("/recipes/{recipe_id:path}")
def get_custom_recipe_content_endpoint(recipe_id: str):
    """Get YAML content of a custom recipe.

    recipe_id: "custom/my-recipe" or "custom/subdir/recipe"
    """
    result = get_custom_recipe_content(recipe_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return result


@router.put("/recipes/{recipe_id:path}")
def save_custom_recipe_endpoint(recipe_id: str, content: dict):
    """Save/update a custom recipe's YAML content.

    Content: {"content": "name: My Recipe\\nmodel: ..."}
    """
    yaml_content = content.get("content", "")
    if not yaml_content.strip():
        raise HTTPException(status_code=400, detail="YAML content cannot be empty")
    try:
        save_custom_recipe(recipe_id, yaml_content)
        return {"saved": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/recipes/{recipe_id:path}")
def delete_custom_recipe_endpoint(recipe_id: str):
    """Delete a custom recipe."""
    deleted = delete_custom_recipe(recipe_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return {"deleted": True}


@router.post("/recipes/upload")
async def upload_custom_recipe_endpoint(file: UploadFile = File(...)):
    """Upload a recipe YAML file."""
    filename = file.filename or "recipe.yaml"
    content = await file.read()
    try:
        return upload_custom_recipe(content, filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Custom Mods ───────────────────────────────────────────────────────────


@router.get("/mods/list")
def list_custom_mods():
    """List all custom mods."""
    return discover_custom_mods()


@router.get("/mods/{mod_id:path}")
def get_custom_mod_files_endpoint(mod_id: str):
    """Get all files from a custom mod.

    mod_id: "custom/my-mod"
    """
    result = get_custom_mod_files(mod_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Mod not found")
    return result


@router.put("/mods/{mod_id:path}")
def save_custom_mod_endpoint(mod_id: str, files: dict):
    """Save files for a custom mod.

    files: {"run.sh": "...", "template.jinja": "..."}
    """
    if not files:
        raise HTTPException(status_code=400, detail="Files cannot be empty")
    try:
        save_custom_mod(mod_id, files)
        return {"saved": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/mods/{mod_id:path}")
def delete_custom_mod_endpoint(mod_id: str):
    """Delete a custom mod."""
    deleted = delete_custom_mod(mod_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Mod not found")
    return {"deleted": True}


@router.post("/mods/upload")
async def upload_custom_mod_endpoint(
    zip_file: UploadFile = File(None),
    name: str = Form(None),
):
    """Upload a custom mod (currently placeholder — expects directory structure via POST).

    For now, creates a stub mod with the given name. ZIP upload can be added later.
    """
    mod_name = name or (
        zip_file.filename.replace(".zip", "") if zip_file.filename else "mod"
    )
    save_custom_mod(
        f"custom/{mod_name}", {"run.sh": f"#!/bin/bash\necho 'Mod: {mod_name}'"}
    )
    return {"id": f"custom/{mod_name}", "name": mod_name, "saved": True}
