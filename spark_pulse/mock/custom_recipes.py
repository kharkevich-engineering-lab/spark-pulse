"""Mock custom recipes tools — recipe customization simulation.

Returns deterministic results without touching the filesystem.
Mirrors the real custom_recipes.py API exactly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Simulated customizations stored in memory
_CUSTOMIZATIONS: dict[str, dict[str, Any]] = {
    "qwen3.5-397b-int4": {
        "command": "vllm serve {model} --port {port} --tensor-parallel {tensor_parallel}",
        "defaults": {
            "tensor_parallel": 4,
            "gpu_memory_utilization": 0.95,
        },
        "env": {
            "VLLM_LOG_LEVEL": "debug",
        },
        "container": "vllm-node-tf5-custom",
    },
    "minimax-m2-awq": {
        "defaults": {
            "max_num_seqs": 32,
        },
        "model": "QuantTrio/MiniMax-M2-AWQ-fine-tuned",
    },
}


def load_customizations() -> dict[str, dict]:
    """Load all recipe customizations (simulated)."""
    return dict(_CUSTOMIZATIONS)


def save_customizations(customizations: dict[str, dict]) -> None:
    """Persist recipe customizations (simulated — in-memory only)."""
    _CUSTOMIZATIONS.clear()
    _CUSTOMIZATIONS.update(customizations)


def get_customization(recipe_id: str) -> dict | None:
    """Get partial customization for a specific recipe."""
    return _CUSTOMIZATIONS.get(recipe_id)


def save_customization(recipe_id: str, customization: dict) -> dict:
    """Save (merge) customizations for a recipe.

    Only keys in CUSTOMIZABLE_FIELDS are stored. Returns the complete
    customization dict for this recipe.
    """
    CUSTOMIZABLE_FIELDS = {
        "command",
        "defaults",
        "env",
        "build_args",
        "container",
        "model",
        "mods",
    }
    existing = _CUSTOMIZATIONS.get(recipe_id, {})
    filtered = {
        k: v
        for k, v in customization.items()
        if k in CUSTOMIZABLE_FIELDS and v is not None
    }
    merged = {**existing, **filtered}
    if merged:
        _CUSTOMIZATIONS[recipe_id] = merged
    else:
        _CUSTOMIZATIONS.pop(recipe_id, None)
    return merged


def delete_customization(recipe_id: str) -> bool:
    """Remove customization for a specific recipe."""
    if recipe_id in _CUSTOMIZATIONS:
        del _CUSTOMIZATIONS[recipe_id]
        return True
    return False


def has_customization(recipe_id: str) -> bool:
    """Quick check whether a recipe has any customizations."""
    return recipe_id in _CUSTOMIZATIONS


def get_customized_recipe(
    recipe_id: str, spark_path: Path | None = None
) -> dict | None:
    """Load a recipe and merge any user customizations on top.

    Returns the merged recipe dict, or None if the base recipe doesn't exist.
    In simulation mode, returns a synthetic recipe with customizations applied.
    """
    # Simulated base recipes
    _BASE_RECIPES: dict[str, dict[str, Any]] = {
        "qwen3.5-397b-int4": {
            "name": "qwen3.5-397b-int4",
            "model": "Intel/Qwen3.5-397B-INT4-AutoRound",
            "container": "vllm-node-tf5",
            "description": "Qwen3.5 397B INT4 quantized with AutoRound.",
            "solo_only": False,
            "cluster_only": True,
            "defaults": {
                "tensor_parallel": 2,
                "port": 9000,
                "gpu_memory_utilization": 0.9,
                "max_num_seqs": 2,
            },
            "env": {},
            "build_args": [],
        },
        "minimax-m2-awq": {
            "name": "minimax-m2-awq",
            "model": "QuantTrio/MiniMax-M2-AWQ",
            "container": "vllm-node",
            "description": "MiniMax-M2 with AWQ quantization.",
            "solo_only": False,
            "cluster_only": True,
            "defaults": {
                "tensor_parallel": 2,
                "port": 9020,
                "gpu_memory_utilization": 0.7,
                "max_num_seqs": 16,
            },
            "env": {},
            "build_args": [],
        },
    }

    recipe = _BASE_RECIPES.get(recipe_id)
    if recipe is None:
        return None

    customization = _CUSTOMIZATIONS.get(recipe_id)
    if not customization:
        return recipe

    # Merge defaults (user overrides original defaults)
    custom_defaults = customization.get("defaults")
    if custom_defaults:
        recipe = {
            **recipe,
            "defaults": {**recipe.get("defaults", {}), **custom_defaults},
        }

    # Direct field overrides
    for field in ("command", "env", "build_args", "container", "model"):
        if field in customization:
            recipe[field] = customization[field]

    return recipe
