"""Functional tests for the custom_recipes router.

These tests use the FastAPI TestClient with mocked storage.

Usage:
    pytest tests/test_router_custom_recipes.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from spark_pulse.app import create_app


@pytest.fixture
def app_client():
    """Create a test FastAPI app and return a TestClient."""
    app = create_app()
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def custom_path(tmp_path):
    """Return a temp path for custom-recipes.json."""
    return tmp_path / "custom-recipes.json"


@pytest.fixture
def mock_custom_path(custom_path):
    """Patch the custom recipes path to use a temp file."""
    import spark_pulse.tools.custom_recipes as cr

    original = cr._CUSTOM_PATH
    cr._CUSTOM_PATH = custom_path
    yield custom_path
    cr._CUSTOM_PATH = original


# ── Test: GET /api/recipes/customize/{id} ──────────────────────────────────


class TestGetCustomization:
    """Test the GET customization endpoint."""

    def test_returns_empty_when_none(self, app_client, mock_custom_path):
        resp = app_client.get("/api/recipes/customize/my-recipe")
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_returns_customization(self, app_client, mock_custom_path):
        custom = {"command": "custom-serve", "defaults": {"port": 9999}}
        mock_custom_path.write_text(json.dumps({"my-recipe": custom}))

        resp = app_client.get("/api/recipes/customize/my-recipe")
        assert resp.status_code == 200
        assert resp.json() == custom

    def test_returns_none_for_missing(self, app_client, mock_custom_path):
        mock_custom_path.write_text(json.dumps({"other": {}}))
        resp = app_client.get("/api/recipes/customize/my-recipe")
        assert resp.status_code == 200
        assert resp.json() == {}


# ── Test: PUT /api/recipes/customize/{id} ──────────────────────────────────


class TestSaveCustomization:
    """Test the PUT customization endpoint."""

    def test_saves_customization(self, app_client, mock_custom_path):
        body = {"command": "custom-cmd", "defaults": {"port": 8888}}
        resp = app_client.put("/api/recipes/customize/my-recipe", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["command"] == "custom-cmd"
        assert data["defaults"]["port"] == 8888

    def test_stores_only_customizable_fields(self, app_client, mock_custom_path):
        body = {"command": "x", "description": "ignored", "model": "m"}
        resp = app_client.put("/api/recipes/customize/my-recipe", json=body)
        assert resp.status_code == 200
        result = resp.json()
        assert "command" in result
        assert "model" in result
        assert "description" not in result

    def test_updates_existing(self, app_client, mock_custom_path):
        mock_custom_path.write_text(json.dumps({"r": {"command": "old"}}))
        resp = app_client.put("/api/recipes/customize/r", json={"model": "new"})
        assert resp.status_code == 200
        # The stored file should have both
        stored = json.loads(mock_custom_path.read_text())
        assert stored["r"]["command"] == "old"
        assert stored["r"]["model"] == "new"


# ── Test: DELETE /api/recipes/customize/{id} ──────────────────────────────


class TestDeleteCustomization:
    """Test the DELETE customization endpoint."""

    def test_deletes_existing(self, app_client, mock_custom_path):
        mock_custom_path.write_text(json.dumps({"r": {"command": "cmd"}}))
        resp = app_client.delete("/api/recipes/customize/r")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_returns_false_when_missing(self, app_client, mock_custom_path):
        mock_custom_path.write_text(json.dumps({}))
        resp = app_client.delete("/api/recipes/customize/r")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is False

    def test_preserves_other_entries(self, app_client, mock_custom_path):
        mock_custom_path.write_text(json.dumps({"r1": {}, "r2": {}}))
        resp = app_client.delete("/api/recipes/customize/r1")
        assert resp.status_code == 200
        stored = json.loads(mock_custom_path.read_text())
        assert "r1" not in stored
        assert "r2" in stored


# ── Test: recipes list includes is_customized ──────────────────────────────


class TestRecipeSummaryIncludesCustomized:
    """Test that recipe list includes is_customized field."""

    def test_list_recipes_includes_is_customized(self, app_client, custom_path):
        """GET /api/recipes should include is_customized for each recipe."""
        # Create a fake spark-vllm-docker with a recipe
        spark_path = custom_path.parent / "spark-vllm-docker"
        spark_path.mkdir(exist_ok=True)
        recipes_dir = spark_path / "recipes"
        recipes_dir.mkdir()
        (recipes_dir / "test-recipe.yaml").write_text(
            "name: Test\nmodel: test-model\n", encoding="utf-8"
        )

        import spark_pulse.config as config_mod

        original_path = config_mod.config._data.get("spark_vllm_path")
        config_mod.config._data["spark_vllm_path"] = str(spark_path)

        # Patch custom path to avoid interference
        import spark_pulse.tools.custom_recipes as cr

        original_custom = cr._CUSTOM_PATH
        cr._CUSTOM_PATH = custom_path

        try:
            resp = app_client.get("/api/recipes")
            assert resp.status_code == 200
            data = resp.json()
            recipe = next((r for r in data if r["id"] == "test-recipe"), None)
            assert recipe is not None
            assert "is_customized" in recipe
            assert isinstance(recipe["is_customized"], bool)
        finally:
            if original_path:
                config_mod.config._data["spark_vllm_path"] = original_path
            else:
                config_mod.config._data.pop("spark_vllm_path", None)
            cr._CUSTOM_PATH = original_custom
