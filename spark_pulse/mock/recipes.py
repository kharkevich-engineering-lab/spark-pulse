"""Mock recipe tools — 6 realistic DGX Spark deployment recipes."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from typing import Any

import types
import yaml
from filelock import FileLock

from spark_pulse.config import config

# Parsing/resolution logic is shared with the real module so both listings
# report the same schema fields and sources.
import spark_pulse.tools.recipes as _real_recipes

# Default mock recipes (used when no spark_path is provided)
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

_CUSTOM_RECIPES_DIR = Path.home() / ".config" / "spark-pulse" / "custom_recipes"
_CUSTOM_MODS_DIR = Path.home() / ".config" / "spark-pulse" / "custom_mods"
_CUSTOM_RECIPES_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "custom_recipes.json"
)
_CUSTOM_RECIPES_LOCK = FileLock(f"{_CUSTOM_RECIPES_PATH}.lock", timeout=30)


def _iter_recipe_files(recipe_dir: Path):
    """Iterate over recipe YAML files in the recipes directory."""
    if not recipe_dir.is_dir():
        return
    for ext in ("yaml", "yml"):
        for yaml_file in recipe_dir.rglob(f"*.{ext}"):
            yield yaml_file
        # Also yield extensionless files that are symlinks or directories
        for item in recipe_dir.rglob("*"):
            if item.is_symlink() and not item.suffix:
                yield item


def _recipe_id_from_path(recipe_dir: Path, recipe_file: Path) -> str:
    """Derive recipe ID from file path."""
    rel = recipe_file.relative_to(recipe_dir)
    if recipe_file.suffix.lower() in {".yaml", ".yml"}:
        return str(rel.with_suffix(""))
    return str(rel)


def _load_customizations() -> dict[str, Any]:
    """Load custom recipe customizations."""
    custom_path = getattr(custom_recipes, "_CUSTOM_PATH", None)
    if custom_path is None:
        # Fallback to the default path
        custom_path = _CUSTOM_RECIPES_PATH
    if not custom_path.exists():
        return {}
    try:
        with open(custom_path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_customizations(data: dict[str, Any]) -> None:
    """Save custom recipe customizations."""
    custom_path = getattr(custom_recipes, "_CUSTOM_PATH", None)
    if custom_path is None:
        custom_path = _CUSTOM_RECIPES_PATH
    custom_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = custom_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(custom_path)


@contextmanager
def _atomic_customizations():
    """Context manager for atomic read-modify-write of custom recipes."""
    with _CUSTOM_RECIPES_LOCK:
        data = _load_customizations()
        yield data
        _save_customizations(data)


def _load_customized_recipe(
    recipe_id: str, spark_path: Path | None = None
) -> dict | None:
    """Load a recipe from spark_path if it exists."""
    spark_path = spark_path or Path(__file__).resolve().parent.parent.parent
    recipe_dir = Path(spark_path) / "recipes"

    # Try direct path
    candidates = [
        recipe_dir / recipe_id,
        recipe_dir / f"{recipe_id}.yaml",
        recipe_dir / f"{recipe_id}.yml",
    ]
    candidates = [c for c in candidates if c.exists()]

    if not candidates:
        # Try to find by id in all recipe files
        for yaml_file in _iter_recipe_files(recipe_dir):
            try:
                with open(yaml_file) as f:
                    data = yaml.safe_load(f)
                if data and _recipe_id_from_path(recipe_dir, yaml_file) == recipe_id:
                    candidates.append(yaml_file)
                    break
            except (yaml.YAMLError, OSError):
                continue

    for candidate in candidates:
        try:
            with open(candidate) as f:
                data = yaml.safe_load(f)
            if not data:
                continue
            candidate_id = _recipe_id_from_path(recipe_dir, candidate)
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
            return recipe
        except (yaml.YAMLError, OSError):
            continue
    return None


def _canned(recipe: dict[str, Any]) -> dict[str, Any]:
    """Shape a canned recipe like a parsed one."""
    out = {"id": recipe["name"], "is_customized": False, **recipe}
    out.setdefault("recipe_version", "1")
    out.setdefault("engine", None)
    out.setdefault("engines", ["vllm"])
    out["params"] = dict(recipe.get("defaults", {}))
    return out


def list_recipes(spark_path: Path | None = None) -> list[dict[str, Any]]:
    """Scan all YAML files in the recipes directory (when spark_path provided),
    or return mock recipe list when no spark_path.

    Parsing is delegated to the real module so simulation mode reports the
    same schema fields (recipe_version, engine, engines, params) and the same
    sources (including `imported/`) as production.
    """
    if spark_path is None:
        spark_path = Path(config.spark_vllm_path)
    spark_path = Path(spark_path)
    payloads = _real_recipes.iter_recipe_payloads(spark_path)
    if payloads or (spark_path / "recipes").is_dir():
        recipes = []
        for payload in payloads:
            summary = {f: payload[f] for f in _real_recipes.SUMMARY_FIELDS}
            summary["is_customized"] = bool(has_customization(payload["id"]))
            recipes.append(summary)
        return recipes
    return [_canned(r) for r in _RECIPES]


def get_recipe(recipe_id: str, spark_path: Path | None = None) -> dict[str, Any] | None:
    """Load a specific recipe by relative path id or display name.
    Uses spark_path if provided, otherwise falls back to config.spark_vllm_path
    and finally returns mock data."""
    if spark_path is None:
        spark_path = Path(config.spark_vllm_path)
    recipe = _real_recipes.resolve_recipe(recipe_id, Path(spark_path))
    if recipe is None:
        for canned in _RECIPES:
            if canned["name"] == recipe_id:
                recipe = _canned(canned)
                break
    if recipe is None:
        return None
    _real_recipes.apply_customization(recipe, recipe["id"], get_customization)
    return recipe


def build_launch_command(recipe: dict[str, Any], params: dict[str, Any]) -> str:
    """Build a mock vLLM launch command."""
    # Use the recipe command template if provided
    if "command" in recipe:
        cmd = recipe["command"]
    else:
        cmd = "vllm serve {model}"

    # Replace tokens like {host}, {port}, {model} with --flag value
    host = params.get("host", "0.0.0.0")
    port = params.get("port", 8000)
    model = recipe.get("model", "unknown")
    tp = params.get("tensor_parallel", params.get("tp"))
    gpu_mem = params.get("gpu_memory_utilization")
    max_len = params.get("max_model_len")

    cmd = cmd.replace("{host}", f"--host {host}")
    cmd = cmd.replace("{port}", f"--port {port}")
    cmd = cmd.replace("{model}", model)
    cmd = cmd.replace("{}", model)

    if tp:
        cmd = cmd.replace("{-tp}", f"--tensor-parallel-size {tp}")
    if gpu_mem:
        cmd = cmd.replace(
            "{--gpu-memory-utilization}", f"--gpu-memory-utilization {gpu_mem}"
        )
    if max_len:
        cmd = cmd.replace("{--max-model-len}", f"--max-model-len {max_len}")

    return cmd


# ── Custom recipes (mirrors real module API) ─────────────────────────────────


def has_customization(recipe_id: str) -> bool:
    """Check if a recipe has customizations."""
    data = _load_customizations()
    return recipe_id in data


def get_customization(recipe_id: str) -> dict[str, Any] | None:
    """Get customization for a recipe."""
    data = _load_customizations()
    return data.get(recipe_id)


def get_customizations() -> dict[str, Any]:
    """Get all customizations."""
    return _load_customizations()


def save_customization(recipe_id: str, customization: dict[str, Any]) -> bool:
    """Save a recipe customization."""
    data = _load_customizations()
    # Store all meaningful customizable fields
    customizable = {
        "port",
        "tensor_parallel",
        "gpu_memory_utilization",
        "max_num_seqs",
        "env",
        "build_args",
        "privileged",
        "command",
        "mods",
    }
    filtered = {k: v for k, v in customization.items() if k in customizable}
    if filtered:
        data[recipe_id] = filtered
    else:
        data.pop(recipe_id, None)
    _save_customizations(data)
    return True


def delete_customization(recipe_id: str) -> bool:
    """Delete a recipe customization."""
    data = _load_customizations()
    if recipe_id in data:
        del data[recipe_id]
        _save_customizations(data)
        return True
    return False


def get_customized_recipe(
    recipe_id: str, spark_path: Path | None = None
) -> dict[str, Any] | None:
    """Get a recipe with customizations applied."""
    recipe = get_recipe(recipe_id, spark_path) or _load_customized_recipe(
        recipe_id, spark_path
    )
    if recipe is None:
        return None
    customization = get_customization(recipe_id)
    if customization:
        custom_defaults = customization.get("defaults")
        if isinstance(custom_defaults, dict):
            recipe["defaults"] = {**recipe.get("defaults", {}), **custom_defaults}
    return recipe


def list_custom_recipes() -> list[dict[str, Any]]:
    """List custom recipe files in the custom recipes directory."""
    if not _CUSTOM_RECIPES_DIR.exists():
        return []
    custom_recipes = []
    for yaml_file in _CUSTOM_RECIPES_DIR.rglob("*.yaml"):
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
            if data:
                custom_recipes.append(
                    {
                        "name": yaml_file.stem,
                        "path": str(yaml_file),
                        "content": yaml_file.read_text(),
                    }
                )
        except (yaml.YAMLError, OSError):
            custom_recipes.append(
                {
                    "name": yaml_file.stem,
                    "path": str(yaml_file),
                    "error": "Failed to parse YAML",
                }
            )
    return custom_recipes


def get_recipe_content(recipe_name: str) -> dict[str, Any] | None:
    """Get the content of a custom recipe file."""
    recipe_path = _CUSTOM_RECIPES_DIR / f"{recipe_name}.yaml"
    if not recipe_path.exists():
        return None
    return {
        "name": recipe_name,
        "path": str(recipe_path),
        "content": recipe_path.read_text(),
    }


def save_recipe_content(recipe_name: str, content: str) -> bool:
    """Save content to a custom recipe file."""
    # Check for path traversal
    if ".." in recipe_name or "/" in recipe_name or "\\" in recipe_name:
        raise ValueError("Recipe name contains invalid characters")
    recipe_path = _CUSTOM_RECIPES_DIR / f"{recipe_name}.yaml"
    recipe_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML: {e}")
    recipe_path.write_text(content)
    return True


def delete_recipe_content(recipe_name: str) -> bool:
    """Delete a custom recipe file."""
    recipe_path = _CUSTOM_RECIPES_DIR / f"{recipe_name}.yaml"
    if recipe_path.exists():
        recipe_path.unlink()
        return True
    return False


def list_custom_mods() -> list[dict[str, Any]]:
    """List custom mod directories in the custom mods directory."""
    if not _CUSTOM_MODS_DIR.exists():
        return []
    mods = []
    for item in sorted(_CUSTOM_MODS_DIR.iterdir()):
        if item.is_dir() and (item / "run.sh").exists():
            description = ""
            run_sh = item / "run.sh"
            for line in run_sh.read_text().split("\n"):
                if line.startswith("#"):
                    description = line.lstrip("# ").strip()
                else:
                    break
            mods.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "description": description,
                }
            )
    return mods


def get_mod_files(mod_name: str) -> dict[str, Any] | None:
    """Get files and content for a custom mod."""
    mod_dir = _CUSTOM_MODS_DIR / mod_name
    if not mod_dir.exists():
        return None
    files = []
    for f in sorted(mod_dir.rglob("*")):
        if f.is_file():
            rel = f.relative_to(mod_dir)
            try:
                content = f.read_text()
            except (OSError, UnicodeDecodeError):
                content = None
            files.append(
                {
                    "path": str(rel),
                    "content": content,
                }
            )
    return {"name": mod_name, "path": str(mod_dir), "files": files}


def save_mod_files(mod_name: str, files: list[dict[str, Any]]) -> bool:
    """Save files for a custom mod."""
    if ".." in mod_name or "/" in mod_name or "\\" in mod_name:
        raise ValueError("Mod name contains invalid characters")
    mod_dir = _CUSTOM_MODS_DIR / mod_name
    mod_dir.mkdir(parents=True, exist_ok=True)
    for file_info in files:
        file_path = mod_dir / file_info["path"]
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(file_info["content"])
    return True


def delete_mod(mod_name: str) -> bool:
    """Delete a custom mod directory."""
    mod_dir = _CUSTOM_MODS_DIR / mod_name
    if mod_dir.exists():
        import shutil

        shutil.rmtree(mod_dir)
        return True
    return False


# ── Test compatibility: create custom_recipes submodule ──────────────────────

_custom_recipes_mod = types.ModuleType("custom_recipes")
_custom_recipes_mod.__file__ = __file__
_custom_recipes_mod._CUSTOM_PATH = _CUSTOM_RECIPES_PATH
_custom_recipes_mod.get_customization = get_customization
_custom_recipes_mod.save_customization = save_customization
_custom_recipes_mod.delete_customization = delete_customization
_custom_recipes_mod.has_customization = has_customization
custom_recipes = _custom_recipes_mod
