"""Mock recipe importer — canned results, in-memory manifest.

Mirrors the public surface of :mod:`spark_pulse.tools.recipe_import` so the
router and the UI behave identically in simulation mode. Pure helpers
(``iter_imported_recipe_files`` and the directory accessors) delegate to the
real module, so a test that points at a temp dir still works.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spark_pulse.tools.recipe_import import (
    IMPORT_SOURCE_PREFIX as IMPORT_SOURCE_PREFIX,
    IMPORTED_DIR as IMPORTED_DIR,
    imported_mods_dir as imported_mods_dir,
    imported_recipes_dir as imported_recipes_dir,
    iter_imported_recipe_files as iter_imported_recipe_files,
)

__all__ = [
    "IMPORTED_DIR",
    "IMPORT_SOURCE_PREFIX",
    "import_from_path",
    "import_from_git",
    "get_import_status",
    "imported_recipes_dir",
    "imported_mods_dir",
    "iter_imported_recipe_files",
    "clear_imported",
]

_MANIFEST: dict[str, Any] | None = None

_FAKE_RECIPES = [
    {
        "file": "qwen3.5-122b-fp8.yaml",
        "id": f"{IMPORT_SOURCE_PREFIX}/qwen3.5-122b-fp8",
        "status": "ok",
        "name": "Qwen3.5-122B-FP8",
        "recipe_version": "1",
        "message": "",
    },
    {
        "file": "cluster/minimax-m2-awq.yaml",
        "id": f"{IMPORT_SOURCE_PREFIX}/cluster/minimax-m2-awq",
        "status": "ok",
        "name": "MiniMax-M2-AWQ",
        "recipe_version": "1",
        "message": "",
    },
    {
        "file": "broken.yaml",
        "id": f"{IMPORT_SOURCE_PREFIX}/broken",
        "status": "error",
        "message": "command: field required",
    },
]

_FAKE_MODS = [
    {"name": "nemotron-nano", "status": "ok", "message": ""},
    {"name": "notes", "status": "skipped", "message": "no run.sh"},
]


def _counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    out = {"ok": 0, "skipped": 0, "error": 0}
    for entry in entries:
        out[entry.get("status", "error")] = out.get(entry.get("status", "error"), 0) + 1
    return out


def _build_manifest(
    source: str, source_url: str | None, ref: str | None
) -> dict[str, Any]:
    return {
        "source": source,
        "source_url": source_url,
        "ref": ref,
        "git_sha": "0123456789abcdef0123456789abcdef01234567",
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "dest": str(IMPORTED_DIR),
        "recipes": [dict(r) for r in _FAKE_RECIPES],
        "mods": [dict(m) for m in _FAKE_MODS],
        "counts": {
            "recipes": _counts(_FAKE_RECIPES),
            "mods": _counts(_FAKE_MODS),
        },
    }


def import_from_path(
    path: str | Path,
    dest: str | Path | None = None,
    *,
    source_url: str | None = None,
    ref: str | None = None,
) -> dict[str, Any]:
    """Pretend to import from a local path."""
    global _MANIFEST
    if not str(path).strip():
        raise ValueError("an import path is required")
    _MANIFEST = _build_manifest(str(path), source_url, ref)
    return _MANIFEST


def import_from_git(
    url: str,
    ref: str | None = None,
    dest: str | Path | None = None,
) -> dict[str, Any]:
    """Pretend to shallow-clone and import from a git URL."""
    global _MANIFEST
    if not url or not url.strip():
        raise ValueError("a git URL is required")
    _MANIFEST = _build_manifest(url, url, ref)
    return _MANIFEST


def get_import_status(dest: str | Path | None = None) -> dict[str, Any]:
    """Return the simulated last-import manifest."""
    if _MANIFEST is None:
        return {"imported": False}
    return {"imported": True, **_MANIFEST}


def clear_imported(dest: str | Path | None = None) -> bool:
    """Forget the simulated import."""
    global _MANIFEST
    had = _MANIFEST is not None
    _MANIFEST = None
    return had
