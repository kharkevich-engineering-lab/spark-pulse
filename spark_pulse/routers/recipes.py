"""Recipes API — list and view deployment recipes."""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from spark_pulse import tools

router = APIRouter(prefix="/api/recipes", tags=["recipes"])


@router.get("")
def list_recipes():
    return tools.recipes.list_recipes()


@router.get("/{recipe_id}")
def get_recipe(recipe_id: str):
    recipe = tools.recipes.get_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"Recipe '{recipe_id}' not found")
    return recipe
