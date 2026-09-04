"""Import recipes and mods from an upstream checkout into the config dir.

This replaces symlinking our files *into* the ``spark-vllm-docker`` checkout
with copying its files *out* of it. The upstream repo becomes an import source
rather than a runtime dependency.

Imported files land in ``~/.config/spark-pulse/imported``:

    imported/
      recipes/<same relative layout as upstream>
      mods/<mod name>/...
      manifest.json      provenance: source, git sha, time, per-file results

Recipes are validated on the way in and copied verbatim — a v1 recipe stays a
v1 recipe. Conversion, if it ever happens, is a separate explicit step.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spark_pulse.tools.recipe_schema import validate_recipe_file

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ── Provenance ───────────────────────────────────────────────────────────────


def _git_sha(path: Path) -> str | None:
    """Return the HEAD sha if ``path`` is inside a git work tree."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def _git_remote(path: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


# ── Import ───────────────────────────────────────────────────────────────────


def _import_recipes(src_root: Path, dest_root: Path) -> list[dict[str, Any]]:
    """Copy and validate ``recipes/**/*.yaml`` from ``src_root``."""
    src = src_root / "recipes"
    results: list[dict[str, Any]] = []
    if not src.is_dir():
        return results

    files: list[Path] = []
    for suffix in _RECIPE_SUFFIXES:
        files.extend(src.rglob(f"*{suffix}"))

    dest = dest_root / "recipes"
    for path in sorted(set(files)):
        if path.is_symlink() and not _is_within(src, path):
            results.append(
                {
                    "file": str(path.relative_to(src)),
                    "id": None,
                    "status": "skipped",
                    "message": "symlink pointing outside the source tree",
                }
            )
            continue
        if not path.is_file():
            continue

        rel = path.relative_to(src)
        recipe_id = f"{IMPORT_SOURCE_PREFIX}/{rel.with_suffix('').as_posix()}"
        check = validate_recipe_file(path)
        if not check["ok"]:
            results.append(
                {
                    "file": rel.as_posix(),
                    "id": recipe_id,
                    "status": "error",
                    "message": "; ".join(
                        f"{e['path']}: {e['message']}" if e["path"] else e["message"]
                        for e in check["errors"]
                    ),
                }
            )
            continue

        target = dest / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        except OSError as exc:
            results.append(
                {
                    "file": rel.as_posix(),
                    "id": recipe_id,
                    "status": "error",
                    "message": f"copy failed: {exc}",
                }
            )
            continue

        results.append(
            {
                "file": rel.as_posix(),
                "id": recipe_id,
                "status": "ok",
                "name": check["name"],
                "recipe_version": check["recipe_version"],
                "message": "",
            }
        )
    return results


def _import_mods(src_root: Path, dest_root: Path) -> list[dict[str, Any]]:
    """Copy ``mods/<name>/`` directories that carry a ``run.sh``."""
    src = src_root / "mods"
    results: list[dict[str, Any]] = []
    if not src.is_dir():
        return results

    dest = dest_root / "mods"
    for item in sorted(src.iterdir()):
        if item.name.startswith("."):
            continue
        if not item.is_dir():
            results.append(
                {
                    "name": item.name,
                    "status": "skipped",
                    "message": "not a directory",
                }
            )
            continue
        if not (item / "run.sh").is_file():
            results.append(
                {
                    "name": item.name,
                    "status": "skipped",
                    "message": "no run.sh",
                }
            )
            continue

        target = dest / item.name
        try:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target, symlinks=True)
        except OSError as exc:
            results.append(
                {
                    "name": item.name,
                    "status": "error",
                    "message": f"copy failed: {exc}",
                }
            )
            continue
        results.append({"name": item.name, "status": "ok", "message": ""})
    return results


def _counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    out = {"ok": 0, "skipped": 0, "error": 0}
    for entry in entries:
        status = entry.get("status", "error")
        out[status] = out.get(status, 0) + 1
    return out


def import_from_path(
    path: str | Path,
    dest: str | Path | None = None,
    *,
    source_url: str | None = None,
    ref: str | None = None,
) -> dict[str, Any]:
    """Import ``recipes/**`` and ``mods/*`` from an upstream-layout directory.

    Every recipe is validated before it is copied; invalid ones are reported
    and skipped. Nothing is converted — a v1 recipe stays v1.

    Returns a report with per-file results and the manifest that was written.
    """
    src_root = Path(path).expanduser()
    if not src_root.is_dir():
        raise FileNotFoundError(f"import source is not a directory: {src_root}")

    has_recipes = (src_root / "recipes").is_dir()
    has_mods = (src_root / "mods").is_dir()
    if not has_recipes and not has_mods:
        raise ValueError(
            f"{src_root} has neither a 'recipes' nor a 'mods' directory; "
            "point at a spark-vllm-docker checkout or a directory with that layout"
        )

    dest_root = _dest_root(dest)
    dest_root.mkdir(parents=True, exist_ok=True)

    recipes = _import_recipes(src_root, dest_root)
    mods = _import_mods(src_root, dest_root)

    manifest: dict[str, Any] = {
        "source": str(src_root),
        "source_url": source_url or _git_remote(src_root),
        "ref": ref,
        "git_sha": _git_sha(src_root),
        "imported_at": _now(),
        "dest": str(dest_root),
        "recipes": recipes,
        "mods": mods,
        "counts": {
            "recipes": _counts(recipes),
            "mods": _counts(mods),
        },
    }
    _write_manifest(dest_root, manifest)
    return manifest


def import_from_git(
    url: str,
    ref: str | None = None,
    dest: str | Path | None = None,
) -> dict[str, Any]:
    """Shallow-clone ``url`` (optionally at ``ref``) and import from it."""
    if not url or not url.strip():
        raise ValueError("a git URL is required")

    with tempfile.TemporaryDirectory(prefix="spark-pulse-import-") as tmp:
        clone_dir = Path(tmp) / "checkout"
        cmd = ["git", "clone", "--depth", "1"]
        if ref:
            cmd += ["--branch", ref]
        cmd += [url, str(clone_dir)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"git clone failed: {exc}") from exc
        if proc.returncode != 0:
            raise RuntimeError(
                f"git clone failed: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        return import_from_path(clone_dir, dest, source_url=url, ref=ref)


# ── Manifest ─────────────────────────────────────────────────────────────────


def _write_manifest(dest_root: Path, manifest: dict[str, Any]) -> None:
    dest_root.mkdir(parents=True, exist_ok=True)
    tmp = dest_root / f"{_MANIFEST_NAME}.tmp"
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp.replace(dest_root / _MANIFEST_NAME)


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
