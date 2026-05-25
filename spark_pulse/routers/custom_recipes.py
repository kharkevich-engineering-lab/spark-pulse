"""Recipe customization API endpoints.

Provides CRUD operations for per-recipe customizations. Customizations
are stored in ~/.config/spark-pulse/custom-recipes.json so original
YAML files are never modified — git updates always work.
"""

from fastapi import APIRouter, HTTPException

from spark_pulse.tools.custom_recipes import (
    delete_customization,
    get_customization,
    save_customization,
)

router = APIRouter(prefix="/api/recipes/customize", tags=["recipe-customizations"])


@router.get("/{recipe_id:path}")
def get_recipe_customization(recipe_id: str):
    """Get current customizations for a recipe.

    Returns the partial override dict, or an empty dict if none.
    """
    custom = get_customization(recipe_id)
    if custom is None:
        return {}
    return custom


@router.put("/{recipe_id:path}")
def save_recipe_customization(recipe_id: str, customization: dict):
    """Save customizations for a recipe.

    Only the following fields are accepted:
    - command: str — override the command template
    - defaults: dict — merge with recipe defaults (user wins)
    - env: dict — override/add environment variables
    - build_args: list[str] — override/add build arguments
    - container: str — override container name
    - model: str — override model identifier
    """
    result = save_customization(recipe_id, customization)
    return result if result else {}


@router.delete("/{recipe_id:path}")
def delete_recipe_customization(recipe_id: str):
    """Remove all customizations for a recipe, reverting to original YAML."""
    deleted = delete_customization(recipe_id)
    return {"deleted": deleted}
