"""Recipes and mods a previous build imported from an upstream checkout.

There was an importer here, driven by an "Import from upstream" panel on the
Recipes page: it copied ``recipes/`` and ``mods/`` out of a ``spark-vllm-docker``
checkout into ``~/.config/spark-pulse/imported``. It is gone. Recipes come from
the bundled set, the OCI registry and the operator's own custom directory, and
an import that had to be re-run by hand whenever upstream moved was a fourth
source that nobody kept current.

What is left is the *reading* half, and deliberately so: an operator who ran
that import still has the files, and they are still served. Deleting this would
have removed recipes from under someone who never asked for that.

    imported/
      recipes/<same relative layout as upstream>
      mods/<mod name>/...
      manifest.json      provenance written by the importer that no longer runs

:mod:`spark_pulse.tools.recipe_sources` reads this as one source among four.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

__all__ = [
    "IMPORTED_DIR",
    "IMPORT_SOURCE_PREFIX",
    "get_import_status",
    "imported_recipes_dir",
    "imported_mods_dir",
    "iter_imported_recipe_files",
    "clear_imported",
]

IMPORTED_DIR = Path.home() / ".config" / "spark-pulse" / "imported"

#: Recipe ids from this source are prefixed so they never collide with
#: bundled, custom- or oci- recipes.
IMPORT_SOURCE_PREFIX = "imported"

_MANIFEST_NAME = "manifest.json"
_RECIPE_SUFFIXES = (".yaml", ".yml")


# ── Paths ────────────────────────────────────────────────────────────────────


def _dest_root(dest: str | Path | None = None) -> Path:
    return Path(dest) if dest is not None else IMPORTED_DIR


def imported_recipes_dir(dest: str | Path | None = None) -> Path:
    """Directory holding imported recipe YAML files."""
    return _dest_root(dest) / "recipes"


def imported_mods_dir(dest: str | Path | None = None) -> Path:
    """Directory holding imported mod directories."""
    return _dest_root(dest) / "mods"


def iter_imported_recipe_files(dest: str | Path | None = None) -> list[Path]:
    """Return every imported recipe file, sorted."""
    root = imported_recipes_dir(dest)
    if not root.is_dir():
        return []
    files: list[Path] = []
    for suffix in _RECIPE_SUFFIXES:
        files.extend(root.rglob(f"*{suffix}"))
    return sorted(set(files))


# ── Provenance ───────────────────────────────────────────────────────────────


def get_import_status(dest: str | Path | None = None) -> dict[str, Any]:
    """Return the last import manifest, or ``{"imported": False}``."""
    manifest_path = _dest_root(dest) / _MANIFEST_NAME
    if not manifest_path.is_file():
        return {"imported": False}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"imported": False}
    if not isinstance(data, dict):
        return {"imported": False}
    return {"imported": True, **data}


def clear_imported(dest: str | Path | None = None) -> bool:
    """Delete everything previously imported. Returns True if anything went."""
    root = _dest_root(dest)
    if not root.is_dir():
        return False
    shutil.rmtree(root)
    return True
