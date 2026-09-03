"""Real recipe tools — parsing and listing across every recipe source.

Recipes are parsed through :mod:`spark_pulse.tools.recipe_schema`, so both the
v1 (upstream ``spark-vllm-docker``) and v2 (structured, multi-engine) formats
are understood. Parsing is *lenient* here: a half-written recipe still shows up
in the listing. Use ``recipe_schema.validate_recipe_file`` for strict checks.

Sources, in listing order:

* the ``recipes/`` directory of the spark-vllm-docker checkout (which is also
  where ``custom-*`` and ``oci-*`` symlinks live), and
* ``~/.config/spark-pulse/imported/recipes`` (ids prefixed ``imported/``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from spark_pulse.config import config
from spark_pulse.tools import custom_recipes, recipe_schema
from spark_pulse.tools.recipe_import import (
    IMPORT_SOURCE_PREFIX,
    imported_recipes_dir,
    iter_imported_recipe_files,
)
from spark_pulse.tools.recipe_schema import RecipeV1, RecipeV2

logger = logging.getLogger(__name__)

DEFAULT_CONTAINER = "vllm-node"

#: Placeholders that only Spark Pulse ever understood — upstream's
#: ``run-recipe.py`` has no such thing. Deprecated in favour of plain
#: ``{tensor_parallel}`` / ``{gpu_memory_utilization}`` / ``{max_model_len}``
#: substitutions; still expanded so existing recipes keep working.
DEPRECATED_PLACEHOLDERS = ("{-tp}", "{--gpu-memory-utilization}", "{--max-model-len}")


def _iter_recipe_files(recipe_dir: Path) -> list[Path]:
    """Return recipe file candidates, including extensionless symlinks.

    Custom recipe links are created as extensionless entries like
    `recipes/custom-my-recipe -> .../custom-my-recipe.yaml`.
    Those are valid recipe files and should be included in discovery.
    """
    seen: set[Path] = set()
    files: list[Path] = []

    if not recipe_dir.is_dir():
        return files

    for pattern in ("*.yaml", "*.yml"):
        for path in sorted(recipe_dir.rglob(pattern)):
            if path not in seen:
                seen.add(path)
                files.append(path)

    # Include extensionless symlinks that resolve to YAML files.
    for path in sorted(recipe_dir.iterdir()):
        if path.suffix:
            continue
        if not path.is_symlink():
            continue
        try:
            target = path.resolve(strict=True)
        except OSError:
            continue
        if not target.is_file() or target.suffix.lower() not in {".yaml", ".yml"}:
            continue
        if path not in seen:
            seen.add(path)
            files.append(path)

    return files


def _recipe_id_from_path(recipe_dir: Path, recipe_file: Path) -> str:
    rel = recipe_file.relative_to(recipe_dir)
    if recipe_file.suffix.lower() in {".yaml", ".yml"}:
        return str(rel.with_suffix(""))
    return str(rel)


def _imported_recipe_id(recipe_file: Path) -> str:
    rel = recipe_file.relative_to(imported_recipes_dir())
    return f"{IMPORT_SOURCE_PREFIX}/{rel.with_suffix('').as_posix()}"


def _candidate_files(spark_path: Path) -> list[tuple[str, Path]]:
    """Return ``(recipe_id, file)`` pairs for every known recipe source."""
    pairs: list[tuple[str, Path]] = []
    recipe_dir = spark_path / "recipes"
    for path in _iter_recipe_files(recipe_dir):
        pairs.append((_recipe_id_from_path(recipe_dir, path), path))
    for path in iter_imported_recipe_files():
        pairs.append((_imported_recipe_id(path), path))
    return pairs


# ── Schema → API shape ───────────────────────────────────────────────────────


def _parse_file(path: Path, recipe_id: str) -> RecipeV1 | RecipeV2 | None:
    """Parse one recipe file leniently; ``None`` when it is unusable."""
    try:
        return recipe_schema.parse_recipe(path, strict=False, source_label=recipe_id)
    except recipe_schema.RecipeValidationError as exc:
        logger.debug("Skipping recipe %s: %s", recipe_id, exc)
        return None
    except OSError as exc:
        logger.debug("Skipping recipe %s: %s", recipe_id, exc)
        return None


def _to_payload(
    recipe: RecipeV1 | RecipeV2, recipe_id: str, fallback_name: str
) -> dict[str, Any]:
    """Flatten a parsed recipe into the dict shape the API has always served.

    v1 callers keep ``container`` / ``defaults`` / ``command``; every recipe
    additionally reports ``recipe_version``, ``engine``, ``engines`` and
    ``params``.
    """
    if isinstance(recipe, RecipeV2):
        spec = recipe.engine_spec()
        params = recipe.params.as_dict()
        payload = {
            "id": recipe_id,
            "name": recipe.name or fallback_name,
            "model": recipe.model or "unknown",
            "container": (spec.image if spec and spec.image else DEFAULT_CONTAINER),
            "command": recipe.command or (spec.command if spec else None) or "",
            "description": recipe.description,
            "mods": list(spec.mods) if spec else [],
            "defaults": dict(params),
            "params": dict(params),
            "env": dict(spec.env) if spec else {},
            "build_args": [],
            "solo_only": recipe.constraints.solo_only,
            "cluster_only": recipe.constraints.cluster_only,
            "min_nodes": recipe.constraints.min_nodes,
            "recipe_version": "2",
            "engine": recipe.engine,
            "engines": recipe.engine_names(),
        }
        return payload

    return {
        "id": recipe_id,
        "name": recipe.name or fallback_name,
        "model": recipe.model or "unknown",
        "container": recipe.container or DEFAULT_CONTAINER,
        "command": recipe.command,
        "description": recipe.description,
        "mods": list(recipe.mods),
        "defaults": dict(recipe.defaults),
        "params": dict(recipe.defaults),
        "env": dict(recipe.env),
        "build_args": list(recipe.build_args),
        "solo_only": recipe.solo_only,
        "cluster_only": recipe.cluster_only,
        "min_nodes": None,
        "recipe_version": "1",
        "engine": None,
        "engines": recipe.engines,
    }


#: Keys a listing entry carries. The mock module reuses this so both listings
#: stay identical.
SUMMARY_FIELDS = (
    "id",
    "name",
    "model",
    "container",
    "description",
    "solo_only",
    "cluster_only",
    "mods",
    "defaults",
    "params",
    "recipe_version",
    "engine",
    "engines",
)


def iter_recipe_payloads(spark_path: Path) -> list[dict[str, Any]]:
    """Parse every recipe from every source into the flat payload shape.

    Shared with the mock module; the caller decides which fields to keep and
    how to resolve customizations.
    """
    payloads: list[dict[str, Any]] = []
    for recipe_id, path in _candidate_files(spark_path):
        parsed = _parse_file(path, recipe_id)
        if parsed is None:
            continue
        payloads.append(_to_payload(parsed, recipe_id, Path(path).stem))
    return payloads


def resolve_recipe(recipe_id: str, spark_path: Path) -> dict[str, Any] | None:
    """Find and parse one recipe, without applying any customization."""
    recipe_dir = spark_path / "recipes"

    candidates: list[tuple[str, Path]] = []
    if recipe_id.startswith(f"{IMPORT_SOURCE_PREFIX}/"):
        rel = recipe_id[len(IMPORT_SOURCE_PREFIX) + 1 :]
        base = imported_recipes_dir()
        for suffix in (".yaml", ".yml", ""):
            path = base / f"{rel}{suffix}"
            if path.is_file():
                candidates.append((recipe_id, path))
                break
    else:
        for path in (
            recipe_dir / recipe_id,
            recipe_dir / f"{recipe_id}.yaml",
            recipe_dir / f"{recipe_id}.yml",
        ):
            if path.exists():
                candidates.append((_recipe_id_from_path(recipe_dir, path), path))

    if not candidates and recipe_id:
        candidates = _candidate_files(spark_path)

    for candidate_id, path in candidates:
        parsed = _parse_file(path, candidate_id)
        if parsed is None:
            continue
        if candidate_id != recipe_id and parsed.name != recipe_id:
            continue
        return _to_payload(parsed, candidate_id, Path(path).stem)
    return None


# ── Public API ───────────────────────────────────────────────────────────────


def list_recipes(spark_path: Path | None = None) -> list[dict[str, Any]]:
    """List every recipe from every source."""
    spark_path = spark_path or Path(config.spark_vllm_path)
    recipes: list[dict[str, Any]] = []
    for payload in iter_recipe_payloads(spark_path):
        summary = {field: payload[field] for field in SUMMARY_FIELDS}
        summary["is_customized"] = custom_recipes.has_customization(payload["id"])
        recipes.append(summary)
    return recipes


def get_recipe(recipe_id: str, spark_path: Path | None = None) -> dict[str, Any] | None:
    """Load a specific recipe by relative path id or display name."""
    spark_path = spark_path or Path(config.spark_vllm_path)
    recipe = resolve_recipe(recipe_id, spark_path)
    if recipe is None:
        return None
    apply_customization(recipe, recipe["id"], custom_recipes.get_customization)
    return recipe


def apply_customization(
    recipe: dict[str, Any],
    recipe_id: str,
    lookup: Any,
) -> None:
    """Merge persisted user overrides on top of the parsed recipe, in place.

    ``lookup`` is the ``get_customization`` callable to use, so the mock module
    can pass its own store.
    """
    customization = lookup(recipe_id)
    if not customization:
        return

    custom_defaults = customization.get("defaults")
    if isinstance(custom_defaults, dict):
        recipe["defaults"] = {**recipe.get("defaults", {}), **custom_defaults}
        recipe["params"] = dict(recipe["defaults"])

    for field in ("command", "env", "build_args", "container", "model", "mods"):
        if field in customization:
            recipe[field] = customization[field]


def build_launch_command(recipe: dict[str, Any], params: dict[str, Any]) -> str:
    """Build the serve command from a recipe and params.

    The plain ``{port}`` / ``{host}`` / ``{tensor_parallel}`` placeholders are
    upstream's. The bracketed-flag forms in :data:`DEPRECATED_PLACEHOLDERS` were
    only ever a Spark Pulse extension; they are still expanded but warn.
    """
    command = recipe.get("command", "") or ""

    replacements: dict[str, str] = {
        "port": str(params.get("port", 8000)),
        "host": str(params.get("host", "0.0.0.0")),
    }

    tp = params.get("tensor_parallel", params.get("tp"))
    if tp:
        replacements["tensor_parallel"] = str(int(tp))
        replacements["-tp"] = f"--tensor-parallel-size {int(tp)}"

    gpu_mem = params.get("gpu_memory_utilization", params.get("gpu_mem_util"))
    if gpu_mem:
        replacements["gpu_memory_utilization"] = str(gpu_mem)
        replacements["--gpu-memory-utilization"] = str(gpu_mem)

    max_len = params.get("max_model_len")
    if max_len:
        replacements["max_model_len"] = str(max_len)
        replacements["--max-model-len"] = str(max_len)

    used = [p for p in DEPRECATED_PLACEHOLDERS if p in command]
    if used:
        logger.warning(
            "Recipe %r uses deprecated Spark Pulse placeholders %s; "
            "use the plain {tensor_parallel}/{gpu_memory_utilization}/"
            "{max_model_len} forms instead.",
            recipe.get("id", recipe.get("name", "<unknown>")),
            ", ".join(used),
        )

    for key, value in replacements.items():
        command = command.replace("{" + key + "}", value)
        command = command.replace("{" + key.lower() + "}", value)
    return command
