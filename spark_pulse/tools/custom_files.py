"""Custom recipe and mod management.

Custom recipes and mods are stored in ``~/.config/spark-pulse/custom-recipes/``
and ``~/.config/spark-pulse/custom-mods/``, and are read from there directly:
:mod:`spark_pulse.tools.recipe_sources` lists the recipes as a first-class
source and :mod:`spark_pulse.tools.mods` lists the mods, both under a
``custom-`` id prefix.

They used to be reachable only through symlinks planted in a
spark-vllm-docker checkout (``recipes/custom-*``, ``mods/custom-*``), so that
upstream's ``run-recipe.sh`` could see them. That runner is gone, and so are
the symlinks: these files no longer need a checkout to exist.

Real-only: the router imports this module directly and there is no simulated
behaviour here — only files under the operator's config directory.
"""

from __future__ import annotations

import shutil
from pathlib import Path

#: Id prefix under which a custom recipe or mod appears in the unified
#: listings. It matches the name the old symlinks used, so recipe ids,
#: customizations and existing deployment records all keep resolving.
CUSTOM_PREFIX = "custom-"

_CUSTOM_RECIPES_DIR = Path.home() / ".config" / "spark-pulse" / "custom-recipes"
_CUSTOM_MODS_DIR = Path.home() / ".config" / "spark-pulse" / "custom-mods"


def custom_recipes_dir() -> Path:
    """Where custom recipes live. Read through this, never cached by callers."""
    return _CUSTOM_RECIPES_DIR


def custom_mods_dir() -> Path:
    """Where custom mods live. Read through this, never cached by callers."""
    return _CUSTOM_MODS_DIR


def _is_safe_path_part(part: str) -> bool:
    """Allow only plain relative path parts (no traversal, no separators)."""
    return (
        bool(part) and part not in {".", ".."} and "/" not in part and "\\" not in part
    )


def _is_safe_rel_path(rel_path: str) -> bool:
    """Validate a relative path under a mod directory."""
    p = Path(rel_path)
    if p.is_absolute():
        return False
    # ``Path`` normalises "" and "." away to no parts at all, so the check
    # below never saw them and the caller went on to write to the mod
    # directory itself — an IsADirectoryError, i.e. a 500 for what is really
    # a rejected file name.
    if not p.parts:
        return False
    return all(part not in {"", ".", ".."} for part in p.parts)


def _ensure_dirs():
    """Create custom recipes and mods directories if they don't exist."""
    _CUSTOM_RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    _CUSTOM_MODS_DIR.mkdir(parents=True, exist_ok=True)


def discover_custom_recipes() -> list[dict]:
    """Discover custom recipes in the custom-recipes directory.

    Returns list of recipe info dicts:
    [{
        "id": "custom/my-recipe",
        "name": "My Recipe",
        "filename": "my-recipe.yaml",
        "created_at": "2025-01-15T10:30:00",
    }, ...]
    """
    # A directory recipe reads YAML too, and ``import yaml`` inside the file
    # branch made the name local to this whole function: a custom-recipes
    # directory whose first entry is a subdirectory raised UnboundLocalError
    # and took every recipe listing down with it.
    import yaml

    _ensure_dirs()
    recipes = []

    if not _CUSTOM_RECIPES_DIR.is_dir():
        return recipes

    for item in sorted(_CUSTOM_RECIPES_DIR.iterdir()):
        # Skip hidden files and directories
        if item.name.startswith("."):
            continue

        # For YAML files
        if item.suffix in (".yaml", ".yml"):
            try:
                with open(item) as f:
                    data = yaml.safe_load(f) or {}
                name = data.get("name", item.stem)
                recipes.append(
                    {
                        "id": f"custom/{item.stem}",
                        "name": name,
                        "filename": item.name,
                        "filepath": str(item),
                        "created_at": item.stat().st_ctime,
                    }
                )
            except (yaml.YAMLError, OSError):
                recipes.append(
                    {
                        "id": f"custom/{item.stem}",
                        "name": item.stem,
                        "filename": item.name,
                        "filepath": str(item),
                        "created_at": item.stat().st_ctime,
                    }
                )

        # For directories (subdirectory recipes)
        elif item.is_dir():
            # Look for a YAML file in the directory
            yaml_files = list(item.glob("*.yaml")) + list(item.glob("*.yml"))
            if yaml_files:
                yaml_file = yaml_files[0]
                try:
                    with open(yaml_file) as f:
                        data = yaml.safe_load(f) or {}
                    name = data.get("name", item.stem)
                    rel_id = f"custom/{item.name}/{yaml_file.stem}"
                    recipes.append(
                        {
                            "id": rel_id,
                            "name": name,
                            "filename": yaml_file.name,
                            "filepath": str(yaml_file),
                            "created_at": item.stat().st_ctime,
                        }
                    )
                except OSError:
                    pass

    return recipes


def discover_custom_mods() -> list[dict]:
    """Discover custom mods in the custom-mods directory.

    Returns list of mod info dicts:
    [{
        "id": "custom/my-mod",
        "name": "My Mod",
        "description": "",
        "has_run_sh": True,
    }, ...]
    """
    _ensure_dirs()
    mods = []

    if not _CUSTOM_MODS_DIR.is_dir():
        return mods

    for item in sorted(_CUSTOM_MODS_DIR.iterdir()):
        if item.name.startswith("."):
            continue
        if not item.is_dir():
            continue

        has_run_sh = (item / "run.sh").exists()

        # Skip mods without run.sh — not a valid mod
        if not has_run_sh:
            continue

        description = ""

        # Try to extract description from run.sh (first comment line, skip shebang)
        run_sh = item / "run.sh"
        if has_run_sh:
            try:
                with open(run_sh) as f:
                    for line in f:
                        line = line.strip()
                        # Skip shebang
                        if line.startswith("#!"):
                            continue
                        if line.startswith("#"):
                            description = line.lstrip("# ").strip()
                            break
            except OSError:
                pass

        mods.append(
            {
                "id": f"custom/{item.name}",
                "name": item.name,
                "description": description,
                "filepath": str(item),
                "has_run_sh": has_run_sh,
            }
        )

    return mods


def get_custom_recipe_content(recipe_id: str) -> dict | None:
    """Get YAML content of a custom recipe.

    recipe_id should be like "custom/my-recipe" or "custom/subdir/recipe".

    Returns {"content": "...", "id": "..."} or None.
    """
    _ensure_dirs()
    # Remove "custom/" prefix
    parts = recipe_id.replace("custom/", "").split("/")

    if not parts or not all(_is_safe_path_part(part) for part in parts):
        return None

    # Single file recipe: custom/my-recipe → custom-recipes/my-recipe.yaml
    if len(parts) == 1:
        for ext in (".yaml", ".yml"):
            fpath = _CUSTOM_RECIPES_DIR / f"{parts[0]}{ext}"
            if fpath.exists():
                with open(fpath) as f:
                    content = f.read()
                return {"content": content, "id": recipe_id}

    # Directory recipe: custom/subdir/recipe → custom-recipes/subdir/recipe.yaml
    elif len(parts) == 2:
        dir_path = _CUSTOM_RECIPES_DIR / parts[0]
        if dir_path.is_dir():
            for ext in (".yaml", ".yml"):
                fpath = dir_path / f"{parts[1]}{ext}"
                if fpath.exists():
                    with open(fpath) as f:
                        content = f.read()
                    return {"content": content, "id": recipe_id}

    return None


def save_custom_recipe(recipe_id: str, yaml_content: str) -> bool:
    """Save a custom recipe's YAML content.

    recipe_id: "custom/my-recipe"
    yaml_content: the full YAML string

    Returns True on success.
    """
    _ensure_dirs()
    parts = recipe_id.replace("custom/", "").split("/")

    if not parts or not all(_is_safe_path_part(part) for part in parts):
        raise ValueError("Invalid recipe id")

    if len(parts) == 1:
        fpath = _CUSTOM_RECIPES_DIR / f"{parts[0]}.yaml"
    elif len(parts) == 2:
        fpath = _CUSTOM_RECIPES_DIR / parts[0] / f"{parts[1]}.yaml"
    else:
        raise ValueError("Invalid recipe id")

    # Validate YAML
    import yaml

    try:
        yaml.safe_load(yaml_content)
    except yaml.YAMLError:
        raise ValueError("Invalid YAML content")

    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(yaml_content, encoding="utf-8")
    return True


def delete_custom_recipe(recipe_id: str) -> bool:
    """Delete a custom recipe.

    Returns True if deleted.
    """
    _ensure_dirs()
    parts = recipe_id.replace("custom/", "").split("/")

    if not parts or not all(_is_safe_path_part(part) for part in parts):
        return False

    if len(parts) == 1:
        for ext in (".yaml", ".yml"):
            fpath = _CUSTOM_RECIPES_DIR / f"{parts[0]}{ext}"
            if fpath.exists():
                fpath.unlink()
                return True

    elif len(parts) == 2:
        dir_path = _CUSTOM_RECIPES_DIR / parts[0]
        if dir_path.is_dir():
            for ext in (".yaml", ".yml"):
                fpath = dir_path / f"{parts[1]}{ext}"
                if fpath.exists():
                    fpath.unlink()
                    return True

    return False


def upload_custom_recipe(file_content: bytes, filename: str) -> dict:
    """Upload a custom recipe file.

    Returns {"id": "...", "name": "..."} on success.
    """
    _ensure_dirs()

    stem = Path(filename).stem

    # Check if directory already exists
    dir_path = _CUSTOM_RECIPES_DIR / stem
    if dir_path.exists() and dir_path.is_dir():
        raise ValueError(f"Recipe directory '{stem}' already exists")

    # Save as YAML file
    fpath = _CUSTOM_RECIPES_DIR / f"{stem}.yaml"

    # Validate YAML
    import yaml

    try:
        data = yaml.safe_load(file_content.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("YAML must be a mapping")
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML: {e}")

    fpath.write_bytes(file_content)

    name = data.get("name", stem)
    return {"id": f"custom/{stem}", "name": name}


def get_custom_mod_files(mod_id: str) -> dict | None:
    """Get all files from a custom mod.

    Returns {"files": {"run.sh": "...", "template.jinja": "..."}} or None.
    """
    _ensure_dirs()
    parts = mod_id.replace("custom/", "").split("/")
    if len(parts) != 1 or not _is_safe_path_part(parts[0]):
        return None

    mod_path = _CUSTOM_MODS_DIR / parts[0]
    if not mod_path.is_dir():
        return None

    files = {}
    for item in mod_path.rglob("*"):
        if item.is_file() and not item.name.startswith("."):
            rel = item.relative_to(mod_path)
            try:
                files[str(rel)] = item.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass

    return {"files": files, "id": mod_id}


def save_custom_mod(mod_id: str, files: dict[str, str]) -> bool:
    """Save files for a custom mod.

    files: {"run.sh": "...", "template.jinja": "..."}

    Returns True on success.
    """
    _ensure_dirs()
    parts = mod_id.replace("custom/", "").split("/")
    if len(parts) != 1 or not _is_safe_path_part(parts[0]):
        raise ValueError("Invalid mod id")

    mod_path = _CUSTOM_MODS_DIR / parts[0]
    mod_path.mkdir(parents=True, exist_ok=True)

    for filepath, content in files.items():
        if not _is_safe_rel_path(filepath):
            raise ValueError(f"Invalid mod file path: {filepath}")
        full_path = mod_path / filepath
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")

    return True


def delete_custom_mod(mod_id: str) -> bool:
    """Delete a custom mod directory.

    Returns True if deleted.
    """
    _ensure_dirs()
    parts = mod_id.replace("custom/", "").split("/")
    if len(parts) != 1 or not _is_safe_path_part(parts[0]):
        return False

    mod_path = _CUSTOM_MODS_DIR / parts[0]
    if mod_path.is_dir():
        shutil.rmtree(mod_path)
        return True

    return False
