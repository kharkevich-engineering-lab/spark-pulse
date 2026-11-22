"""Recipes API — list and view deployment recipes."""

from fastapi import APIRouter, HTTPException

from spark_pulse import tools

router = APIRouter(prefix="/api/recipes", tags=["recipes"])


@router.get("/{recipe_id:path}")
def get_recipe(recipe_id: str):
    if not recipe_id:
        return tools.recipe_fit.annotate(tools.recipes.list_recipes())
    recipe = tools.recipes.get_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"Recipe '{recipe_id}' not found")
    # Whether it runs on this fleet, and on which engines and variants: the
    # deploy dialog offers only those (#172).
    return tools.recipe_fit.annotate([recipe])[0]


@router.get("")
def list_recipes():
    """Every recipe, each with ``fit``: whether it runs on this fleet (#172)."""
    return tools.recipe_fit.annotate(tools.recipes.list_recipes())
