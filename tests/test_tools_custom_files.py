"""Unit tests for the custom_files tool module."""

from __future__ import annotations


import pytest

from spark_pulse.tools import custom_files


def _make_temp_paths(monkeypatch, tmp_path):
    """Set custom files paths to use temp directories and clean them."""
    recipes_dir = tmp_path / "custom-recipes"
    mods_dir = tmp_path / "custom-mods"
    recipes_dir.mkdir(exist_ok=True)
    mods_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(custom_files, "_CUSTOM_RECIPES_DIR", recipes_dir)
    monkeypatch.setattr(custom_files, "_CUSTOM_MODS_DIR", mods_dir)
    return recipes_dir, mods_dir


# ── Test: ensure_dirs ───────────────────────────────────────────────────────


class TestEnsureDirs:
    """Test directory creation."""

    def test_creates_custom_directories(self, tmp_path, monkeypatch):
        """Should create custom-recipes and custom-mods dirs."""
        monkeypatch.setattr(custom_files, "_CUSTOM_RECIPES_DIR", tmp_path / "r")
        monkeypatch.setattr(custom_files, "_CUSTOM_MODS_DIR", tmp_path / "m")
        custom_files._ensure_dirs()
        assert (tmp_path / "r").is_dir()
        assert (tmp_path / "m").is_dir()


# ── Test: create_symlinks ──────────────────────────────────────────────────


class TestCreateSymlinks:
    """Test symlink creation from custom dirs into spark_vllm_path."""

    def test_no_recipes_in_custom_dir(self, tmp_path, monkeypatch):
        """Should return empty when custom dirs are empty."""
        spark = tmp_path / "spark"
        spark.mkdir()
        monkeypatch.setattr(custom_files, "_CUSTOM_RECIPES_DIR", tmp_path / "cr")
        monkeypatch.setattr(custom_files, "_CUSTOM_MODS_DIR", tmp_path / "cm")
        custom_files._ensure_dirs()

        created = custom_files.create_symlinks(str(spark))
        assert created == {"recipes": [], "mods": []}

    def test_create_recipe_symlink(self, tmp_path, monkeypatch):
        """Should create a symlink for a YAML file, stripping extension."""
        spark = tmp_path / "spark"
        spark.mkdir()
        (spark / "recipes").mkdir()
        recipes_dir, _ = _make_temp_paths(monkeypatch, tmp_path)

        fpath = recipes_dir / "test-recipe.yaml"
        fpath.write_text("name: Test\n", encoding="utf-8")

        created = custom_files.create_symlinks(str(spark))
        assert "custom-test-recipe" in created["recipes"]
        link = spark / "recipes" / "custom-test-recipe"
        assert link.is_symlink()
        assert link.resolve() == fpath

    def test_create_mod_symlink(self, tmp_path, monkeypatch):
        """Should create a symlink for a mod directory."""
        spark = tmp_path / "spark"
        spark.mkdir()
        (spark / "mods").mkdir()
        _, mods_dir = _make_temp_paths(monkeypatch, tmp_path)

        mpath = mods_dir / "my-mod"
        mpath.mkdir()
        (mpath / "run.sh").write_text("#!/bin/bash\necho hello", encoding="utf-8")

        created = custom_files.create_symlinks(str(spark))
        assert "custom-my-mod" in created["mods"]
        link = spark / "mods" / "custom-my-mod"
        assert link.is_symlink()
        assert link.resolve() == mpath

    def test_idempotent_creates_no_duplicates(self, tmp_path, monkeypatch):
        """Should not create duplicate symlinks on second call."""
        spark = tmp_path / "spark"
        spark.mkdir()
        (spark / "recipes").mkdir()
        recipes_dir, _ = _make_temp_paths(monkeypatch, tmp_path)

        fpath = recipes_dir / "dup.yaml"
        fpath.write_text("name: Dup\n", encoding="utf-8")

        custom_files.create_symlinks(str(spark))
        created2 = custom_files.create_symlinks(str(spark))
        assert created2 == {"recipes": [], "mods": []}

    def test_skips_existing_non_symlink(self, tmp_path, monkeypatch):
        """Should skip if spark-vllm-docker already has a file there."""
        spark = tmp_path / "spark"
        spark.mkdir()
        (spark / "recipes").mkdir()
        recipes_dir, _ = _make_temp_paths(monkeypatch, tmp_path)

        # Create the recipe to customize
        fpath = recipes_dir / "skip.yaml"
        fpath.write_text("name: Skip\n", encoding="utf-8")
        # Create a real file (not symlink) at the target location
        real_recipe = spark / "recipes" / "custom-skip"
        real_recipe.write_text("name: existing\n", encoding="utf-8")

        created = custom_files.create_symlinks(str(spark))
        assert created["recipes"] == []
        assert real_recipe.read_text() == "name: existing\n"


# ── Test: remove_symlinks ──────────────────────────────────────────────────


class TestRemoveSymlinks:
    """Test symlink removal."""

    def test_remove_created_symlinks(self, tmp_path, monkeypatch):
        """Should remove symlinks created by create_symlinks."""
        spark = tmp_path / "spark"
        spark.mkdir()
        (spark / "recipes").mkdir()
        (spark / "mods").mkdir()
        recipes_dir, mods_dir = _make_temp_paths(monkeypatch, tmp_path)

        recipes_dir / "remove-recipe.yaml"
        recipes_dir.joinpath("remove-recipe.yaml").write_text(
            "name: Remove\n", encoding="utf-8"
        )
        mpath = mods_dir / "remove-mod"
        mpath.mkdir()
        (mpath / "run.sh").write_text("#!/bin/bash", encoding="utf-8")

        custom_files.create_symlinks(str(spark))
        removed = custom_files.remove_symlinks(str(spark))
        assert removed["recipes"] >= 1
        assert removed["mods"] >= 1

        assert not (spark / "recipes" / "custom-remove-recipe").exists()
        assert not (spark / "mods" / "custom-remove-mod").exists()

    def test_does_not_remove_originals(self, tmp_path, monkeypatch):
        """Should not remove original files/dirs, only symlinks."""
        spark = tmp_path / "spark"
        spark.mkdir()
        (spark / "recipes").mkdir()
        recipes_dir, _ = _make_temp_paths(monkeypatch, tmp_path)

        recipe_path = recipes_dir / "safe.yaml"
        recipe_path.write_text("name: Safe\n", encoding="utf-8")

        custom_files.create_symlinks(str(spark))
        custom_files.remove_symlinks(str(spark))

        assert recipe_path.exists()
        assert recipe_path.read_text() == "name: Safe\n"

    def test_does_not_remove_other_symlinks(self, tmp_path, monkeypatch):
        """Should not remove symlinks not pointing to our custom dirs."""
        spark = tmp_path / "spark"
        spark.mkdir()
        (spark / "recipes").mkdir()
        recipes_dir, _ = _make_temp_paths(monkeypatch, tmp_path)
        custom_files._ensure_dirs()

        # Create a symlink to an external path
        external = spark / "external"
        external.write_text("external", encoding="utf-8")
        link = spark / "recipes" / "custom-external"
        link.symlink_to(external)

        custom_files.remove_symlinks(str(spark))
        # Should not remove external symlink (it points outside custom dir)
        assert link.exists()


# ── Test: discover_custom_recipes ──────────────────────────────────────────


class TestDiscoverCustomRecipes:
    """Test recipe discovery from custom-recipes directory."""

    def test_empty_when_missing(self, tmp_path, monkeypatch):
        """Should return empty list when custom-recipes dir is empty."""
        recipes_dir, _ = _make_temp_paths(monkeypatch, tmp_path)
        # Ensure dir is empty
        for f in recipes_dir.iterdir():
            f.unlink()

        recipes = custom_files.discover_custom_recipes()
        assert recipes == []

    def test_discovers_yaml_files(self, tmp_path, monkeypatch):
        """Should discover .yaml files in custom-recipes directory."""
        recipes_dir, _ = _make_temp_paths(monkeypatch, tmp_path)

        recipes_dir.joinpath("test-recipe.yaml").write_text(
            "name: Test Recipe\nmodel: intel/qwen", encoding="utf-8"
        )

        recipes = custom_files.discover_custom_recipes()
        assert len(recipes) == 1
        assert recipes[0]["id"] == "custom/test-recipe"
        assert recipes[0]["name"] == "Test Recipe"
        assert recipes[0]["filename"] == "test-recipe.yaml"

    def test_ignores_hidden_files(self, tmp_path, monkeypatch):
        """Should ignore files starting with dot."""
        recipes_dir, _ = _make_temp_paths(monkeypatch, tmp_path)

        recipes_dir.joinpath(".hidden.yaml").write_text(
            "name: Hidden\n", encoding="utf-8"
        )
        recipes_dir.joinpath("visible.yaml").write_text(
            "name: Visible\n", encoding="utf-8"
        )

        recipes = custom_files.discover_custom_recipes()
        ids = [r["id"] for r in recipes]
        assert "custom/visible" in ids
        assert "custom/.hidden" not in ids

    def test_discovers_invalid_yaml_as_stub(self, tmp_path, monkeypatch):
        """Should discover invalid YAML files as stubs with stem as name."""
        recipes_dir, _ = _make_temp_paths(monkeypatch, tmp_path)

        recipes_dir.joinpath("broken.yaml").write_text(
            "this is not yaml: [[[", encoding="utf-8"
        )

        recipes = custom_files.discover_custom_recipes()
        assert len(recipes) == 1
        assert recipes[0]["name"] == "broken"


# ── Test: discover_custom_mods ─────────────────────────────────────────────


class TestDiscoverCustomMods:
    """Test mod discovery from custom-mods directory."""

    def test_discovers_mod_with_run_sh(self, tmp_path, monkeypatch):
        """Should discover directories with run.sh."""
        _, mods_dir = _make_temp_paths(monkeypatch, tmp_path)

        mpath = mods_dir / "test-mod"
        mpath.mkdir()
        (mpath / "run.sh").write_text("#!/bin/bash\necho hello", encoding="utf-8")

        mods = custom_files.discover_custom_mods()
        assert len(mods) == 1
        assert mods[0]["id"] == "custom/test-mod"
        assert mods[0]["has_run_sh"] is True

    def test_ignores_dir_without_run_sh(self, tmp_path, monkeypatch):
        """Should ignore directories without run.sh."""
        _, mods_dir = _make_temp_paths(monkeypatch, tmp_path)

        d = mods_dir / "no-runsh"
        d.mkdir()
        (d / "other.txt").write_text("hello")

        mods = custom_files.discover_custom_mods()
        ids = [m["id"] for m in mods]
        assert "custom/no-runsh" not in ids

    def test_extracts_description_from_run_sh(self, tmp_path, monkeypatch):
        """Should extract first comment line from run.sh (skip shebang)."""
        _, mods_dir = _make_temp_paths(monkeypatch, tmp_path)

        mpath = mods_dir / "desc-mod"
        mpath.mkdir()
        (mpath / "run.sh").write_text(
            "#!/bin/bash\n# This is my custom mod\n\necho hello", encoding="utf-8"
        )

        mods = custom_files.discover_custom_mods()
        assert len(mods) == 1
        assert mods[0]["description"] == "This is my custom mod"


# ── Test: get/save/delete custom recipe ────────────────────────────────────


class TestRecipeContent:
    """Test get/save/delete for custom recipe content."""

    def test_get_recipe_content(self, tmp_path, monkeypatch):
        recipes_dir, _ = _make_temp_paths(monkeypatch, tmp_path)

        content = "name: My Recipe\nmodel: intel/qwen"
        custom_files.save_custom_recipe("custom/my-recipe", content)

        result = custom_files.get_custom_recipe_content("custom/my-recipe")
        assert result is not None
        assert result["content"] == content
        assert result["id"] == "custom/my-recipe"

    def test_get_nonexistent_recipe(self, tmp_path, monkeypatch):
        _, _ = _make_temp_paths(monkeypatch, tmp_path)
        result = custom_files.get_custom_recipe_content("custom/nonexistent")
        assert result is None

    def test_save_valid_yaml(self, tmp_path, monkeypatch):
        recipes_dir, _ = _make_temp_paths(monkeypatch, tmp_path)

        result = custom_files.save_custom_recipe(
            "custom/new-recipe", "name: New\nmodel: foo"
        )
        assert result is True
        assert (recipes_dir / "new-recipe.yaml").exists()

    def test_save_invalid_yaml_raises(self, tmp_path, monkeypatch):
        _, _ = _make_temp_paths(monkeypatch, tmp_path)

        with pytest.raises(ValueError, match="Invalid YAML"):
            custom_files.save_custom_recipe("custom/bad", "not: [[valid: yaml")

    def test_save_recipe_rejects_path_traversal(self, tmp_path, monkeypatch):
        _, _ = _make_temp_paths(monkeypatch, tmp_path)

        with pytest.raises(ValueError, match="Invalid recipe id"):
            custom_files.save_custom_recipe("custom/../escape", "name: Bad\nmodel: foo")

    def test_delete_recipe(self, tmp_path, monkeypatch):
        recipes_dir, _ = _make_temp_paths(monkeypatch, tmp_path)

        custom_files.save_custom_recipe("custom/del-me", "name: Del\n")
        deleted = custom_files.delete_custom_recipe("custom/del-me")
        assert deleted is True
        assert not (recipes_dir / "del-me.yaml").exists()

    def test_delete_nonexistent_recipe(self, tmp_path, monkeypatch):
        _, _ = _make_temp_paths(monkeypatch, tmp_path)
        deleted = custom_files.delete_custom_recipe("custom/nonexistent")
        assert deleted is False


# ── Test: get/save/delete custom mod ──────────────────────────────────────


class TestModContent:
    """Test get/save/delete for custom mod content."""

    def test_get_mod_files(self, tmp_path, monkeypatch):
        _, mods_dir = _make_temp_paths(monkeypatch, tmp_path)

        mpath = mods_dir / "get-mod"
        mpath.mkdir()
        (mpath / "run.sh").write_text("#!/bin/bash", encoding="utf-8")
        (mpath / "template.jinja").write_text("{{ hello }}", encoding="utf-8")

        result = custom_files.get_custom_mod_files("custom/get-mod")
        assert result is not None
        assert "run.sh" in result["files"]
        assert "template.jinja" in result["files"]

    def test_save_mod_files(self, tmp_path, monkeypatch):
        _, mods_dir = _make_temp_paths(monkeypatch, tmp_path)

        result = custom_files.save_custom_mod(
            "custom/save-mod",
            {
                "run.sh": "#!/bin/bash\necho hello",
                "template.jinja": "{{ world }}",
            },
        )
        assert result is True
        assert (mods_dir / "save-mod" / "run.sh").exists()
        assert (mods_dir / "save-mod" / "template.jinja").exists()

    def test_save_mod_files_rejects_path_traversal(self, tmp_path, monkeypatch):
        _, _ = _make_temp_paths(monkeypatch, tmp_path)

        with pytest.raises(ValueError, match="Invalid mod file path"):
            custom_files.save_custom_mod(
                "custom/save-mod",
                {
                    "run.sh": "#!/bin/bash",
                    "../../escape.txt": "oops",
                },
            )

    def test_delete_mod(self, tmp_path, monkeypatch):
        _, mods_dir = _make_temp_paths(monkeypatch, tmp_path)

        mpath = mods_dir / "del-mod"
        mpath.mkdir()
        (mpath / "run.sh").write_text("#!/bin/bash", encoding="utf-8")

        deleted = custom_files.delete_custom_mod("custom/del-mod")
        assert deleted is True
        assert not mpath.exists()

    def test_delete_nonexistent_mod(self, tmp_path, monkeypatch):
        _, _ = _make_temp_paths(monkeypatch, tmp_path)
        deleted = custom_files.delete_custom_mod("custom/nonexistent")
        assert deleted is False
