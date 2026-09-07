"""Reading the recipes and mods a previous build imported.

The importer that wrote this directory is gone, along with the "Import from
upstream" panel that drove it. What is left is the reading half, and it is
tested by building the directory by hand rather than by running an import —
which is also how it will be met in production from now on: an operator upgrades,
the importer is no longer there, and the files it left behind are still served.

That makes these tests stricter than the ones they replace. The old suite
imported a fixture and then asserted against whatever the importer had just
written, so it could not have caught a reader that disagreed with the layout
on disk. Here the layout is the fixture.
"""

import json
import sys
from pathlib import Path

import pytest

import spark_pulse.tools.recipe_import  # noqa: F401  (load the real submodule)

# The suite runs with SIMULATION_MODE=1. ``recipe_import`` is real-only now —
# it reads a directory and has nothing left to simulate — but addressing it
# through sys.modules keeps this honest whichever way the switch is set.
recipe_import = sys.modules["spark_pulse.tools.recipe_import"]

VALID_V1 = """
name: TinyLlama
container: vllm-node
model: TinyLlama/TinyLlama-1.1B
command: vllm serve TinyLlama/TinyLlama-1.1B --port {port}
defaults:
  port: 8000
""".strip()

VALID_V2 = """
recipe_version: "2"
name: Structured
model: org/structured
engine: vllm
params:
  port: 8100
engines:
  vllm:
    args: --enable-prefix-caching
""".strip()


@pytest.fixture
def imported(tmp_path) -> Path:
    """An ``imported/`` directory as the retired importer used to leave it."""
    root = tmp_path / "imported"
    recipes = root / "recipes"
    (recipes / "cluster").mkdir(parents=True)
    (recipes / "tiny.yaml").write_text(VALID_V1, encoding="utf-8")
    (recipes / "cluster" / "structured.yml").write_text(VALID_V2, encoding="utf-8")
    # A file that is not a recipe, sitting where recipes live.
    (recipes / "notes.md").write_text("# not a recipe\n", encoding="utf-8")

    mods = root / "mods" / "nemotron-nano"
    mods.mkdir(parents=True)
    (mods / "run.sh").write_text(
        "#!/usr/bin/env bash\necho patching\n", encoding="utf-8"
    )

    (root / "manifest.json").write_text(
        json.dumps(
            {
                "source": "/home/user/spark-vllm-docker",
                "git_sha": "abc1234",
                "imported_at": "2026-01-01T00:00:00+00:00",
                "recipes": [{"file": "tiny.yaml", "ok": True}],
            }
        ),
        encoding="utf-8",
    )
    return root


class TestTheDirectories:
    def test_the_paths_hang_off_the_root_it_is_given(self, imported):
        assert recipe_import.imported_recipes_dir(imported) == imported / "recipes"
        assert recipe_import.imported_mods_dir(imported) == imported / "mods"

    def test_without_a_root_it_uses_the_configured_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recipe_import, "IMPORTED_DIR", tmp_path / "elsewhere")

        assert (
            recipe_import.imported_recipes_dir() == tmp_path / "elsewhere" / "recipes"
        )


class TestIterImportedRecipeFiles:
    def test_lists_nothing_when_nothing_was_imported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recipe_import, "IMPORTED_DIR", tmp_path / "nope")

        assert recipe_import.iter_imported_recipe_files() == []

    def test_finds_recipes_at_every_depth(self, imported, monkeypatch):
        monkeypatch.setattr(recipe_import, "IMPORTED_DIR", imported)

        names = [p.name for p in recipe_import.iter_imported_recipe_files()]

        # Sorted, so a deploy's recipe list does not reorder itself between
        # reads for no reason the operator can see.
        assert names == ["structured.yml", "tiny.yaml"]

    def test_a_file_that_is_not_a_recipe_is_not_one(self, imported, monkeypatch):
        """``notes.md`` sits in the recipes directory and is not a recipe."""
        monkeypatch.setattr(recipe_import, "IMPORTED_DIR", imported)

        assert not any(
            p.name == "notes.md" for p in recipe_import.iter_imported_recipe_files()
        )


class TestStatus:
    def test_status_is_empty_when_nothing_was_ever_imported(self, tmp_path):
        assert recipe_import.get_import_status(tmp_path / "nope") == {"imported": False}

    def test_status_returns_the_manifest_the_importer_left(self, imported):
        status = recipe_import.get_import_status(imported)

        assert status["imported"] is True
        assert status["source"] == "/home/user/spark-vllm-docker"
        assert status["git_sha"] == "abc1234"

    def test_status_survives_a_corrupt_manifest(self, imported):
        """A half-written manifest must not take the recipes down with it."""
        (imported / "manifest.json").write_text("{not json", encoding="utf-8")

        assert recipe_import.get_import_status(imported) == {"imported": False}

    def test_status_survives_a_manifest_that_is_not_an_object(self, imported):
        (imported / "manifest.json").write_text("[]", encoding="utf-8")

        assert recipe_import.get_import_status(imported) == {"imported": False}


class TestClear:
    def test_clearing_removes_the_directory_and_says_so(self, imported):
        assert recipe_import.clear_imported(imported) is True
        assert not imported.exists()

    def test_clearing_what_is_not_there_is_not_an_error(self, tmp_path):
        assert recipe_import.clear_imported(tmp_path / "nope") is False
