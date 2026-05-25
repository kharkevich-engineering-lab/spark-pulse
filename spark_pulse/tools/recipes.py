"""Real recipe tools — YAML parsing from spark-vllm-docker."""

from pathlib import Path
from typing import Any

import yaml

from spark_pulse.config import config
from spark_pulse.tools import custom_recipes


def list_recipes(spark_path: Path | None = None) -> list[dict[str, Any]]:
    """Scan all YAML files in the recipes directory."""
    spark_path = spark_path or Path(config.spark_vllm_path)
    recipe_dir = spark_path / "recipes"
    if not recipe_dir.is_dir():
        return []
    recipes = []
    for yaml_file in sorted(recipe_dir.rglob("*.yaml")):
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
            recipe_id = str(yaml_file.relative_to(recipe_dir).with_suffix(""))
            if data:
                recipes.append(
                    {
                        "id": recipe_id,
                        "name": data.get("name", yaml_file.stem),
                        "model": data.get("model", "unknown"),
                        "container": data.get("container", "vllm-node"),
                        "description": data.get("description", ""),
                        "solo_only": bool(data.get("solo_only", False)),
                        "cluster_only": bool(data.get("cluster_only", False)),
                        "mods": data.get("mods", []),
                        "defaults": data.get("defaults", {}),
                        "is_customized": custom_recipes.has_customization(recipe_id),
                    }
                )
        except (yaml.YAMLError, OSError):
            continue
    return recipes


def get_recipe(recipe_id: str, spark_path: Path | None = None) -> dict[str, Any] | None:
    """Load a specific recipe by relative path id or display name."""
    spark_path = spark_path or Path(config.spark_vllm_path)
    recipe_dir = spark_path / "recipes"
    yaml_file = recipe_dir / f"{recipe_id}.yaml"
    candidates = [yaml_file] if yaml_file.exists() else []
    if not candidates and recipe_id:
        candidates = sorted(recipe_dir.rglob("*.yaml"))

    for candidate in candidates:
        try:
            with open(candidate) as f:
                data = yaml.safe_load(f)
            if not data:
                continue
            candidate_id = str(candidate.relative_to(recipe_dir).with_suffix(""))
            if candidate_id != recipe_id and data.get("name") != recipe_id:
                continue
            recipe = {
                "id": candidate_id,
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

            # Apply persisted user customizations on top of YAML data.
            customization = custom_recipes.get_customization(candidate_id)
            if customization:
                custom_defaults = customization.get("defaults")
                if isinstance(custom_defaults, dict):
                    recipe["defaults"] = {
                        **recipe.get("defaults", {}),
                        **custom_defaults,
                    }

                for field in (
                    "command",
                    "env",
                    "build_args",
                    "container",
                    "model",
                    "mods",
                ):
                    if field in customization:
                        recipe[field] = customization[field]

            return recipe
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
