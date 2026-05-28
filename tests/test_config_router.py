"""Unit tests for the /api/config endpoint."""

from __future__ import annotations

from spark_pulse.app import create_app
from spark_pulse.config import config


class TestApiConfigEndpoint:
    """Tests for the /api/config endpoint."""

    def test_config_returns_auth_enabled_false(self, monkeypatch):
        """Config should report auth_enabled=False when disabled."""
        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "false")

        app = create_app()
        from fastapi.testclient import TestClient

        client = TestClient(app)

        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_enabled"] is False
        # oidc_configured is no longer exposed to the frontend

    def test_config_returns_auth_enabled_true(self, monkeypatch):
        """Config should report auth_enabled=True when enabled."""
        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")

        app = create_app()
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_enabled"] is True

    def test_config_returns_mcp_enabled(self, monkeypatch):
        """Config should report mcp_enabled status."""
        monkeypatch.setitem(config._data, "mcp_enabled", True)

        app = create_app()
        from fastapi.testclient import TestClient

        client = TestClient(app)

        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mcp_enabled"] is True

    def test_config_returns_cluster_enabled(self, monkeypatch):
        """Config should report cluster_enabled status."""
        monkeypatch.setitem(config._data, "cluster_enabled", True)

        app = create_app()
        from fastapi.testclient import TestClient

        client = TestClient(app)

        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cluster_enabled"] is True

    def test_config_does_not_expose_oidc_configured(self, monkeypatch):
        """oidc_configured should not be in the API response.

        The frontend only needs to know whether auth is enabled (auth_enabled).
        The internal OIDC setup state is not exposed.
        """
        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")

        app = create_app()
        from fastapi.testclient import TestClient

        client = TestClient(app)

        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "oidc_configured" not in data
