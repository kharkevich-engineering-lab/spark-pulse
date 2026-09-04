"""Unit tests for the /api/config endpoint."""

from __future__ import annotations

from spark_pulse.app import create_app
from spark_pulse.config import config


class TestApiConfigEndpoint:
    """Tests for the /api/config endpoint."""

    def test_config_no_auth_fields(self, monkeypatch):
        """Config should include auth_enabled so frontend can distinguish disabled vs not-authenticated."""
        monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")

        app = create_app()
        from fastapi.testclient import TestClient

        client = TestClient(app)

        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "auth_enabled" in data
        # oidc_configured is not exposed — frontend uses auth_enabled + 401 handling
        assert "oidc_configured" not in data

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

    def test_config_reports_cluster_as_experimental_by_default(self):
        """Multi-node has never run on hardware, so the UI must say so.

        The flag ships true and is meant to be flipped once a two-node
        bring-up has actually been verified.
        """
        app = create_app()
        from fastapi.testclient import TestClient

        client = TestClient(app)

        data = client.get("/api/config").json()
        assert data["cluster_experimental"] is True

    def test_cluster_experimental_can_be_turned_off(self, monkeypatch):
        monkeypatch.setitem(config._data, "cluster_experimental", False)

        app = create_app()
        from fastapi.testclient import TestClient

        client = TestClient(app)

        assert client.get("/api/config").json()["cluster_experimental"] is False
