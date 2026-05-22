"""Real recipe tools — YAML parsing from spark-vllm-docker."""

import os
from pathlib import Path
from typing import Any

import yaml

from spark_pulse.config import config


def list_recipes(spark_path: Path | None = None) -> list[dict[str, Any]]:
    """Scan all YAML files in the recipes directory."""
    spark_path = spark_path or Path(config.spark_vllm_path)
    recipe_dir = spark_path / "recipes"
    if not recipe_dir.is_dir():
        return []
    recipes = []
    for yaml_file in sorted(recipe_dir.glob("*.yaml")):
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
            if data:
                recipes.append({
                    "id": yaml_file.stem,
                    "name": data.get("name", yaml_file.stem),
                    "model": data.get("model", "unknown"),
                    "container": data.get("container", "vllm-node"),
                    "description": data.get("description", ""),
                    "solo_only": bool(data.get("solo_only", False)),
                    "cluster_only": bool(data.get("cluster_only", False)),
                    "mods": data.get("mods", []),
                    "defaults": data.get("defaults", {}),
                })
        except (yaml.YAMLError, OSError):
            continue
    return recipes


def get_recipe(recipe_id: str, spark_path: Path | None = None) -> dict[str, Any] | None:
    """Load a specific recipe by filename."""
    spark_path = spark_path or Path(config.spark_vllm_path)
    yaml_file = spark_path / "recipes" / f"{recipe_id}.yaml"
    candidates = [yaml_file] if yaml_file.exists() else []
    if not candidates and recipe_id:
        candidates = sorted((spark_path / "recipes").glob("*.yaml"))

    for candidate in candidates:
        try:
            with open(candidate) as f:
                data = yaml.safe_load(f)
            if not data:
                continue
            if candidate.stem != recipe_id and data.get("name") != recipe_id:
                continue
            return {
                "id": candidate.stem,
                "name": data.get("name", candidate.stem),
                "model": data.get("model", "unknown"),
                "container": data.get("container", "vllm-node"),
                "command": data.get("command", ""),
                "description": data.get("description", ""),
                "mods": data.get("mods", []),
                "defaults": data.get("defaults", {}),
                "env": data.get("env", {}),
                "build_args": data.get("build_args", []),
                "solo_only": bool(data.get("solo_only", False)),
                "cluster_only": bool(data.get("cluster_only", False)),
                "recipe_version": data.get("recipe_version", "1"),
            }
        except (yaml.YAMLError, OSError):
            continue
    return None


def build_launch_command(recipe: dict[str, Any], params: dict[str, Any]) -> str:
    """Build the vLLM serve command from a recipe and params."""
    command = recipe.get("command", "")
    replacements = {
        "port": str(params.get("port", 8000)),
        "host": str(params.get("host", "0.0.0.0")),
    }
    tp = params.get("tensor_parallel", params.get("tp"))
    if tp:
        replacements["-tp"] = f"--tensor-parallel-size {int(tp)}"
    gpu_mem = params.get("gpu_memory_utilization", params.get("gpu_mem_util"))
    if gpu_mem:
        replacements["--gpu-memory-utilization"] = str(gpu_mem)
    max_len = params.get("max_model_len")
    if max_len:
        replacements["--max-model-len"] = str(max_len)
    for key, value in replacements.items():
        command = command.replace("{" + key + "}", value)
        command = command.replace("{" + key.lower() + "}", value)
    return command
