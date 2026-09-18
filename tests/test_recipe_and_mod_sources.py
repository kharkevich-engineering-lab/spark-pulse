"""Every recipe and mod source is one Spark Pulse owns.

There are three, and no more: the recipes bundled in the package, the
operator's own ``~/.config/spark-pulse/custom-recipes`` and ``custom-mods``,
and ``~/.config/spark-pulse/recipes`` where OCI collections install.

There used to be a fourth — a spark-vllm-docker checkout — and the operator's
own directories reached the listing only because a symlink was planted in it
(``recipes/custom-my-recipe``, ``recipes/oci-thing``, ``mods/custom-tuning``)
so that upstream's ``run-recipe.sh`` could see them. The runner, the symlinks
and the checkout are all gone. These tests are the guarantee that the three
directories are read directly, under exactly the ids the symlinks minted.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from spark_pulse.tools import custom_files, recipe_sources

# The real modules: ``mods`` and ``recipes`` are both swapped under
# SIMULATION_MODE, and it is the real lookup that has to find these files.
mods = importlib.import_module("spark_pulse.tools.mods")
oci_registry = importlib.import_module("spark_pulse.tools.oci_registry")
native_runtime = importlib.import_module("spark_pulse.tools.native_runtime")
real_recipes = importlib.import_module("spark_pulse.tools.recipes")

RECIPE_YAML = (
    "name: {name}\nmodel: org/{name}\ncontainer: vllm-node\ncommand: vllm serve\n"
)


def _write_recipe(directory: Path, stem: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.yaml"
    path.write_text(RECIPE_YAML.format(name=stem), encoding="utf-8")
    return path


def _write_mod(directory: Path, name: str) -> Path:
    mod_dir = directory / name
    mod_dir.mkdir(parents=True, exist_ok=True)
    (mod_dir / "run.sh").write_text("#!/bin/bash\n# a custom mod\n", encoding="utf-8")
    return mod_dir


# ── Recipes ──────────────────────────────────────────────────────────────────


class TestCustomAndOciRecipes:
    def test_a_custom_recipe_is_listed(self):
        _write_recipe(custom_files.custom_recipes_dir(), "mine")

        ids = dict(recipe_sources.candidate_files())
        assert "custom-mine" in ids
        assert recipe_sources.source_of("custom-mine") == "custom"

    def test_an_oci_recipe_is_listed(self):
        _write_recipe(oci_registry.RECIPES_DIR, "thing")

        ids = dict(recipe_sources.candidate_files())
        assert "oci-thing" in ids
        assert recipe_sources.source_of("oci-thing") == "oci"

    def test_a_custom_recipe_resolves_by_id(self):
        _write_recipe(custom_files.custom_recipes_dir(), "mine")

        payload = real_recipes.get_recipe("custom-mine")
        assert payload is not None
        assert payload["id"] == "custom-mine"
        assert payload["model"] == "org/mine"
        assert payload["source"] == "custom"

    def test_an_oci_recipe_resolves_by_id(self):
        _write_recipe(oci_registry.RECIPES_DIR, "thing")

        payload = real_recipes.get_recipe("oci-thing")
        assert payload is not None
        assert payload["id"] == "oci-thing"
        assert payload["source"] == "oci"

    def test_a_non_yaml_file_in_the_custom_dir_is_ignored(self):
        directory = custom_files.custom_recipes_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "notes.txt").write_text("not a recipe", encoding="utf-8")

        ids = dict(recipe_sources.candidate_files())
        assert "notes" not in ids
        assert "custom-notes" not in ids

    def test_the_bundled_set_is_listed_on_its_own(self):
        listed = real_recipes.list_recipes()
        assert any(r["source"] == "bundled" for r in listed)


class TestNothingIsReadFromAPath(object):
    """The listing takes no directory argument, so none can be passed one."""

    def test_candidate_files_takes_no_arguments(self):
        with pytest.raises(TypeError):
            recipe_sources.candidate_files(Path("/somewhere"))  # type: ignore[call-arg]

    def test_list_recipes_takes_no_arguments(self):
        with pytest.raises(TypeError):
            real_recipes.list_recipes(Path("/somewhere"))  # type: ignore[call-arg]


# ── Mods ─────────────────────────────────────────────────────────────────────


class TestCustomMods:
    def test_a_custom_mod_is_listed(self):
        _write_mod(custom_files.custom_mods_dir(), "tuning")

        assert [m["id"] for m in mods.list_mods()] == ["custom-tuning"]

    def test_get_mod_resolves_a_custom_id(self):
        _write_mod(custom_files.custom_mods_dir(), "tuning")

        info = mods.get_mod("custom-tuning")
        assert info is not None
        assert info["id"] == "custom-tuning"
        assert info["description"] == "a custom mod"

    def test_the_deploy_path_finds_a_custom_mod_by_the_name_a_recipe_uses(self):
        """Recipes name it ``mods/custom-tuning`` — the old symlink's path."""
        expected = _write_mod(custom_files.custom_mods_dir(), "tuning")

        for name in ("mods/custom-tuning", "custom-tuning", "tuning"):
            assert native_runtime._resolve_mod_dir(name) == expected

    def test_an_unknown_mod_says_where_it_looked(self):
        with pytest.raises(native_runtime.NativeRuntimeError) as exc:
            native_runtime._resolve_mod_dir("nope")

        assert "custom-mods" in str(exc.value)
