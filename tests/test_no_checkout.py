"""Custom and OCI content works with no spark-vllm-docker checkout at all.

Before the native runtime, a custom or OCI-installed recipe reached the
deployable listing only because a symlink was planted in the checkout —
``recipes/custom-my-recipe``, ``recipes/oci-thing``, ``mods/custom-tuning`` —
so that upstream's ``run-recipe.sh`` could see it. The runner and the symlinks
are gone. These tests are the replacement guarantee: those directories are read
directly, under the same ids, and the checkout is genuinely optional.
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

NO_CHECKOUT = Path("/nonexistent-spark-vllm-checkout")

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
    def test_a_custom_recipe_is_listed_without_a_checkout(self):
        _write_recipe(custom_files.custom_recipes_dir(), "mine")

        ids = dict(recipe_sources.candidate_files(NO_CHECKOUT))
        assert "custom-mine" in ids
        assert recipe_sources.source_of("custom-mine") == "custom"

    def test_an_oci_recipe_is_listed_without_a_checkout(self):
        _write_recipe(oci_registry.RECIPES_DIR, "thing")

        ids = dict(recipe_sources.candidate_files(NO_CHECKOUT))
        assert "oci-thing" in ids
        assert recipe_sources.source_of("oci-thing") == "oci"

    def test_a_custom_recipe_resolves_by_id(self):
        _write_recipe(custom_files.custom_recipes_dir(), "mine")

        payload = real_recipes.get_recipe("custom-mine", spark_path=NO_CHECKOUT)
        assert payload is not None
        assert payload["id"] == "custom-mine"
        assert payload["model"] == "org/mine"
        assert payload["source"] == "custom"

    def test_an_oci_recipe_resolves_by_id(self):
        _write_recipe(oci_registry.RECIPES_DIR, "thing")

        payload = real_recipes.get_recipe("oci-thing", spark_path=NO_CHECKOUT)
        assert payload is not None
        assert payload["id"] == "oci-thing"
        assert payload["source"] == "oci"

    def test_a_leftover_symlink_in_a_checkout_does_not_duplicate_the_recipe(
        self, tmp_path
    ):
        """An install upgraded from an older version still has the symlinks."""
        source = _write_recipe(custom_files.custom_recipes_dir(), "mine")
        checkout_recipes = tmp_path / "recipes"
        checkout_recipes.mkdir(parents=True)
        (checkout_recipes / "custom-mine").symlink_to(source)

        ids = [rid for rid, _ in recipe_sources.candidate_files(tmp_path)]
        assert ids.count("custom-mine") == 1

    def test_a_non_yaml_file_in_the_custom_dir_is_ignored(self):
        directory = custom_files.custom_recipes_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "notes.txt").write_text("not a recipe", encoding="utf-8")

        ids = dict(recipe_sources.candidate_files(NO_CHECKOUT))
        assert "notes" not in ids
        assert "custom-notes" not in ids


class TestTheCheckoutIsOptional:
    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_an_unset_path_never_resolves_to_the_working_directory(self, value):
        assert recipe_sources.checkout_recipes_dir(value) is None

    def test_a_path_that_points_nowhere_is_no_directory(self):
        assert recipe_sources.checkout_recipes_dir(NO_CHECKOUT) is None

    def test_listing_still_works_with_no_checkout(self):
        listed = real_recipes.list_recipes(spark_path=None)
        assert any(r["source"] == "bundled" for r in listed)

    def test_config_reports_no_directory_for_a_missing_checkout(self, monkeypatch):
        import spark_pulse.config as cfg

        monkeypatch.setitem(cfg.config._data, "spark_vllm_path", str(NO_CHECKOUT))
        assert cfg.config.spark_vllm_dir is None
        monkeypatch.setitem(cfg.config._data, "spark_vllm_path", "")
        assert cfg.config.spark_vllm_dir is None


# ── Mods ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def no_checkout(monkeypatch):
    """Configure a spark_vllm_path that does not exist."""
    import spark_pulse.config as cfg

    monkeypatch.setitem(cfg.config._data, "spark_vllm_path", str(NO_CHECKOUT))


class TestCustomMods:
    def test_a_custom_mod_is_listed_without_a_checkout(self, no_checkout):
        _write_mod(custom_files.custom_mods_dir(), "tuning")

        assert [m["id"] for m in mods.list_mods()] == ["custom-tuning"]

    def test_get_mod_resolves_a_custom_id(self, no_checkout):
        _write_mod(custom_files.custom_mods_dir(), "tuning")

        info = mods.get_mod("custom-tuning")
        assert info is not None
        assert info["id"] == "custom-tuning"
        assert info["description"] == "a custom mod"

    def test_the_deploy_path_finds_a_custom_mod_by_the_name_a_recipe_uses(
        self, no_checkout
    ):
        """Recipes name it ``mods/custom-tuning`` — the old symlink's path."""
        expected = _write_mod(custom_files.custom_mods_dir(), "tuning")

        for name in ("mods/custom-tuning", "custom-tuning", "tuning"):
            assert native_runtime._resolve_mod_dir(name) == expected

    def test_a_checkout_mod_still_wins_its_own_name(self, monkeypatch, tmp_path):
        import spark_pulse.config as cfg

        monkeypatch.setitem(cfg.config._data, "spark_vllm_path", str(tmp_path))
        checkout_mod = _write_mod(tmp_path / "mods", "fix-x")
        _write_mod(custom_files.custom_mods_dir(), "tuning")

        assert native_runtime._resolve_mod_dir("mods/fix-x") == checkout_mod
        assert sorted(m["id"] for m in mods.list_mods()) == [
            "custom-tuning",
            "fix-x",
        ]

    def test_an_unknown_mod_says_where_it_looked(self, no_checkout):
        with pytest.raises(native_runtime.NativeRuntimeError) as exc:
            native_runtime._resolve_mod_dir("nope")

        assert "custom-mods" in str(exc.value)
