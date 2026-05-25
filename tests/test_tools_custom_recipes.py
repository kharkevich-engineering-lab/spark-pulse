"""Unit tests for the custom_recipes tool module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, mock_open

import pytest

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

    def test_writes_json_file(self, tmp_path):
        """Should write a valid JSON file."""
        data = {"recipe-1": {"command": "echo test"}}
        fpath = tmp_path / "custom.json"

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            custom_recipes.save_customizations(data)

        assert fpath.exists()
        loaded = json.loads(fpath.read_text())
        assert loaded == data

    def test_creates_parent_dir(self, tmp_path):
        """Should create parent directory if missing."""
        nested = tmp_path / "deep" / "nested" / "dir.json"

        with patch.object(custom_recipes, "_CUSTOM_PATH", nested):
            custom_recipes.save_customizations({})

        assert nested.parent.exists()

    def test_atomic_write_via_tmp(self, tmp_path):
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
            result = custom_recipes.save_customization("new-recipe", {"command": "echo hi"})

        assert result["command"] == "echo hi"

        # Verify file has the entry
        stored = json.loads(fpath.read_text())
        assert "new-recipe" in stored

    def test_merges_into_existing(self, tmp_path):
        """Should merge into existing entries, not overwrite others."""
        existing = {"other-recipe": {"container": "custom-container"}}
        fpath = tmp_path / "custom.json"
        fpath.write_text(json.dumps(existing), encoding="utf-8")

        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            result = custom_recipes.save_customization("new-recipe", {"model": "new-model"})

        # Result is the merged dict for new-recipe
        assert result["model"] == "new-model"
        # File should have both entries
        stored = json.loads(fpath.read_text())
        assert "other-recipe" in stored
        assert "new-recipe" in stored

    def test_only_stores_customizable_fields(self, tmp_path):
        """Should only store fields in CUSTOMIZABLE_FIELDS."""
        fpath = tmp_path / "custom.json"
        with patch.object(custom_recipes, "_CUSTOM_PATH", fpath):
            result = custom_recipes.save_customization("r", {
                "command": "new-cmd",
                "description": "ignored",  # not in CUSTOMIZABLE_FIELDS
                "defaults": {"port": 8000},
            })

        assert "command" in result
        assert "description" not in result
        assert "defaults" in result

        # Verify file state
        stored = json.loads(fpath.read_text())
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
        fake_recipe = {"id": "r", "name": "Test", "model": "model-x", "command": "cmd", "defaults": {}}
        with patch("spark_pulse.tools.custom_recipes.get_customization", return_value=None):
            with patch("spark_pulse.tools.recipes.get_recipe", return_value=fake_recipe):
                result = custom_recipes.get_customized_recipe("r", spark_path=tmp_path)
                assert result == fake_recipe

    def test_merges_custom_defaults(self, tmp_path):
        """User defaults should merge with original defaults (user wins)."""
        original = {
            "id": "r", "name": "Test", "model": "m", "command": "cmd",
            "defaults": {"port": 8000, "gpu_mem_util": 0.8},
        }
        customization = {"defaults": {"port": 9999, "extra": 42}}

        with patch("spark_pulse.tools.custom_recipes.get_customization", return_value=customization):
            with patch("spark_pulse.tools.recipes.get_recipe", return_value=original):
                result = custom_recipes.get_customized_recipe("r")

        assert result["defaults"]["port"] == 9999  # user override
        assert result["defaults"]["gpu_mem_util"] == 0.8  # original preserved
        assert result["defaults"]["extra"] == 42  # user new

    def test_overrides_fields(self, tmp_path):
        """Non-default fields should be directly overridden."""
        original = {
            "id": "r", "name": "Test", "model": "original-model",
            "command": "vllm serve {model}", "env": {"X": "1"},
            "build_args": ["--arg"], "container": "vllm-node",
            "defaults": {},
        }
        customization = {
            "model": "custom-model",
            "command": "custom serve {model}",
            "env": {"Y": "2"},
            "build_args": ["--custom"],
            "container": "custom-container",
        }

        with patch("spark_pulse.tools.custom_recipes.get_customization", return_value=customization):
            with patch("spark_pulse.tools.recipes.get_recipe", return_value=original):
                result = custom_recipes.get_customized_recipe("r")

        assert result["model"] == "custom-model"
        assert result["command"] == "custom serve {model}"
        assert result["env"] == {"Y": "2"}
        assert result["build_args"] == ["--custom"]
        assert result["container"] == "custom-container"

    def test_returns_none_for_missing_recipe(self, tmp_path):
        """Should return None when original recipe doesn't exist."""
        with patch("spark_pulse.tools.custom_recipes.get_customization", return_value={}):
            with patch("spark_pulse.tools.recipes.get_recipe", return_value=None):
                result = custom_recipes.get_customized_recipe("missing")
                assert result is None
