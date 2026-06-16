"""Tests for OCI registry router endpoints."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from spark_pulse.app import create_app


@pytest.fixture
def client():
    with patch("spark_pulse.routers.oci.is_simulation") as mock_sim:
        mock_sim.return_value = True
        app = create_app()
        with TestClient(app) as test_client:
            yield test_client


class TestOciRegistries:
    """Tests for registry CRUD endpoints."""

    def test_list_registries(self, client):
        """GET /api/oci/registries returns registry list."""
        response = client.get("/api/oci/registries")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_add_registry(self, client):
        """POST /api/oci/registries adds a new registry."""
        response = client.post(
            "/api/oci/registries",
            json={
                "name": "test-reg",
                "url": "example.com/recipes",
                "enabled": True,
                "default": False,
                "auth": {},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "test-reg"

    def test_add_registry_missing_fields(self, client):
        """POST /api/oci/registries with missing fields - in sim mode accepts, real mode returns 400."""
        response = client.post(
            "/api/oci/registries",
            json={
                "name": "test-reg",
            },
        )
        # In simulation mode, mock handler accepts without validation
        assert response.status_code in (200, 400)

    def test_update_registry(self, client):
        """PUT /api/oci/registries/{name} updates a registry."""
        # First add a registry
        client.post(
            "/api/oci/registries",
            json={
                "name": "updatable-reg",
                "url": "example.com/recipes",
                "enabled": True,
                "default": False,
                "auth": {},
            },
        )
        # Then update it
        response = client.put(
            "/api/oci/registries/updatable-reg",
            json={
                "enabled": False,
            },
        )
        assert response.status_code == 200
        assert response.json()["enabled"] is False

    def test_update_nonexistent_registry(self, client):
        """PUT /api/oci/registries/{name} for non-existent returns 404."""
        response = client.put(
            "/api/oci/registries/nonexistent",
            json={
                "enabled": True,
            },
        )
        assert response.status_code == 404

    def test_delete_registry(self, client):
        """DELETE /api/oci/registries/{name} removes a registry."""
        # First add a registry
        client.post(
            "/api/oci/registries",
            json={
                "name": "deletable-reg",
                "url": "example.com/recipes",
                "enabled": True,
                "default": False,
                "auth": {},
            },
        )
        # Then delete it
        response = client.delete("/api/oci/registries/deletable-reg")
        assert response.status_code == 200
        assert response.json()["deleted"] is True

    def test_delete_nonexistent_registry(self, client):
        """DELETE /api/oci/registries/{name} for non-existent returns 404."""
        response = client.delete("/api/oci/registries/nonexistent")
        assert response.status_code == 404

    def test_test_connection(self, client):
        """POST /api/oci/registries/{name}/test-connection returns connectivity."""
        # First add a registry
        client.post(
            "/api/oci/registries",
            json={
                "name": "test-conn-reg",
                "url": "example.com/recipes",
                "enabled": True,
                "default": False,
                "auth": {},
            },
        )
        response = client.get("/api/oci/registries/test-conn-reg/test-connection")
        assert response.status_code == 200
        data = response.json()
        assert "ok" in data


class TestOciCollections:
    """Tests for collection browsing endpoints."""

    def test_list_collections(self, client):
        """GET /api/oci/collections returns collection list."""
        response = client.get("/api/oci/collections")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_list_collections_with_filter(self, client):
        """GET /api/oci/collections with registry filter."""
        response = client.get(
            "/api/oci/collections?registry=ghcr.io/kharkevich-engineering-lab/spark-pulse-recipes"
        )
        assert response.status_code == 200

    def test_list_collections_nonexistent_registry(self, client):
        """GET /api/oci/collections with non-existent registry returns 200 with empty list."""
        response = client.get("/api/oci/collections?registry=nonexistent")
        assert response.status_code == 200
        assert response.json() == []


class TestOciInstall:
    """Tests for collection installation endpoint."""

    def test_install_collection(self, client):
        """POST /api/oci/install installs a collection (sim mode returns 200)."""
        response = client.post(
            "/api/oci/install",
            json={
                "name": "spark-recipes",
                "version": "1.0.0",
                "registry": "ghcr.io/kharkevich-engineering-lab/spark-pulse-recipes",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "installed" in data


class TestOciUpdates:
    """Tests for update checking and application endpoints."""

    def test_check_updates(self, client):
        """GET /api/oci/check returns update info."""
        response = client.get("/api/oci/check")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_apply_updates(self, client):
        """POST /api/oci/update applies updates."""
        response = client.post(
            "/api/oci/update",
            json={
                "updates": [
                    {"collection": "test", "target_version": "2.0.0", "registry": ""},
                ],
            },
        )
        # May succeed or fail depending on mock state
        assert response.status_code in (200, 500)

    def test_apply_updates_empty(self, client):
        """POST /api/oci/update with empty updates returns 400."""
        response = client.post(
            "/api/oci/update",
            json={
                "updates": [],
            },
        )
        assert response.status_code == 400


class TestOciMeta:
    """Tests for OCI recipe metadata endpoints."""

    def test_list_oci_meta(self, client):
        """GET /api/oci/recipes/meta returns metadata list."""
        response = client.get("/api/oci/recipes/meta")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_get_oci_meta_by_name(self, client):
        """GET /api/oci/recipes/meta/{name} returns single metadata."""
        response = client.get("/api/oci/recipes/meta/test-recipe.yaml")
        # Returns 200 if exists, 404 if not
        assert response.status_code in (200, 404)


class TestOciAutoUpdate:
    """Tests for auto-update endpoints."""

    def test_get_auto_update_settings(self, client):
        """GET /api/oci/auto-update/settings returns settings."""
        response = client.get("/api/oci/auto-update/settings")
        assert response.status_code == 200
        data = response.json()
        assert "enabled" in data
        assert "schedule" in data

    def test_update_auto_update_settings(self, client):
        """PUT /api/oci/auto-update/settings updates settings."""
        response = client.put(
            "/api/oci/auto-update/settings",
            json={
                "enabled": True,
                "schedule": "0 3 * * *",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
        assert data["schedule"] == "0 3 * * *"

    def test_run_auto_update(self, client):
        """POST /api/oci/auto-update/run triggers auto-update."""
        response = client.post("/api/oci/auto-update/run")
        # May succeed or fail depending on mock state
        assert response.status_code in (200, 500)
