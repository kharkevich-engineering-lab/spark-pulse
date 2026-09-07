"""User recipe customizations storage.

Stores partial overrides for recipes as one row per recipe id, separately from
the recipe YAML, so original files are never modified — git updates to
spark-vllm-docker always work. ``~/.config/spark-pulse/custom-recipes.json`` is
where they used to live and is now only the one-time import source.

Data structure (one row's ``overrides``, keyed by recipe id):
  {
    "<recipe_id>": {
      "command": "...",         // override command template
      "defaults": {...},        // override/merge with recipe defaults
      "env": {...},             // override/add env vars
      "build_args": [...],      // override/add build args
      "container": "...",       // override container name
      "model": "...",           // override model identifier
    }
  }

When loading a customized recipe, custom fields are merged on top of
the original YAML fields (low → high priority).
"""

from __future__ import annotations

import json
import threading
from contextlib import AbstractContextManager
from pathlib import Path

from sqlalchemy import JSON, String, select
from sqlalchemy.orm import Mapped, mapped_column

from sqlalchemy.exc import IntegrityError

from spark_pulse.db import Base, is_done, mark_done_within, session_scope

_CUSTOM_PATH = Path.home() / ".config" / "spark-pulse" / "custom-recipes.json"

# Fields that can be customized per recipe
CUSTOMIZABLE_FIELDS = {
    "command",
    "defaults",
    "env",
    "build_args",
    "container",
    "model",
    "mods",
}


class _CustomizationRow(Base):
    """One recipe's partial override."""

    __tablename__ = "recipe_customizations"

    recipe_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)


_IMPORT_KEY = "custom_recipes.imported_from_json"


def _migrate_from_json() -> None:
    if is_done(_IMPORT_KEY):
        return
    if not _CUSTOM_PATH.exists():
        return
    try:
        with open(_CUSTOM_PATH) as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    # Marker and rows in one transaction, and ``get``-then-write rather than
    # ``merge`` (see the house rule in :mod:`spark_pulse.db`). Marking in its
    # own transaction leaves a window in which the marker says the file was
    # taken while none of it was — and the marker is what stops it being
    # retried, so the file would be discarded for good.
    try:
        with session_scope() as db:
            if not mark_done_within(db, _IMPORT_KEY):
                return
            for recipe_id, overrides in data.items():
                if not isinstance(overrides, dict):
                    continue
                key = str(recipe_id)
                row = db.get(_CustomizationRow, key)
                if row is None:
                    db.add(_CustomizationRow(recipe_id=key, overrides=overrides))
                else:
                    row.overrides = overrides
    except IntegrityError:
        return


def _apply(row: _CustomizationRow, overrides: dict) -> bool:
    """Copy ``overrides`` onto an existing row; ``False`` if nothing differed.

    Assigning an equal-but-distinct dict to a JSON column still marks the
    attribute dirty, so without this comparison a whole-set save that changed
    one recipe emits an UPDATE for every other one — a write that grows with
    the number of customized recipes rather than with the size of the change.
    """
    if row.overrides == overrides:
        return False
    row.overrides = overrides
    return True


#: Held across every read-modify-write of the customizations.
#:
#: :func:`save_customization` merges what the caller sent into what is stored,
#: so two of them are the classic lost update: both read the same overrides,
#: both write their own merge, and whichever field the first one set is gone.
#: It also serialises those against :func:`save_customizations`, whose whole-set
#: write deletes every recipe absent from the set it was handed — including one
#: customized since that set was assembled. Reentrant because the writers below
#: call one another.
_MUTATION_LOCK = threading.RLock()


def transaction() -> AbstractContextManager[None]:
    """Serialise a read-modify-write of the customizations.

    Every caller that reads customizations, changes them and writes them back
    must hold this for the whole sequence — not merely around the write, which
    is where the transaction already is.
    """
    return _MUTATION_LOCK


# ── The whole set ────────────────────────────────────────────────────────────


def load_customizations() -> dict[str, dict]:
    """Every recipe customization.

    Ordered by recipe id so the mapping is built the same way on both
    backends: without an ``ORDER BY`` the row order is whatever the storage
    engine last did to the heap.
    """
    _migrate_from_json()
    with session_scope() as db:
        rows = db.execute(
            select(_CustomizationRow).order_by(_CustomizationRow.recipe_id)
        )
        return {row.recipe_id: dict(row.overrides) for row in rows.scalars()}


def save_customizations(customizations: dict[str, dict]) -> None:
    """Replace the whole set, in one transaction.

    Kept because "the set is now exactly this" — recipes absent from
    ``customizations`` are removed — is a guarantee the per-recipe writers
    below deliberately do not make. What it no longer does is *write* the whole
    set: only rows that actually differ are updated.
    """
    with transaction():
        # Before the write, not after: an import that ran later would merge the
        # old JSON file back in and resurrect every customization this save had
        # just removed.
        _migrate_from_json()
        with session_scope() as db:
            stored = {
                row.recipe_id: row
                for row in db.execute(select(_CustomizationRow)).scalars()
            }
            for recipe_id, row in stored.items():
                if recipe_id not in customizations:
                    db.delete(row)
            for recipe_id, overrides in customizations.items():
                row = stored.get(str(recipe_id))
                if row is None:
                    db.add(
                        _CustomizationRow(recipe_id=str(recipe_id), overrides=overrides)
                    )
                else:
                    _apply(row, overrides)


# ── One recipe at a time ─────────────────────────────────────────────────────
#
# A customization is per recipe in every direction: the UI edits one recipe,
# the router addresses one recipe, and a recipe listing asks about one recipe
# at a time. Loading and rewriting the whole set to serve any of that was the
# storage migration's first step, not its shape.


def get_customization(recipe_id: str) -> dict | None:
    """Get partial customization for a specific recipe.

    Returns the partial override dict, or None if no customization exists.

    A keyed read rather than a load of the whole set: this is called once per
    recipe while a listing is assembled, so reading every customization here
    made a listing cost the whole table once per recipe on it.
    """
    _migrate_from_json()
    with session_scope() as db:
        row = db.get(_CustomizationRow, recipe_id)
        return dict(row.overrides) if row is not None else None


def save_customization(recipe_id: str, customization: dict) -> dict:
    """Save (merge) customizations for a recipe.

    Only keys in CUSTOMIZABLE_FIELDS are stored. Returns the complete
    customization dict for this recipe (all fields), or an empty one when the
    merge left nothing and the row was dropped.

    The merge is against what is *stored now* — inside the mutex, in one
    transaction, on the single row being changed — rather than against a copy
    of the whole set read earlier. A caller therefore cannot use this to write
    a stale field back over somebody else's change, only the fields it named,
    and cannot delete a recipe it never mentioned.

    The row is selected ``FOR UPDATE``, which SQLAlchemy renders for PostgreSQL
    and omits for SQLite. The mutex serialises one process; the reason
    PostgreSQL is a supported backend at all is the day there is more than one,
    and a lock held in Python says nothing to the other process.
    """
    filtered = {
        k: v
        for k, v in customization.items()
        if k in CUSTOMIZABLE_FIELDS and v is not None
    }
    with transaction():
        _migrate_from_json()
        with session_scope() as db:
            row = db.get(_CustomizationRow, recipe_id, with_for_update=True)
            merged = {**(dict(row.overrides) if row is not None else {}), **filtered}
            if not merged:
                if row is not None:
                    db.delete(row)
            elif row is None:
                db.add(_CustomizationRow(recipe_id=str(recipe_id), overrides=merged))
            else:
                _apply(row, merged)
            return merged


def delete_customization(recipe_id: str) -> bool:
    """Remove customization for a specific recipe.

    Returns True if a customization existed and was deleted.

    By name, not by saving the set minus it: the whole-set path would also
    delete any recipe customized between the load and the save.
    """
    with transaction():
        _migrate_from_json()
        with session_scope() as db:
            row = db.get(_CustomizationRow, recipe_id)
            if row is None:
                return False
            db.delete(row)
            return True


def has_customization(recipe_id: str) -> bool:
    """Quick check whether a recipe has any customizations."""
    return get_customization(recipe_id) is not None


def get_customized_recipe(
    recipe_id: str, spark_path: Path | None = None
) -> dict | None:
    """Load a recipe and merge any user customizations on top.

    This is the main entry point — same return shape as get_recipe()
    but with custom fields merged into the base recipe data.

    Merge strategy (low → high):
      1. Original YAML fields from the recipe
      2. Custom defaults merged with original defaults (user wins)
      3. Other custom fields override the originals directly
    """
    from spark_pulse.tools.recipes import get_recipe as _get_recipe

    recipe = _get_recipe(recipe_id, spark_path)
    if recipe is None:
        return None

    customization = get_customization(recipe_id)
    if not customization:
        return recipe

    # Merge defaults (user overrides original defaults)
    custom_defaults = customization.get("defaults")
    if custom_defaults:
        recipe["defaults"] = {**recipe.get("defaults", {}), **custom_defaults}

    # Direct field overrides
    for field in ("command", "env", "build_args", "container", "model"):
        if field in customization:
            recipe[field] = customization[field]

    return recipe
