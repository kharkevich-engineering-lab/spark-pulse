"""Reading mods, and checking what is in one.

Listing, inspection and security validation. Applying a mod is not here: a
recipe names its mods and :func:`native_runtime._apply_mods` copies them into
each rank's container at deploy time, through that rank's node service.

There was a second implementation — a ``ModOrchestrator`` that walked a
``cluster_state`` of head and workers and applied a mod to each. That state
came from the removed cluster orchestrator, nothing had produced one since it
was deleted, and the two REST endpoints that took it had no caller. It is
gone; the deploy path is the one that runs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from spark_pulse.config import config
from spark_pulse.tools.launch_script import (
    ValidationResult,
    validate_mod_content as validate_mod_content_raw,
)

_ASSET_EXTENSIONS = {
    ".patch",
    ".diff",
    ".jinja",
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".sh",
}


def validate_mod_content(mod_path: Path) -> ValidationResult:
    """Security validation for mod content.

    Delegates to the shared implementation in launch_script.py.
    """
    return validate_mod_content_raw(mod_path)


def _mods_dir() -> Path | None:
    """``<checkout>/mods``, or ``None`` when there is no checkout."""
    root = config.spark_vllm_dir
    return None if root is None else root / "mods"


def _extract_description(run_sh: Path) -> str:
    """Extract description from leading comments or first echo statement in run.sh."""
    try:
        lines = run_sh.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    desc: list[str] = []
    for line in lines:
        if line.startswith("#!/"):
            continue
        if line.startswith("#"):
            text = line.lstrip("#").strip()
            if text:
                desc.append(text)
        elif line.strip() == "":
            if desc:
                break  # blank line ends the leading comment block
        else:
            break  # first real code line ends it

    if desc:
        return " ".join(desc)

    # Fallback: look for the first `echo "..."` line anywhere in the script
    for line in lines:
        m = re.match(r'\s*echo\s+["\'](.+?)["\']', line)
        if m:
            return m.group(1).strip("=:- ").rstrip(".")
    return ""


def _asset_kind(name: str) -> str:
    ext = Path(name).suffix
    if ext in (".patch", ".diff"):
        return "patch"
    if ext == ".jinja":
        return "template"
    if ext == ".py":
        return "python"
    if ext in (".yaml", ".yml"):
        return "yaml"
    if ext == ".sh":
        return "script"
    return "file"


def _mod_info(mod_dir: Path, include_script: bool = False) -> dict[str, Any]:
    run_sh = mod_dir / "run.sh"
    files = sorted(
        (
            {"name": f.name, "kind": _asset_kind(f.name)}
            for f in mod_dir.iterdir()
            if f.is_file() and f.name != "run.sh"
        ),
        key=lambda x: x["name"],
    )
    info: dict[str, Any] = {
        "id": mod_dir.name,
        "description": _extract_description(run_sh) if run_sh.exists() else "",
        "files": files,
        "has_patches": any(f["kind"] == "patch" for f in files),
    }
    if include_script and run_sh.exists():
        info["script"] = run_sh.read_text(errors="replace")
    return info


def list_mods() -> list[dict[str, Any]]:
    """Mods from the checkout, plus the operator's own under ``custom-`` ids.

    Custom mods used to appear here only because a ``mods/custom-x`` symlink
    was planted in the checkout. They are read from their own directory now, so
    they are listed with or without one — under the same ids, which is what the
    recipes that name them expect.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for directory, prefix in _mod_dirs():
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            info = _mod_info(path)
            info["id"] = f"{prefix}{path.name}" if prefix else path.name
            if info["id"] in seen:
                continue
            seen.add(info["id"])
            out.append(info)
    return out


def _mod_dirs() -> list[tuple[Path, str]]:
    """``(directory, id prefix)`` for every place a mod can live."""
    from spark_pulse.tools import custom_files

    dirs: list[tuple[Path, str]] = []
    checkout = _mods_dir()
    if checkout is not None:
        dirs.append((checkout, ""))
    dirs.append((custom_files.custom_mods_dir(), custom_files.CUSTOM_PREFIX))
    return dirs


def get_mod(mod_id: str) -> dict[str, Any] | None:
    # Sanitise: no path traversal
    if "/" in mod_id or ".." in mod_id:
        return None
    for directory, prefix in _mod_dirs():
        if prefix and not mod_id.startswith(prefix):
            continue
        mod_dir = directory / mod_id.removeprefix(prefix)
        if not mod_dir.is_dir():
            continue
        info = _mod_info(mod_dir, include_script=True)
        info["id"] = mod_id
        return info
    return None
