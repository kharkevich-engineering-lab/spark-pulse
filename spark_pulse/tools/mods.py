"""Tools for reading spark-vllm-docker mods."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from spark_pulse.config import config

_ASSET_EXTENSIONS = {".patch", ".diff", ".jinja", ".py", ".json", ".yaml", ".yml", ".sh"}


def _mods_dir() -> Path:
    return Path(config.spark_vllm_path) / "mods"


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
    import re
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
        ({"name": f.name, "kind": _asset_kind(f.name)} for f in mod_dir.iterdir()
         if f.is_file() and f.name != "run.sh"),
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
    d = _mods_dir()
    if not d.exists():
        return []
    return [_mod_info(p) for p in sorted(d.iterdir()) if p.is_dir()]


def get_mod(mod_id: str) -> dict[str, Any] | None:
    # Sanitise: no path traversal
    if "/" in mod_id or ".." in mod_id:
        return None
    mod_dir = _mods_dir() / mod_id
    if not mod_dir.is_dir():
        return None
    return _mod_info(mod_dir, include_script=True)
