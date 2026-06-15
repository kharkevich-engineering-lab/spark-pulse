"""Tests for mock OCI registry provider."""

import pytest

from spark_pulse.mock.oci_registry import (
    mock_list_collections,
    mock_install_collection,
    mock_check_updates,
    mock_list_oci_recipes,
)


class TestMockOciRegistry:
    """Tests for mock OCI registry functions."""

    def test_mock_list_collections(self):
        """Mock list returns sample collections."""
        collections = mock_list_collections()
        assert len(collections) >= 1
        assert collections[0].name == "spark-recipes"
        assert collections[0].version == "1.0.0"

    def test_mock_list_collections_filter_by_name(self):
        """Mock list with registry filter works."""
        collections = mock_list_collections(registry_name="spark-official")
        assert all(c.registry == "spark-official" for c in collections)

    def test_mock_install_collection(self, tmp_path, monkeypatch):
        """Mock install creates recipe files and metadata."""
        monkeypatch.setenv("HOME", str(tmp_path))
        import importlib
        import spark_pulse.tools.oci_registry as mod
        importlib.reload(mod)
        from spark_pulse.mock import oci_registry as mock_mod
        mock_mod.RECIPES_DIR = mod.RECIPES_DIR

        installed = mock_install_collection(
            name="spark-recipes",
            version="1.0.0",
        )
        assert len(installed) >= 1
        # Check that files were created
        for filename in installed:
            assert (mod.RECIPES_DIR / filename).exists()
            assert (mod.RECIPES_DIR / f"{filename}.meta").exists()

    def test_mock_check_updates(self):
        """Mock check returns sample updates."""
        updates = mock_check_updates()
        assert len(updates) >= 1
        assert updates[0].collection == "spark-recipes"
        assert updates[0].current_version == "1.0.0"
        assert updates[0].latest_version == "1.1.0"

    def test_mock_list_oci_recipes(self):
        """Mock list returns sample OCI-installed recipes."""
        recipes = mock_list_oci_recipes()
        assert len(recipes) >= 1
        assert recipes[0].source == "spark-official"
        assert recipes[0].collection == "spark-recipes"
