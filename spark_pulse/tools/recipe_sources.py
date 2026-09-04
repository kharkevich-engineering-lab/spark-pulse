"""Recipe discovery, parsing and flattening, shared by the real and mock tools.

Real-only on purpose: like ``labels`` this module has no mock twin, so
both ``tools.recipes`` and ``mock.recipes`` can import it without disturbing the
``SIMULATION_MODE`` module switch in ``tools/__init__.py``. It knows nothing
about customization storage — that differs between the two — and nothing about
rendering a launch command for an engine.

Sources, in listing order:

* ``spark_pulse/recipes`` shipped inside the package (ids prefixed
  ``bundled/``) — engine-neutral v2 recipes that work without any checkout,
* the ``recipes/`` directory of the spark-vllm-docker checkout (which is also
  where ``custom-*`` and ``oci-*`` symlinks live), and
* ``~/.config/spark-pulse/imported/recipes`` (ids prefixed ``imported/``).

Every payload carries a ``source`` label so the UI can tell them apart.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from spark_pulse.tools import recipe_schema
from spark_pulse.tools.recipe_import import (
    IMPORT_SOURCE_PREFIX,
    imported_recipes_dir,
    iter_imported_recipe_files,
)
from spark_pulse.tools.recipe_schema import RecipeV1, RecipeV2

logger = logging.getLogger(__name__)

DEFAULT_CONTAINER = "vllm-node"

#: Recipes shipped inside the package. Ids are prefixed so they never collide
#: with an upstream, ``custom-``, ``oci-`` or ``imported/`` recipe.
BUNDLED_SOURCE_PREFIX = "bundled"
BUNDLED_RECIPES_DIR = Path(__file__).resolve().parent.parent / "recipes"

#: Source labels a payload can carry, in listing order.
SOURCE_BUNDLED = "bundled"
SOURCE_CUSTOM = "custom"
SOURCE_OCI = "oci"
SOURCE_IMPORTED = "imported"
SOURCE_UPSTREAM = "upstream"

#: Placeholders that only Spark Pulse ever understood — upstream's
#: ``run-recipe.py`` has no such thing. Deprecated in favour of plain
#: ``{tensor_parallel}`` / ``{gpu_memory_utilization}`` / ``{max_model_len}``
#: substitutions; still expanded so existing recipes keep working.
DEPRECATED_PLACEHOLDERS = ("{-tp}", "{--gpu-memory-utilization}", "{--max-model-len}")

#: Keys a listing entry carries. Both listings use this so they stay identical.
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
    "engine_support",
    "source",
)


# ── Discovery ────────────────────────────────────────────────────────────────


def iter_recipe_files(recipe_dir: Path) -> list[Path]:
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


def recipe_id_from_path(recipe_dir: Path, recipe_file: Path) -> str:
    """Derive a recipe id from its path relative to the recipes directory."""
    rel = recipe_file.relative_to(recipe_dir)
    if recipe_file.suffix.lower() in {".yaml", ".yml"}:
        return str(rel.with_suffix(""))
    return str(rel)


def bundled_recipes_dir() -> Path:
    """Directory holding the recipes shipped inside the package."""
    return BUNDLED_RECIPES_DIR


def iter_bundled_recipe_files() -> list[Path]:
    """Every bundled recipe file, sorted."""
    directory = bundled_recipes_dir()
    if not directory.is_dir():
        return []
    files: list[Path] = []
    for pattern in ("*.yaml", "*.yml"):
        files.extend(directory.rglob(pattern))
    return sorted(set(files))


def _bundled_recipe_id(recipe_file: Path) -> str:
    rel = recipe_file.relative_to(bundled_recipes_dir())
    return f"{BUNDLED_SOURCE_PREFIX}/{rel.with_suffix('').as_posix()}"


def source_of(recipe_id: str) -> str:
    """Label the source a recipe id came from."""
    if recipe_id.startswith(f"{BUNDLED_SOURCE_PREFIX}/"):
        return SOURCE_BUNDLED
    if recipe_id.startswith(f"{IMPORT_SOURCE_PREFIX}/"):
        return SOURCE_IMPORTED
    leaf = recipe_id.rsplit("/", 1)[-1]
    if leaf.startswith("custom-"):
        return SOURCE_CUSTOM
    if leaf.startswith("oci-"):
        return SOURCE_OCI
    return SOURCE_UPSTREAM


def _imported_recipe_id(recipe_file: Path) -> str:
    rel = recipe_file.relative_to(imported_recipes_dir())
    return f"{IMPORT_SOURCE_PREFIX}/{rel.with_suffix('').as_posix()}"


def candidate_files(spark_path: Path) -> list[tuple[str, Path]]:
    """Return ``(recipe_id, file)`` pairs for every known recipe source."""
    pairs: list[tuple[str, Path]] = []
    for path in iter_bundled_recipe_files():
        pairs.append((_bundled_recipe_id(path), path))
    recipe_dir = spark_path / "recipes"
    for path in iter_recipe_files(recipe_dir):
        pairs.append((recipe_id_from_path(recipe_dir, path), path))
    for path in iter_imported_recipe_files():
        pairs.append((_imported_recipe_id(path), path))
    return pairs


# ── Schema → API shape ───────────────────────────────────────────────────────


def parse_file(path: Path, recipe_id: str) -> RecipeV1 | RecipeV2 | None:
    """Parse one recipe file leniently; ``None`` when it is unusable."""
    try:
        return recipe_schema.parse_recipe(path, strict=False, source_label=recipe_id)
    except recipe_schema.RecipeValidationError as exc:
        logger.debug("Skipping recipe %s: %s", recipe_id, exc)
        return None
    except OSError as exc:
        logger.debug("Skipping recipe %s: %s", recipe_id, exc)
        return None


def engine_support(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-engine ``supported`` / ``reason`` for one flattened recipe payload.

    Asking the engine plugins themselves is the point: the deploy form must
    offer exactly what ``plan()`` will accept, with the same wording when it
    refuses. Engines are not switchable, so importing them here is safe; the
    import is local to keep ``tools`` import order unperturbed.
    """
    from spark_pulse.engines import EngineError, get_registry

    try:
        registry = get_registry()
        specs = registry.list()
    except Exception:  # pragma: no cover - defensive: never break listing
        logger.debug("Engine registry unavailable; reporting no engine support")
        return []

    out: list[dict[str, Any]] = []
    for name in sorted({spec.engine for spec in specs}):
        try:
            engine = registry.engine(name)
            supported, reason = engine.supports(payload)
        except EngineError as exc:
            supported, reason = False, str(exc)
        out.append(
            {
                "engine": name,
                "supported": bool(supported),
                "reason": reason,
                "enabled": bool(registry.enabled(name)),
            }
        )
    return out


def _engine_specs(recipe: RecipeV2) -> dict[str, dict[str, Any]]:
    """Every per-engine block, so a payload keeps args/env/image per engine.

    The flat payload names one engine's image, mods and env at the top level
    (whichever the recipe defaults to); without this the other engines' blocks
    would be lost the moment a recipe left the parser.
    """
    return {
        name: {
            "image": spec.image,
            "mods": list(spec.mods),
            "env": dict(spec.env),
            "args": spec.args_string(),
            "command": spec.command,
        }
        for name, spec in recipe.engines.items()
    }


def to_payload(
    recipe: RecipeV1 | RecipeV2, recipe_id: str, fallback_name: str
) -> dict[str, Any]:
    """Flatten a parsed recipe into the dict shape the API has always served.

    v1 callers keep ``container`` / ``defaults`` / ``command``; every recipe
    additionally reports ``recipe_version``, ``engine``, ``engines``,
    ``engine_specs``, ``engine_support``, ``source`` and ``params``.
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
            "engine_specs": _engine_specs(recipe),
            "source": source_of(recipe_id),
        }
        payload["engine_support"] = engine_support(payload)
        return payload

    payload = {
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
        "engine_specs": {},
        "source": source_of(recipe_id),
    }
    payload["engine_support"] = engine_support(payload)
    return payload


def iter_recipe_payloads(spark_path: Path) -> list[dict[str, Any]]:
    """Parse every recipe from every source into the flat payload shape."""
    payloads: list[dict[str, Any]] = []
    for recipe_id, path in candidate_files(spark_path):
        parsed = parse_file(path, recipe_id)
        if parsed is None:
            continue
        payloads.append(to_payload(parsed, recipe_id, Path(path).stem))
    return payloads


def resolve_recipe(recipe_id: str, spark_path: Path) -> dict[str, Any] | None:
    """Find and parse one recipe by id or display name, without customization."""
    recipe_dir = spark_path / "recipes"

    candidates: list[tuple[str, Path]] = []
    if recipe_id.startswith(f"{BUNDLED_SOURCE_PREFIX}/"):
        rel = recipe_id[len(BUNDLED_SOURCE_PREFIX) + 1 :]
        base = bundled_recipes_dir()
        for suffix in (".yaml", ".yml"):
            path = base / f"{rel}{suffix}"
            if path.is_file():
                candidates.append((recipe_id, path))
                break
    elif recipe_id.startswith(f"{IMPORT_SOURCE_PREFIX}/"):
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
                candidates.append((recipe_id_from_path(recipe_dir, path), path))

    if not candidates and recipe_id:
        candidates = candidate_files(spark_path)

    for candidate_id, path in candidates:
        parsed = parse_file(path, candidate_id)
        if parsed is None:
            continue
        if candidate_id != recipe_id and parsed.name != recipe_id:
            continue
        return to_payload(parsed, candidate_id, Path(path).stem)
    return None


def summarize(payload: dict[str, Any], is_customized: bool) -> dict[str, Any]:
    """Reduce a full payload to a listing entry."""
    summary = {field: payload[field] for field in SUMMARY_FIELDS}
    summary["is_customized"] = is_customized
    return summary


def apply_customization(recipe: dict[str, Any], customization: dict | None) -> None:
    """Merge persisted user overrides on top of a parsed recipe, in place."""
    if not customization:
        return

    custom_defaults = customization.get("defaults")
    if isinstance(custom_defaults, dict):
        recipe["defaults"] = {**recipe.get("defaults", {}), **custom_defaults}
        recipe["params"] = dict(recipe["defaults"])

    for field in ("command", "env", "build_args", "container", "model", "mods"):
        if field in customization:
            recipe[field] = customization[field]

    # A customization can add a ``command``, which pins the recipe to one
    # engine; the support table has to follow it.
    if "command" in customization:
        recipe["engine_support"] = engine_support(recipe)


# ── Command rendering ────────────────────────────────────────────────────────


def render_command(recipe: dict[str, Any], params: dict[str, Any]) -> str:
    """Substitute placeholders in a recipe's command template.

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
