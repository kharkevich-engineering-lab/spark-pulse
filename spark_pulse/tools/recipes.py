"""Real recipe tools — listing and lookup across every recipe source.

Discovery, parsing and flattening live in
:mod:`spark_pulse.tools.recipe_sources` so the mock module can share them
without perturbing the ``SIMULATION_MODE`` module switch. This module adds the
one thing that differs between real and simulation: where customizations live.
"""

from __future__ import annotations

from typing import Any

from spark_pulse.tools import custom_recipes, recipe_sources
from spark_pulse.tools.recipe_sources import (
    DEFAULT_CONTAINER as DEFAULT_CONTAINER,
    DEPRECATED_PLACEHOLDERS as DEPRECATED_PLACEHOLDERS,
    SUMMARY_FIELDS as SUMMARY_FIELDS,
)


def list_recipes() -> list[dict[str, Any]]:
    """List every recipe from every source."""
    return [
        recipe_sources.summarize(payload, custom_recipes.has_customization_for(payload))
        for payload in recipe_sources.iter_recipe_payloads()
    ]


def get_recipe(recipe_id: str) -> dict[str, Any] | None:
    """Load a specific recipe by relative path id or display name."""
    recipe = recipe_sources.resolve_recipe(recipe_id)
    if recipe is None:
        return None
    recipe_sources.apply_customization(
        recipe, custom_recipes.get_customization_for(recipe)
    )
    return recipe


def build_launch_command(recipe: dict[str, Any], params: dict[str, Any]) -> str:
    """Build the serve command from a recipe and params."""
    return recipe_sources.render_command(recipe, params)
