"""Functional tests for the git_update router.

These tests use the FastAPI TestClient with mocked git operations.

Usage:
    pytest tests/test_router_git_update.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from spark_pulse.app import create_app


@pytest.fixture
def app_client():
    """Create a test FastAPI app and return a TestClient."""
    app = create_app()
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


# ── Test: GET /api/git-update/status ────────────────────────────────────────


class TestGitUpdateStatus:
    """Test the git status endpoint."""

    def test_status_returns_git_available_false(self, app_client, tmp_path):
        """When git is not available, status should reflect it."""
        with patch("spark_pulse.tools.git_update.is_git_available", return_value=False):
            resp = app_client.get("/api/git-update/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["git_available"] is False
        assert data["is_repo"] is False
        assert data["version_available"] is False

    def test_status_returns_up_to_date(self, app_client, tmp_path):
        """When repo is up to date, status should reflect it."""
        mock_status = {
            "git_available": True,
            "is_repo": True,
            "local_version": "abc1234",
            "version_available": False,
            "has_uncommitted_changes": False,
            "remote_version": "abc1234",
            "local_date": "2025-01-15T10:30:00+00:00",
            "remote_date": "2025-01-15T10:30:00+00:00",
        }

        with patch(
            "spark_pulse.routers.git_update.get_git_status", return_value=mock_status
        ):
            resp = app_client.get("/api/git-update/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["git_available"] is True
        assert data["is_repo"] is True
        assert data["version_available"] is False
        assert data["local_version"] == "abc1234"

    def test_status_returns_update_available(self, app_client, tmp_path):
        """When update is available, status should reflect it."""
        mock_status = {
            "git_available": True,
            "is_repo": True,
            "local_version": "abc1234",
            "version_available": True,
            "has_uncommitted_changes": False,
            "remote_version": "def5678",
        }

        with patch(
            "spark_pulse.routers.git_update.get_git_status", return_value=mock_status
        ):
            resp = app_client.get("/api/git-update/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["version_available"] is True
        assert data["remote_version"] == "def5678"


# ── Test: POST /api/git-update/check ────────────────────────────────────────


class TestGitUpdateCheck:
    """Test the manual check endpoint."""

    def test_check_returns_update_result(self, app_client, tmp_path):
        """Check should trigger a fetch + comparison."""
        mock_result = {
            "available": True,
            "local_version": "abc1234",
            "remote_version": "def5678",
            "local_date": "2025-01-15T10:30:00+00:00",
            "remote_date": "2025-01-16T08:00:00+00:00",
            "has_uncommitted_changes": False,
            "last_fetch_ok": True,
        }

        with patch(
            "spark_pulse.routers.git_update.check_updates", return_value=mock_result
        ):
            resp = app_client.post("/api/git-update/check")

        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["last_fetch_ok"] is True

    def test_check_handles_fetch_failure(self, app_client, tmp_path):
        """Check should handle fetch failure gracefully."""
        mock_result = {
            "available": False,
            "local_version": "abc1234",
            "remote_version": None,
            "local_date": "2025-01-15T10:30:00+00:00",
            "remote_date": None,
            "has_uncommitted_changes": False,
            "last_fetch_ok": False,
        }

        with patch(
            "spark_pulse.routers.git_update.check_updates", return_value=mock_result
        ):
            resp = app_client.post("/api/git-update/check")

        assert resp.status_code == 200
        data = resp.json()
        assert data["last_fetch_ok"] is False


# ── Test: POST /api/git-update/fetch ────────────────────────────────────────


class TestGitUpdateFetch:
    """Test the manual fetch endpoint."""

    def test_fetch_succeeds(self, app_client, tmp_path):
        """Successful fetch should return success=True."""
        with patch(
            "spark_pulse.routers.git_update.fetch",
            return_value={"success": True, "error": None},
        ):
            resp = app_client.post("/api/git-update/fetch")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_fetch_fails(self, app_client, tmp_path):
        """Failed fetch should return error."""
        with patch(
            "spark_pulse.routers.git_update.fetch",
            return_value={"success": False, "error": "fatal: remote error"},
        ):
            resp = app_client.post("/api/git-update/fetch")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "remote error" in data["error"]


# ── Test: POST /api/git-update/pull ─────────────────────────────────────────


class TestGitUpdatePull:
    """Test the manual pull endpoint."""

    def test_pull_succeeds(self, app_client, tmp_path):
        """Successful pull should return success=True."""
        with patch(
            "spark_pulse.routers.git_update.pull",
            return_value={"success": True, "error": None},
        ):
            resp = app_client.post("/api/git-update/pull")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_pull_fails_with_conflict(self, app_client, tmp_path):
        """Failed pull (merge conflict) should return error."""
        with patch(
            "spark_pulse.routers.git_update.pull",
            return_value={
                "success": False,
                "error": "CONFLICT: Merge conflict in README.md",
            },
        ):
            resp = app_client.post("/api/git-update/pull")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "CONFLICT" in data["error"]


# ── Test: Settings include git update fields ────────────────────────────────


class TestSettingsGitUpdateFields:
    """Test that settings API includes git update fields."""

    def test_settings_includes_git_update_enabled(self, app_client):
        """Settings should include git_update_enabled field."""
        resp = app_client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "git_update_enabled" in data
        assert isinstance(data["git_update_enabled"], bool)

    def test_settings_includes_git_update_check_interval(self, app_client):
        """Settings should include git_update_check_interval_seconds field."""
        resp = app_client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "git_update_check_interval_seconds" in data
        assert isinstance(data["git_update_check_interval_seconds"], int)

    def test_settings_includes_git_update_auto_pull(self, app_client):
        """Settings should include git_update_auto_pull field."""
        resp = app_client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "git_update_auto_pull" in data
        assert isinstance(data["git_update_auto_pull"], bool)

    def test_settings_updates_git_update_enabled(self, app_client):
        """Should be able to update git_update_enabled setting."""
        resp = app_client.put(
            "/api/settings",
            json={"git_update_enabled": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["git_update_enabled"] is False

    def test_settings_updates_git_update_check_interval(self, app_client):
        """Should be able to update git_update_check_interval_seconds setting."""
        resp = app_client.put(
            "/api/settings",
            json={"git_update_check_interval_seconds": 7200},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["git_update_check_interval_seconds"] == 7200

    def test_settings_updates_git_update_auto_pull(self, app_client):
        """Should be able to update git_update_auto_pull setting."""
        resp = app_client.put(
            "/api/settings",
            json={"git_update_auto_pull": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["git_update_auto_pull"] is True


# ── Test: Runtime config includes git_update_enabled ─────────────────────────


class TestRuntimeConfig:
    """Test the runtime config endpoint includes git_update_enabled."""

    def test_config_includes_git_update_enabled(self, app_client):
        """Config endpoint should include git_update_enabled."""
        resp = app_client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "git_update_enabled" in data
        assert isinstance(data["git_update_enabled"], bool)
