"""Real recipe tools — listing and lookup across every recipe source.

Discovery, parsing and flattening live in
:mod:`spark_pulse.tools.recipe_sources` so the mock module can share them
without perturbing the ``SIMULATION_MODE`` module switch. This module adds the
one thing that differs between real and simulation: where customizations live.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from spark_pulse.config import config
from spark_pulse.tools import custom_recipes, recipe_sources
from spark_pulse.tools.recipe_sources import (
    DEFAULT_CONTAINER as DEFAULT_CONTAINER,
    DEPRECATED_PLACEHOLDERS as DEPRECATED_PLACEHOLDERS,
    SUMMARY_FIELDS as SUMMARY_FIELDS,
)


def list_recipes(spark_path: Path | None = None) -> list[dict[str, Any]]:
    """List every recipe from every source."""
    spark_path = spark_path or config.spark_vllm_dir
    return [
        recipe_sources.summarize(
            payload, custom_recipes.has_customization(payload["id"])
        )
        for payload in recipe_sources.iter_recipe_payloads(spark_path)
    ]


def get_recipe(recipe_id: str, spark_path: Path | None = None) -> dict[str, Any] | None:
    """Load a specific recipe by relative path id or display name."""
    spark_path = spark_path or config.spark_vllm_dir
    recipe = recipe_sources.resolve_recipe(recipe_id, spark_path)
    if recipe is None:
        return None
    recipe_sources.apply_customization(
        recipe, custom_recipes.get_customization(recipe["id"])
    )
    return recipe


def build_launch_command(recipe: dict[str, Any], params: dict[str, Any]) -> str:
    """Build the serve command from a recipe and params."""
    return recipe_sources.render_command(recipe, params)
