"""User recipe customizations storage.

Stores partial overrides for recipes in a separate JSON file
(~/.config/spark-pulse/custom-recipes.json) so original YAML files
are never modified — git updates to spark-vllm-docker always work.

Data structure (flat map):
  {
    "<recipe_id>": {
      "command": "...",         // override command template
      "defaults": {...},        // override/merge with recipe defaults
      "env": {...},             // override/add env vars
      "build_args": [...],      // override/add build args
      "container": "...",       // override container name
      "model": "...",           // override model identifier
    }
  }

When loading a customized recipe, custom fields are merged on top of
the original YAML fields (low → high priority).
"""

from __future__ import annotations

import json
from pathlib import Path

_CUSTOM_PATH = Path.home() / ".config" / "spark-pulse" / "custom-recipes.json"

# Fields that can be customized per recipe
CUSTOMIZABLE_FIELDS = {
    "command",
    "defaults",
    "env",
    "build_args",
    "container",
    "model",
    "mods",
}


def load_customizations() -> dict[str, dict]:
    """Load all recipe customizations from disk."""
    if not _CUSTOM_PATH.exists():
        return {}
    try:
        with open(_CUSTOM_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_customizations(customizations: dict[str, dict]) -> None:
    """Persist recipe customizations to disk."""
    _CUSTOM_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CUSTOM_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(customizations, f, indent=2)
    tmp.rename(_CUSTOM_PATH)


def get_customization(recipe_id: str) -> dict | None:
    """Get partial customization for a specific recipe.

    Returns the partial override dict, or None if no customization exists.
    """
    customizations = load_customizations()
    return customizations.get(recipe_id)


def save_customization(recipe_id: str, customization: dict) -> dict:
    """Save (merge) customizations for a recipe.

    Only keys in CUSTOMIZABLE_FIELDS are stored. Returns the complete
    customization dict for this recipe (all fields).
    """
    customizations = load_customizations()
    # Merge into existing
    existing = customizations.get(recipe_id, {})
    # Store only customizable fields, merged with existing
    filtered = {
        k: v for k, v in customization.items()
        if k in CUSTOMIZABLE_FIELDS and v is not None
    }
    merged = {**existing, **filtered}
    if merged:
        customizations[recipe_id] = merged
    else:
        customizations.pop(recipe_id, None)
    save_customizations(customizations)
    return merged


def delete_customization(recipe_id: str) -> bool:
    """Remove customization for a specific recipe.

    Returns True if a customization existed and was deleted.
    """
    customizations = load_customizations()
    if recipe_id in customizations:
        del customizations[recipe_id]
        save_customizations(customizations)
        return True
    return False


def has_customization(recipe_id: str) -> bool:
    """Quick check whether a recipe has any customizations."""
    return get_customization(recipe_id) is not None


def get_customized_recipe(recipe_id: str, spark_path: Path | None = None) -> dict | None:
    """Load a recipe and merge any user customizations on top.

    This is the main entry point — same return shape as get_recipe()
    but with custom fields merged into the base recipe data.

    Merge strategy (low → high):
      1. Original YAML fields from the recipe
      2. Custom defaults merged with original defaults (user wins)
      3. Other custom fields override the originals directly
    """
    from spark_pulse.tools.recipes import get_recipe as _get_recipe

    recipe = _get_recipe(recipe_id, spark_path)
    if recipe is None:
        return None

    customization = get_customization(recipe_id)
    if not customization:
        return recipe

    # Merge defaults (user overrides original defaults)
    custom_defaults = customization.get("defaults")
    if custom_defaults:
        recipe["defaults"] = {**recipe.get("defaults", {}), **custom_defaults}

    # Direct field overrides
    for field in ("command", "env", "build_args", "container", "model"):
        if field in customization:
            recipe[field] = customization[field]

    return recipe
