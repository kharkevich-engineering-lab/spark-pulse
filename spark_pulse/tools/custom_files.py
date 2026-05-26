"""Custom recipe and mod management via symlinks.

Custom recipes and mods are stored in ~/.config/spark-pulse/custom-recipes/
and ~/.config/spark-pulse/custom-mods/. On startup, symlinks are created
from spark_vllm_path/recipes/custom-* and spark_vllm_path/mods/custom-*
so spark-vllm-docker can find and run them natively.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from spark_pulse.config import config

_CUSTOM_RECIPES_DIR = Path.home() / ".config" / "spark-pulse" / "custom-recipes"
_CUSTOM_MODS_DIR = Path.home() / ".config" / "spark-pulse" / "custom-mods"
_SYMLINK_RECIPES_PREFIX = "custom-"
_SYMLINK_MODS_PREFIX = "custom-"


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
    return all(part not in {"", ".", ".."} for part in p.parts)


def _ensure_dirs():
    """Create custom recipes and mods directories if they don't exist."""
    _CUSTOM_RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    _CUSTOM_MODS_DIR.mkdir(parents=True, exist_ok=True)


def _is_symlink_created_by_us(link: Path) -> bool:
    """Check if a path is a symlink pointing to our custom directory."""
    if not link.is_symlink():
        return False
    try:
        target = link.resolve()
        return (
            (target.is_dir() and _CUSTOM_RECIPES_DIR in target.parents)
            or (target.is_file() and _CUSTOM_RECIPES_DIR in target.parents)
            or (target.is_dir() and _CUSTOM_MODS_DIR in target.parents)
            or (target.is_file() and _CUSTOM_MODS_DIR in target.parents)
        )
    except OSError:
        return False


def _create_symlinks_for_dir(
    spark_dir: Path,
    custom_dir: Path,
    symlink_prefix: str,
) -> list[str]:
    """Create symlinks from spark_dir/* → custom_dir/*.

    Returns list of symlink paths created.
    """
    created = []
    if not custom_dir.is_dir():
        return created

    for item in sorted(custom_dir.iterdir()):
        name = item.name
        # Skip hidden files and directories
        if name.startswith("."):
            continue

        # For YAML files, strip extension when creating symlink name
        if item.is_file() and item.suffix in (".yaml", ".yml"):
            symlink_name = f"{symlink_prefix}{item.stem}"
        else:
            symlink_name = f"{symlink_prefix}{name}"
        link_path = spark_dir / symlink_name

        # Skip if already a symlink we created
        if link_path.is_symlink() and _is_symlink_created_by_us(link_path):
            continue

        # Skip if something else exists at this path
        if link_path.exists() or link_path.is_symlink():
            continue

        # Create the symlink
        try:
            if item.is_dir():
                link_path.symlink_to(item)
            else:
                link_path.symlink_to(item)
            created.append(symlink_name)
        except OSError:
            continue

    return created


def _remove_symlinks_for_dir(
    spark_dir: Path,
    symlink_prefix: str,
    custom_dir: Path,
) -> list[str]:
    """Remove symlinks that point into custom_dir.

    Returns list of symlink names removed.
    """
    removed: list[str] = []
    if not spark_dir.is_dir():
        return removed

    for item in spark_dir.iterdir():
        if not item.name.startswith(symlink_prefix):
            continue
        if _is_symlink_created_by_us(item):
            try:
                item.unlink()
                removed.append(item.name)
            except OSError:
                continue

    return removed


def create_symlinks(spark_vllm_path: str) -> dict[str, list[str]]:
    """Create symlinks for custom recipes and mods (full refresh).

    Returns dict with counts: {"recipes": [...], "mods": [...]}
    """
    _ensure_dirs()
    spark_dir = Path(spark_vllm_path)
    recipes_dir = spark_dir / "recipes"
    mods_dir = spark_dir / "mods"

    created = {
        "recipes": [],
        "mods": [],
    }

    # Create recipe symlinks
    if recipes_dir.is_dir():
        created["recipes"] = _create_symlinks_for_dir(
            recipes_dir, _CUSTOM_RECIPES_DIR, _SYMLINK_RECIPES_PREFIX
        )

    # Create mod symlinks
    if mods_dir.is_dir():
        created["mods"] = _create_symlinks_for_dir(
            mods_dir, _CUSTOM_MODS_DIR, _SYMLINK_MODS_PREFIX
        )

    return created


def remove_symlinks(spark_vllm_path: str) -> dict[str, list[str]]:
    """Remove symlinks for custom recipes and mods (full refresh).

    Returns dict with names removed: {"recipes": [...], "mods": [...]}
    """
    spark_dir = Path(spark_vllm_path)
    recipes_dir = spark_dir / "recipes"
    mods_dir = spark_dir / "mods"

    removed: dict[str, list[str]] = {"recipes": [], "mods": []}

    removed["recipes"] = _remove_symlinks_for_dir(
        recipes_dir, _SYMLINK_RECIPES_PREFIX, _CUSTOM_RECIPES_DIR
    )
    removed["mods"] = _remove_symlinks_for_dir(
        mods_dir, _SYMLINK_MODS_PREFIX, _CUSTOM_MODS_DIR
    )

    return removed


def create_symlink_for_recipe(recipe_name: str) -> bool:
    """Create symlink for a specific custom recipe.

    Args:
        recipe_name: The stem of the recipe file (e.g., "my-recipe")
    Returns:
        True if symlink was created.
    """
    _ensure_dirs()
    spark_path = Path(config.spark_vllm_path)
    recipes_dir = spark_path / "recipes"

    if not recipes_dir.is_dir():
        return False

    symlink_name = f"{_SYMLINK_RECIPES_PREFIX}{recipe_name}"
    link_path = recipes_dir / symlink_name

    # Skip if already exists
    if link_path.exists() or link_path.is_symlink():
        return False

    source_path = _CUSTOM_RECIPES_DIR / f"{recipe_name}.yaml"
    if not source_path.exists():
        source_path = _CUSTOM_RECIPES_DIR / f"{recipe_name}.yml"
        if not source_path.exists():
            return False

    try:
        link_path.symlink_to(source_path)
        return True
    except OSError:
        return False


def remove_symlink_for_recipe(recipe_name: str) -> bool:
    """Remove symlink for a specific custom recipe.

    Args:
        recipe_name: The stem of the recipe file (e.g., "my-recipe")
    Returns:
        True if symlink was removed.
    """
    spark_path = Path(config.spark_vllm_path)
    recipes_dir = spark_path / "recipes"

    if not recipes_dir.is_dir():
        return False

    symlink_name = f"{_SYMLINK_RECIPES_PREFIX}{recipe_name}"
    link_path = recipes_dir / symlink_name

    if link_path.is_symlink() and _is_symlink_created_by_us(link_path):
        try:
            link_path.unlink()
            return True
        except OSError:
            return False

    return False


def create_symlink_for_mod(mod_name: str) -> bool:
    """Create symlink for a specific custom mod.

    Args:
        mod_name: The name of the mod directory (e.g., "my-mod")
    Returns:
        True if symlink was created.
    """
    _ensure_dirs()
    spark_path = Path(config.spark_vllm_path)
    mods_dir = spark_path / "mods"

    if not mods_dir.is_dir():
        return False

    symlink_name = f"{_SYMLINK_MODS_PREFIX}{mod_name}"
    link_path = mods_dir / symlink_name

    # Skip if already exists
    if link_path.exists() or link_path.is_symlink():
        return False

    source_path = _CUSTOM_MODS_DIR / mod_name
    if not source_path.is_dir():
        return False

    try:
        link_path.symlink_to(source_path)
        return True
    except OSError:
        return False


def remove_symlink_for_mod(mod_name: str) -> bool:
    """Remove symlink for a specific custom mod.

    Args:
        mod_name: The name of the mod directory (e.g., "my-mod")
    Returns:
        True if symlink was removed.
    """
    spark_path = Path(config.spark_vllm_path)
    mods_dir = spark_path / "mods"

    if not mods_dir.is_dir():
        return False

    symlink_name = f"{_SYMLINK_MODS_PREFIX}{mod_name}"
    link_path = mods_dir / symlink_name

    if link_path.is_symlink() and _is_symlink_created_by_us(link_path):
        try:
            link_path.unlink()
            return True
        except OSError:
            return False

    return False


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
                import yaml

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
