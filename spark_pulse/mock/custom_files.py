"""Mock custom files tools — symlink management simulation.

Returns deterministic results without touching the filesystem.
"""

from __future__ import annotations

from typing import Any


def create_symlinks(spark_vllm_path: str) -> dict[str, list[str]]:
    """Simulate creating symlinks for custom recipes and mods.

    Returns dict with lists of symlink names created.
    """
    return {
        "recipes": [
            "custom-my-recipe",
            "custom-benchmark",
        ],
        "mods": [
            "custom-fix-patches",
            "custom-tuning",
        ],
    }


def remove_symlinks(spark_vllm_path: str) -> dict[str, list[str]]:
    """Simulate removing symlinks for custom recipes and mods.

    Returns dict with lists of symlink names removed.
    """
    return {
        "recipes": [
            "custom-my-recipe",
        ],
        "mods": [
            "custom-fix-patches",
        ],
    }


def create_symlink_for_recipe(recipe_name: str) -> bool:
    """Simulate creating a symlink for a specific custom recipe.

    Returns True if symlink was created.
    """
    # Simulate: always succeeds for non-empty names
    return bool(recipe_name and recipe_name != "exists")


def remove_symlink_for_recipe(recipe_name: str) -> bool:
    """Simulate removing a symlink for a specific custom recipe.

    Returns True if symlink was removed.
    """
    return bool(recipe_name and recipe_name != "missing")


def create_symlink_for_mod(mod_name: str) -> bool:
    """Simulate creating a symlink for a specific custom mod.

    Returns True if symlink was created.
    """
    return bool(mod_name and mod_name != "exists")


def remove_symlink_for_mod(mod_name: str) -> bool:
    """Simulate removing a symlink for a specific custom mod.

    Returns True if symlink was removed.
    """
    return bool(mod_name and mod_name != "missing")
