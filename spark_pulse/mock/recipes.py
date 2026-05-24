"""Mock recipe tools — 6 realistic DGX Spark deployment recipes."""

from __future__ import annotations

from typing import Any

_RECIPES = [
    {
        "name": "qwen3.5-397b-int4",
        "model": "Intel/Qwen3.5-397B-INT4-AutoRound",
        "container": "vllm-node-tf5",
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


def list_recipes() -> list[dict[str, Any]]:
    """Return the mock recipe list."""
    return [{"id": r["name"], **r} for r in _RECIPES]


def get_recipe(recipe_id: str) -> dict[str, Any] | None:
    """Look up a specific recipe by name."""
    for r in _RECIPES:
        if r["name"] == recipe_id:
            return {"id": r["name"], **r}
    return None


def build_launch_command(recipe: dict[str, Any], params: dict[str, Any]) -> str:
    """Build a mock vLLM launch command."""
    cmd = "# Simulation: vllm serve"
    replacements = {
        "port": str(params.get("port", 8000)),
        "host": str(params.get("host", "0.0.0.0")),
    }
    tp = params.get("tensor_parallel", params.get("tp"))
    if tp:
        cmd += f" -tp {tp}"
    for key, value in replacements.items():
        cmd = cmd.replace("{" + key + "}", value)
    return cmd
