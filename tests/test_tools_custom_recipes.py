"""Unit tests for the custom_recipes tool module."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy import event

from spark_pulse.tools import custom_recipes

# ── Test: load_customizations ────────────────────────────────────────────────


class TestLoadCustomizations:
    """Test loading customizations from disk."""

    def test_empty_when_missing(self, tmp_path):
        """When file doesn't exist, should return empty dict."""
        with patch.object(custom_recipes, "_CUSTOM_PATH", tmp_path / "missing.json"):
            result = custom_recipes.load_customizations()
            assert result == {}

    def test_returns_parsed_json(self, tmp_path):
        """When file exists with valid JSON, should return parsed data."""
        data = {"my-recipe": {"command": "echo hello"}}
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps(data), encoding="utf-8")

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            result = custom_recipes.load_customizations()
            assert result == data

    def test_returns_empty_on_corrupt_json(self, tmp_path):
        """When file has invalid JSON, should return empty dict."""
        fpath = tmp_path / "custom.json"
        fpath.write_text("{ invalid json", encoding="utf-8")

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            result = custom_recipes.load_customizations()
            assert result == {}

    def test_returns_empty_on_non_dict(self, tmp_path):
        """When file has non-dict JSON, should return empty dict."""
        fpath = tmp_path / "custom.json"
        fpath.write_text('["not", "a", "dict"]', encoding="utf-8")

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            result = custom_recipes.load_customizations()
            assert result == {}


# ── Test: save_customizations ────────────────────────────────────────────────


class TestSaveCustomizations:
    """Test saving customizations to disk."""

    def test_what_is_saved_is_what_loads(self, tmp_path):
        """The round trip, asserted through the API rather than the storage.

        These used to read the JSON file back. Customizations live in the
        database now, so what is worth pinning is that a save is visible to
        the next load — which is the property the file assertions stood in
        for."""
        data = {"recipe-1": {"command": "echo test"}}

        custom_recipes.save_customizations(data)

        assert custom_recipes.load_customizations() == data

    def test_saving_replaces_rather_than_merges(self, tmp_path):
        custom_recipes.save_customizations({"gone": {"command": "old"}})

        custom_recipes.save_customizations({"kept": {"command": "new"}})

        assert custom_recipes.load_customizations() == {"kept": {"command": "new"}}

    def test_a_failed_save_leaves_the_previous_set(self, tmp_path):
        """What the temp-file-and-rename gave, now given by the transaction."""
        from sqlalchemy.orm import Session as SaSession

        custom_recipes.save_customizations({"kept": {"command": "original"}})
        original = SaSession.commit
        SaSession.commit = lambda self: (_ for _ in ()).throw(OSError("disk full"))
        try:
            with pytest.raises(OSError):
                custom_recipes.save_customizations({"other": {"command": "new"}})
        finally:
            SaSession.commit = original

        assert custom_recipes.load_customizations() == {"kept": {"command": "original"}}

    def _removed_test_atomic_write_via_tmp(self, tmp_path):
        """Should use atomic write (tmp + rename)."""
        fpath = tmp_path / "custom.json"
        original_write = tmp_path / "custom.json.tmp"

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            custom_recipes.save_customizations({"key": "value"})

        # Original file should exist
        assert fpath.exists()
        # .tmp file should be gone after rename
        assert not original_write.exists()


# ── Test: get_customization ──────────────────────────────────────────────────


class TestGetCustomization:
    """Test retrieving a single recipe customization."""

    def test_returns_customization_when_exists(self, tmp_path):
        """Should return the customization dict when it exists."""
        data = {"my-recipe": {"command": "custom-cmd", "defaults": {"port": 9999}}}
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps(data), encoding="utf-8")

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            result = custom_recipes.get_customization("my-recipe")
            assert result == {"command": "custom-cmd", "defaults": {"port": 9999}}

    def test_returns_none_when_missing(self, tmp_path):
        """Should return None when no customization exists."""
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps({"other": {}}), encoding="utf-8")

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            result = custom_recipes.get_customization("my-recipe")
            assert result is None


# ── Test: save_customization ────────────────────────────────────────────────


class TestSaveCustomization:
    """Test saving a single recipe customization."""

    def test_creates_new_entry(self, tmp_path):
        """Should create a new entry in the file."""
        fpath = tmp_path / "custom.json"

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            result = custom_recipes.save_customization(
                "new-recipe", {"command": "echo hi"}
            )

        assert result["command"] == "echo hi"

        stored = custom_recipes.load_customizations()
        assert "new-recipe" in stored

    def test_merges_into_existing(self, tmp_path):
        """Should merge into existing entries, not overwrite others."""
        existing = {"other-recipe": {"container": "custom-container"}}
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps(existing), encoding="utf-8")

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            result = custom_recipes.save_customization(
                "new-recipe", {"model": "new-model"}
            )

        # Result is the merged dict for new-recipe
        assert result["model"] == "new-model"
        # File should have both entries
        stored = custom_recipes.load_customizations()
        assert "other-recipe" in stored
        assert "new-recipe" in stored

    def test_only_stores_customizable_fields(self, tmp_path):
        """Should only store fields in CUSTOMIZABLE_FIELDS."""
        fpath = tmp_path / "custom.json"
        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            result = custom_recipes.save_customization(
                "r",
                {
                    "command": "new-cmd",
                    "description": "ignored",  # not in CUSTOMIZABLE_FIELDS
                    "defaults": {"port": 8000},
                },
            )

        assert "command" in result
        assert "description" not in result
        assert "defaults" in result

        # Verify file state
        stored = custom_recipes.load_customizations()
        assert stored["r"]["command"] == "new-cmd"
        assert "description" not in stored["r"]

    def test_deletes_when_empty(self, tmp_path):
        """Should remove entry when only non-customizable fields are given."""
        existing = {"r": {"command": "old"}}
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps(existing), encoding="utf-8")

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            result = custom_recipes.save_customization("r", {"description": "only"})

        assert "r" not in result


# ── Test: delete_customization ──────────────────────────────────────────────


class TestDeleteCustomization:
    """Test deleting a recipe customization."""

    def test_deletes_existing(self, tmp_path):
        """Should remove the entry and return True."""
        existing = {"r": {"command": "cmd"}}
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps(existing), encoding="utf-8")

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            result = custom_recipes.delete_customization("r")

        assert result is True
        assert custom_recipes.get_customization("r") is None

    def test_returns_false_when_missing(self, tmp_path):
        """Should return False when no entry exists."""
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps({}), encoding="utf-8")

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            result = custom_recipes.delete_customization("r")

        assert result is False

    def test_preserves_other_entries(self, tmp_path):
        """Should not affect other recipe entries."""
        existing = {"r1": {"a": 1}, "r2": {"b": 2}}
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps(existing), encoding="utf-8")

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            custom_recipes.delete_customization("r1")
            assert custom_recipes.get_customization("r1") is None
            assert custom_recipes.get_customization("r2") == {"b": 2}


# ── Test: has_customization ──────────────────────────────────────────────────


class TestHasCustomization:
    """Test quick boolean check for customization existence."""

    def test_returns_true_when_exists(self, tmp_path):
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps({"r": {}}), encoding="utf-8")
        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            assert custom_recipes.has_customization("r") is True

    def test_returns_false_when_missing(self, tmp_path):
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps({}), encoding="utf-8")
        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            assert custom_recipes.has_customization("r") is False


# ── Test: get_customized_recipe ─────────────────────────────────────────────


class TestGetCustomizedRecipe:
    """Test recipe merge with customizations."""

    def test_no_customization_returns_original(self, tmp_path):
        """When no customization exists, should return original recipe."""
        fake_recipe = {
            "id": "r",
            "name": "Test",
            "model": "model-x",
            "command": "cmd",
            "defaults": {},
        }
        with patch(
            "spark_pulse.tools.custom_recipes.get_customization", return_value=None
        ):
            with patch(
                "spark_pulse.tools.recipes.get_recipe", return_value=fake_recipe
            ):
                result = custom_recipes.get_customized_recipe("r", spark_path=tmp_path)
                assert result == fake_recipe

    def test_merges_custom_defaults(self, tmp_path):
        """User defaults should merge with original defaults (user wins)."""
        original = {
            "id": "r",
            "name": "Test",
            "model": "m",
            "command": "cmd",
            "defaults": {"port": 8000, "gpu_mem_util": 0.8},
        }
        customization = {"defaults": {"port": 9999, "extra": 42}}

        with patch(
            "spark_pulse.tools.custom_recipes.get_customization",
            return_value=customization,
        ):
            with patch("spark_pulse.tools.recipes.get_recipe", return_value=original):
                result = custom_recipes.get_customized_recipe("r")

        assert result["defaults"]["port"] == 9999  # user override
        assert result["defaults"]["gpu_mem_util"] == 0.8  # original preserved
        assert result["defaults"]["extra"] == 42  # user new

    def test_overrides_fields(self, tmp_path):
        """Non-default fields should be directly overridden."""
        original = {
            "id": "r",
            "name": "Test",
            "model": "original-model",
            "command": "vllm serve {model}",
            "env": {"X": "1"},
            "build_args": ["--arg"],
            "container": "vllm-node",
            "defaults": {},
        }
        customization = {
            "model": "custom-model",
            "command": "custom serve {model}",
            "env": {"Y": "2"},
            "build_args": ["--custom"],
            "container": "custom-container",
        }

        with patch(
            "spark_pulse.tools.custom_recipes.get_customization",
            return_value=customization,
        ):
            with patch("spark_pulse.tools.recipes.get_recipe", return_value=original):
                result = custom_recipes.get_customized_recipe("r")

        assert result["model"] == "custom-model"
        assert result["command"] == "custom serve {model}"
        assert result["env"] == {"Y": "2"}
        assert result["build_args"] == ["--custom"]
        assert result["container"] == "custom-container"

    def test_returns_none_for_missing_recipe(self, tmp_path):
        """Should return None when original recipe doesn't exist."""
        with patch(
            "spark_pulse.tools.custom_recipes.get_customization", return_value={}
        ):
            with patch("spark_pulse.tools.recipes.get_recipe", return_value=None):
                result = custom_recipes.get_customized_recipe("missing")
                assert result is None


# ── Test: per-recipe writes ─────────────────────────────────────────────────


@contextmanager
def _rows_written():
    """Collect the INSERT/UPDATE/DELETE statements the block sends to the table.

    Whether one recipe or all of them were rewritten is invisible in the data —
    both leave the store correct — and visible only in the statements it took.
    """
    from spark_pulse.db import engine

    statements: list[str] = []

    def _watch(conn, cursor, statement, parameters, context, executemany):
        head = statement.strip().split(maxsplit=1)[0].upper()
        if head in ("INSERT", "UPDATE", "DELETE") and "recipe_customizations" in (
            statement
        ):
            statements.append(statement)

    live = engine()
    event.listen(live, "before_cursor_execute", _watch)
    try:
        yield statements
    finally:
        event.remove(live, "before_cursor_execute", _watch)


class TestPerRecipeWrites:
    """Customizing one recipe must cost one row, not every customized recipe."""

    def test_saving_one_recipe_writes_only_that_row(self):
        custom_recipes.save_customizations(
            {"r1": {"command": "a"}, "r2": {"command": "b"}, "r3": {"command": "c"}}
        )

        with _rows_written() as statements:
            custom_recipes.save_customization("r2", {"model": "m"})

        assert len(statements) == 1
        assert custom_recipes.get_customization("r1") == {"command": "a"}

    def test_deleting_one_recipe_writes_only_that_row(self):
        custom_recipes.save_customizations(
            {"r1": {"command": "a"}, "r2": {"command": "b"}, "r3": {"command": "c"}}
        )

        with _rows_written() as statements:
            custom_recipes.delete_customization("r2")

        assert len(statements) == 1
        assert set(custom_recipes.load_customizations()) == {"r1", "r3"}

    def test_a_save_with_nothing_customizable_in_it_creates_no_row(self):
        """And leaves every other recipe where it was."""
        custom_recipes.save_customizations({"r2": {"command": "b"}})

        assert custom_recipes.save_customization("r1", {"description": "only"}) == {}

        assert custom_recipes.load_customizations() == {"r2": {"command": "b"}}

    def test_the_whole_set_save_writes_only_the_recipes_that_changed(self):
        """The kept API, no longer paying for the rows it was handed unchanged."""
        custom_recipes.save_customizations(
            {"r1": {"command": "a"}, "r2": {"command": "b"}, "r3": {"command": "c"}}
        )

        with _rows_written() as statements:
            custom_recipes.save_customizations(
                {
                    "r1": {"command": "a"},
                    "r2": {"command": "CHANGED"},
                    "r3": {"command": "c"},
                }
            )

        assert len(statements) == 1
        assert custom_recipes.get_customization("r2") == {"command": "CHANGED"}

    def test_a_single_recipe_save_waits_for_a_whole_set_write_in_flight(self):
        """The lost update the mutex exists for.

        A whole-set save deletes every recipe absent from the set it was given,
        so a per-recipe save landing in the middle of one would be deleted by
        it. Both take the same mutex, so the second one waits.
        """
        finished = threading.Event()

        def save_one():
            custom_recipes.save_customization("r", {"command": "x"})
            finished.set()

        with custom_recipes.transaction():
            worker = threading.Thread(target=save_one)
            worker.start()
            assert not finished.wait(0.3), "the save did not wait for the mutex"

        worker.join(5)
        assert finished.is_set()
        assert custom_recipes.get_customization("r") == {"command": "x"}


# ── Test: the one-time JSON import ──────────────────────────────────────────


class TestTheOneTimeImport:
    def test_a_point_read_triggers_the_import(self, tmp_path):
        """``get_customization`` reads one row now, so it must import there too."""
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps({"r": {"command": "from-json"}}), encoding="utf-8")

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            assert custom_recipes.get_customization("r") == {"command": "from-json"}

    def test_the_import_does_not_run_again_after_the_last_row_is_deleted(
        self, tmp_path
    ):
        """Keyed on the ``meta`` table, never on "is the table empty".

        Deleting the last customization empties the table, which is exactly the
        state an emptiness check would mistake for a fresh install — and it
        would import the deleted recipe straight back.
        """
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps({"r": {"command": "from-json"}}), encoding="utf-8")

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            assert custom_recipes.delete_customization("r") is True

            assert custom_recipes.get_customization("r") is None
            assert custom_recipes.load_customizations() == {}

    def test_a_whole_set_save_is_not_undone_by_an_import_that_had_not_run(
        self, tmp_path
    ):
        """The save has to import first, or its deletions are reversed.

        Writing before the import means the import merges the old file in
        afterwards, resurrecting every recipe the caller had just removed.
        """
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps({"gone": {"command": "old"}}), encoding="utf-8")

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            custom_recipes.save_customizations({"kept": {"command": "new"}})

            assert custom_recipes.load_customizations() == {"kept": {"command": "new"}}
