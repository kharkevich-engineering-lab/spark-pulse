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

from spark_pulse.tools import custom_files, custom_recipes, recipe_sources

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

    def test_an_oci_recipe_is_listed_under_a_slug(self):
        """The file stem is the id, so an install that is named for a person
        is renamed to a slug the first time anything lists the directory."""
        directory = oci_registry.RECIPES_DIR
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "Bonsai-2-27B (ternary, llama.cpp).yaml").write_text(
            "name: Bonsai-2-27B (ternary, llama.cpp)\nmodel: org/bonsai\n"
            "container: llama-cpp-node\ncommand: llama-server\n",
            encoding="utf-8",
        )

        ids = dict(recipe_sources.candidate_files())

        assert "oci-bonsai-2-27b-ternary-llama.cpp" in ids
        assert "oci-Bonsai-2-27B (ternary, llama.cpp)" not in ids
        # The rename happened on disk, once.
        assert sorted(p.name for p in directory.iterdir()) == [
            "bonsai-2-27b-ternary-llama.cpp.yaml"
        ]

    def test_the_old_id_still_resolves_after_the_rename(self):
        """This is somebody's data: a deployment record names the old id."""
        directory = oci_registry.RECIPES_DIR
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "Bonsai-2-27B (ternary, llama.cpp).yaml").write_text(
            "name: Bonsai-2-27B (ternary, llama.cpp)\nmodel: org/bonsai\n"
            "container: llama-cpp-node\ncommand: llama-server\n",
            encoding="utf-8",
        )

        payload = real_recipes.get_recipe("oci-Bonsai-2-27B (ternary, llama.cpp)")

        assert payload is not None
        # Resolved, and under the id it has now — never the one asked for,
        # because two ids for one recipe is how the pages disagree.
        assert payload["id"] == "oci-bonsai-2-27b-ternary-llama.cpp"
        assert payload["model"] == "org/bonsai"

    def test_an_old_id_that_differed_only_in_case_resolves(self):
        directory = oci_registry.RECIPES_DIR
        directory.mkdir(parents=True, exist_ok=True)
        _write_recipe(directory, "Gemma4-26B-A4B")

        payload = real_recipes.get_recipe("oci-Gemma4-26B-A4B")

        assert payload is not None
        assert payload["id"] == "oci-gemma4-26b-a4b"

    def test_an_id_no_recipe_answers_to_is_still_nothing(self):
        _write_recipe(oci_registry.RECIPES_DIR, "thing")

        assert real_recipes.get_recipe("oci-something-else") is None

    def test_a_customization_saved_under_the_old_id_is_still_applied(self):
        """The row is keyed by the id, and the id changed under it."""
        directory = oci_registry.RECIPES_DIR
        directory.mkdir(parents=True, exist_ok=True)
        _write_recipe(directory, "My Recipe")
        custom_recipes.save_customization("oci-My Recipe", {"model": "org/overridden"})

        payload = real_recipes.get_recipe("oci-my-recipe")

        assert payload is not None
        assert payload["model"] == "org/overridden"
        assert custom_recipes.get_customized_recipe("oci-my-recipe")["model"] == (
            "org/overridden"
        )
        # And the listing says so, rather than showing an uncustomized recipe.
        listed = {r["id"]: r for r in real_recipes.list_recipes()}
        assert listed["oci-my-recipe"]["is_customized"] is True

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
