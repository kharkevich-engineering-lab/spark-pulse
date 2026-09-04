"""Mock recipe tools — the production recipe path, plus a canned catalogue.

Discovery, parsing, flattening and command rendering are shared with the real
tools through :mod:`spark_pulse.tools.recipe_sources` (no mock twin, so
importing it never disturbs the ``SIMULATION_MODE`` module switch), and
customizations are read from the very store the customization API writes to.
Simulation therefore reports the same schema fields, the same sources and the
same rendered command as production for the same recipe files.

The canned list below is what a simulated server shows when there is no recipe
source at all — it is demo data, not a second implementation.
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

# Default mock recipes (used when no recipe source is available at all)
_RECIPES = [
    {
        "name": "qwen3.5-397b-int4",
        "model": "Intel/Qwen3.5-397B-INT4-AutoRound",
        "container": "vllm-node-tf5",
        "command": (
            "vllm serve Intel/Qwen3.5-397B-INT4-AutoRound --host {host} --port {port} "
            "-tp {tensor_parallel} "
            "--gpu-memory-utilization {gpu_memory_utilization}"
        ),
        "description": "Qwen3.5 397B INT4 quantized with AutoRound. Best quality but requires multi-node cluster.",
        "solo_only": False,
        "cluster_only": True,
        "mods": ["fix-qwen3.5-autoround"],
        "defaults": {
            "tensor_parallel": 2,
            "port": 9000,
            "gpu_memory_utilization": 0.9,
            "max_num_seqs": 2,
        },
    },
    {
        "name": "qwen3.5-122b-fp8",
        "model": "Qwen3.5-122B-FP8",
        "container": "vllm-node",
        "command": (
            "vllm serve Qwen3.5-122B-FP8 --host {host} --port {port} "
            "-tp {tensor_parallel} "
            "--gpu-memory-utilization {gpu_memory_utilization}"
        ),
        "description": "Qwen3.5 122B in FP8 format. Good balance of quality and memory usage.",
        "solo_only": False,
        "cluster_only": False,
        "mods": [],
        "defaults": {
            "tensor_parallel": 2,
            "port": 9010,
            "gpu_memory_utilization": 0.7,
            "max_num_seqs": 32,
        },
    },
    {
        "name": "minimax-m2-awq",
        "model": "QuantTrio/MiniMax-M2-AWQ",
        "container": "vllm-node",
        "command": (
            "vllm serve QuantTrio/MiniMax-M2-AWQ --host {host} --port {port} "
            "-tp {tensor_parallel} "
            "--gpu-memory-utilization {gpu_memory_utilization}"
        ),
        "description": "MiniMax-M2 with AWQ quantization. Strong reasoning and coding capabilities.",
        "solo_only": False,
        "cluster_only": True,
        "mods": [],
        "defaults": {
            "tensor_parallel": 2,
            "port": 9020,
            "gpu_memory_utilization": 0.7,
            "max_num_seqs": 16,
        },
    },
    {
        "name": "glm-4.7-flash",
        "model": "cyankiwi/GLM-4.7-Flash-AWQ",
        "container": "vllm-node-tf5",
        "command": (
            "vllm serve cyankiwi/GLM-4.7-Flash-AWQ --host {host} --port {port} "
            "-tp {tensor_parallel} "
            "--gpu-memory-utilization {gpu_memory_utilization}"
        ),
        "description": "GLM-4.7 Flash with AWQ. Fast inference with good quality.",
        "solo_only": False,
        "cluster_only": False,
        "mods": ["fix-glm-4.7-flash-AWQ"],
        "defaults": {
            "tensor_parallel": 1,
            "port": 9030,
            "gpu_memory_utilization": 0.8,
            "max_num_seqs": 64,
        },
    },
    {
        "name": "gpt-oss-120b",
        "model": "openai/gpt-oss-120b",
        "container": "vllm-node-mxfp4",
        "command": (
            "vllm serve openai/gpt-oss-120b --host {host} --port {port} "
            "-tp {tensor_parallel} "
            "--gpu-memory-utilization {gpu_memory_utilization}"
        ),
        "description": "GPT-OSS 120B from openai. Requires MXFP4 container for best performance.",
        "solo_only": True,
        "cluster_only": False,
        "mods": [],
        "defaults": {
            "tensor_parallel": 1,
            "port": 9040,
            "gpu_memory_utilization": 0.7,
            "max_num_seqs": 32,
        },
    },
    {
        "name": "nemotron-3-super",
        "model": "Nemotron-3-Super-120B",
        "container": "vllm-node",
        "command": (
            "vllm serve Nemotron-3-Super-120B --host {host} --port {port} "
            "-tp {tensor_parallel} "
            "--gpu-memory-utilization {gpu_memory_utilization}"
        ),
        "description": "NVIDIA Nemotron-3-Super 120B with NVFP4 quantization. Reasoning-focused model.",
        "solo_only": False,
        "cluster_only": False,
        "mods": ["nemotron-super"],
        "defaults": {
            "tensor_parallel": 2,
            "port": 9050,
            "gpu_memory_utilization": 0.85,
            "max_num_seqs": 16,
        },
    },
]


def _canned(recipe: dict[str, Any]) -> dict[str, Any]:
    """Shape a canned recipe like a parsed one."""
    out = {"id": recipe["name"], "is_customized": False, **recipe}
    out.setdefault("recipe_version", "1")
    out.setdefault("engine", None)
    out.setdefault("engines", ["vllm"])
    out.setdefault("engine_specs", {})
    out.setdefault("source", recipe_sources.SOURCE_UPSTREAM)
    out["params"] = dict(recipe.get("defaults", {}))
    out["engine_support"] = recipe_sources.engine_support(out)
    return out


def list_recipes(spark_path: Path | None = None) -> list[dict[str, Any]]:
    """List every recipe from every source, or the canned catalogue.

    Parsing is delegated to the shared module so simulation reports the same
    schema fields (recipe_version, engine, engines, params) and the same
    sources (including ``imported/``) as production.
    """
    if spark_path is None:
        spark_path = config.spark_vllm_dir
    payloads = recipe_sources.iter_recipe_payloads(spark_path)
    if payloads or recipe_sources.checkout_recipes_dir(spark_path) is not None:
        return [
            recipe_sources.summarize(p, custom_recipes.has_customization(p["id"]))
            for p in payloads
        ]
    return [_canned(r) for r in _RECIPES]


def get_recipe(recipe_id: str, spark_path: Path | None = None) -> dict[str, Any] | None:
    """Load a specific recipe by relative path id or display name.

    Falls back to the canned catalogue so a simulated server can still answer
    for a recipe that exists nowhere on disk.
    """
    if spark_path is None:
        spark_path = config.spark_vllm_dir
    recipe = recipe_sources.resolve_recipe(recipe_id, spark_path)
    if recipe is None:
        for canned in _RECIPES:
            if canned["name"] == recipe_id:
                recipe = _canned(canned)
                break
    if recipe is None:
        return None
    recipe_sources.apply_customization(
        recipe, custom_recipes.get_customization(recipe["id"])
    )
    return recipe


def build_launch_command(recipe: dict[str, Any], params: dict[str, Any]) -> str:
    """Build the serve command from a recipe and params."""
    return recipe_sources.render_command(recipe, params)
