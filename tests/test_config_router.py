"""Unit tests for the /api/config endpoint."""

from __future__ import annotations

import pytest

from spark_pulse.app import create_app
from spark_pulse.config import config


class TestApiConfigEndpoint:
    """Tests for the /api/config endpoint."""

    def test_config_returns_auth_enabled_false(self, monkeypatch):
        """Config should report auth_enabled=False when disabled."""
        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "false")
        monkeypatch.setitem(config._data, "oidc_provider_url", "")
        monkeypatch.setitem(config._data, "oidc_client_id", "")
        monkeypatch.setitem(config._data, "oidc_client_secret", "")

        app = create_app()
        from fastapi.testclient import TestClient
        client = TestClient(app)

        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_enabled"] is False
        assert data["oidc_configured"] is False

    def test_config_returns_auth_enabled_true(self, monkeypatch):
        """Config should report auth_enabled=True when configured."""
        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
        monkeypatch.setitem(config._data, "oidc_provider_url", "https://keycloak.example.com")
        monkeypatch.setitem(config._data, "oidc_client_id", "test-client")
        monkeypatch.setitem(config._data, "oidc_client_secret", "test-secret")

        app = create_app()
        from fastapi.testclient import TestClient
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_enabled"] is True
        assert data["oidc_configured"] is True

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

    def test_config_oidc_configured_false_when_missing_secrets(self, monkeypatch):
        """oidc_configured should be False when OIDC secrets are missing."""
        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
        monkeypatch.setitem(config._data, "oidc_provider_url", "https://keycloak.example.com")
        monkeypatch.setitem(config._data, "oidc_client_id", "test-client")
        monkeypatch.setitem(config._data, "oidc_client_secret", "")

        app = create_app()
        from fastapi.testclient import TestClient
        client = TestClient(app)

        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["oidc_configured"] is False

    def test_config_oidc_configured_false_when_missing_provider(self, monkeypatch):
        """oidc_configured should be False when provider URL is missing."""
        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
        monkeypatch.setitem(config._data, "oidc_provider_url", "")
        monkeypatch.setitem(config._data, "oidc_client_id", "test-client")
        monkeypatch.setitem(config._data, "oidc_client_secret", "test-secret")

        app = create_app()
        from fastapi.testclient import TestClient
        client = TestClient(app)

        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["oidc_configured"] is False

    def test_config_oidc_configured_false_when_missing_client_id(self, monkeypatch):
        """oidc_configured should be False when client ID is missing."""
        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
        monkeypatch.setitem(config._data, "oidc_provider_url", "https://keycloak.example.com")
        monkeypatch.setitem(config._data, "oidc_client_id", "")
        monkeypatch.setitem(config._data, "oidc_client_secret", "test-secret")

        app = create_app()
        from fastapi.testclient import TestClient
        client = TestClient(app)

        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["oidc_configured"] is False
