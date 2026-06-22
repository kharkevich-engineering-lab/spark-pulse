"""E2E tests for git update feature using Playwright.

These tests verify the frontend integration of the git update notification
system. They require a running spark-pulse server and a browser.

Prerequisites:
    1. Install playwright browsers: playwright install chromium
    2. Start the dev server: ./scripts/run-backend.sh
    3. Run: pytest tests/test_e2e_git_update.py -v

Usage:
    pytest tests/test_e2e_git_update.py -v
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
import httpx

from spark_pulse.app import create_app

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def git_repo_path(tmp_path_factory):
    """Create a fake git repo for testing."""
    repo_dir = tmp_path_factory.mktemp("git-repo")
    git_dir = repo_dir / ".git"
    git_dir.mkdir()

    # Create a minimal git repo structure
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    refs_dir = git_dir / "refs" / "heads"
    refs_dir.mkdir(parents=True)
    (refs_dir / "main").write_text("abc1234def5678\n")

    origin_dir = git_dir / "refs" / "remotes" / "origin"
    origin_dir.mkdir(parents=True)
    (origin_dir / "HEAD").write_text("ref: refs/remotes/origin/main\n")
    (origin_dir / "main").write_text("def5678abc1234\n")

    return str(repo_dir)


@pytest.fixture(scope="module")
def mock_git_available():
    """Mock git as available."""
    with patch("spark_pulse.tools.git_update.is_git_available", return_value=True):
        yield


@pytest.fixture(scope="module")
def mock_git_repo(git_repo_path):
    """Mock the git repo check to return the test repo."""
    with patch("spark_pulse.tools.git_update.is_git_repo") as mock:

        def side_effect(path):
            return path == git_repo_path

        mock.side_effect = side_effect
        yield git_repo_path


@pytest.fixture(scope="module")
def mock_git_status(mock_git_repo):
    """Mock git status to return update-available state."""
    mock_status = {
        "git_available": True,
        "is_repo": True,
        "local_version": "abc1234",
        "version_available": True,
        "has_uncommitted_changes": False,
        "remote_version": "def5678",
        "local_date": "2025-01-15T10:30:00+00:00",
        "remote_date": "2025-01-16T08:00:00+00:00",
    }
    with patch(
        "spark_pulse.routers.git_update.get_git_status",
        return_value=mock_status,
    ):
        yield mock_status


@pytest.fixture(scope="module")
def mock_fetch_success():
    """Mock fetch to succeed."""
    with patch(
        "spark_pulse.routers.git_update.fetch",
        return_value={"success": True},
    ):
        yield


@pytest.fixture(scope="module")
def mock_pull_success():
    """Mock pull to succeed."""
    with patch(
        "spark_pulse.routers.git_update.pull",
        return_value={"success": True},
    ):
        yield


@pytest.fixture(scope="module")
def e2e_config(mock_git_available, mock_git_repo, mock_git_status):
    """Configure the app for e2e tests with a mock git repo."""
    from spark_pulse.config import config

    os.environ["SPARK_PULSE_AUTH_ENABLED"] = "false"
    config._data["spark_vllm_path"] = mock_git_repo
    config._data["git_update_enabled"] = True
    config._data["git_update_check_interval_seconds"] = 3600
    config._data["git_update_auto_pull"] = False

    return config


@pytest.fixture(scope="module")
def e2e_app(e2e_config):
    """Create test app for e2e tests."""
    return create_app()


@pytest.fixture(scope="module")
def e2e_server(e2e_app):
    """Run a test server for e2e tests."""
    import socket
    import threading
    import time

    from uvicorn import Config, Server

    # Use a random port in the ephemeral range since Config.port is not
    # updated by uvicorn >=0.47 after binding (stays at the initial value).
    # We bind a socket ourselves to get the actual port, then pass it via
    # the `fd` parameter workaround by choosing a free port explicitly.
    port = 9499
    for _attempt in range(50):
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.bind(("127.0.0.1", port))
            test_sock.close()
            break
        except OSError:
            port += 1
    else:
        raise RuntimeError("Could not find an available port for test server")

    config = Config(app=e2e_app, host="127.0.0.1", port=port, log_level="error")
    server = Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to be ready
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            httpx.get(f"{base_url}/health", timeout=1)
            break
        except (httpx.ConnectError, httpx.ReadTimeout):
            time.sleep(0.2)

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


# ── Tests ────────────────────────────────────────────────────────────────────


class TestGitUpdateApiE2E:
    """E2E tests for git update API endpoints."""

    def test_git_status_api_endpoint(self, e2e_server, mock_git_status):
        """Test that the git status API returns the correct mock data."""
        resp = httpx.get(f"{e2e_server}/api/git-update/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["git_available"] is True
        assert data["is_repo"] is True
        assert data["version_available"] is True

    def test_git_fetch_api_endpoint(self, e2e_server):
        """Test that the git fetch API endpoint is accessible."""
        resp = httpx.post(f"{e2e_server}/api/git-update/fetch")
        assert resp.status_code == 200
        data = resp.json()
        assert "success" in data

    def test_git_pull_api_endpoint(self, e2e_server):
        """Test that the git pull API endpoint is accessible."""
        resp = httpx.post(f"{e2e_server}/api/git-update/pull")
        assert resp.status_code == 200
        data = resp.json()
        assert "success" in data

    def test_git_check_api_endpoint(self, e2e_server):
        """Test that the git check API endpoint is accessible."""
        resp = httpx.post(f"{e2e_server}/api/git-update/check")
        assert resp.status_code == 200
        data = resp.json()
        assert "available" in data

    def test_settings_api_includes_git_fields(self, e2e_server):
        """Test that settings API includes git update fields."""
        resp = httpx.get(f"{e2e_server}/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "git_update_enabled" in data
        assert "git_update_check_interval_seconds" in data
        assert "git_update_auto_pull" in data

    def test_config_api_includes_git_field(self, e2e_server):
        """Test that config API includes git_update_enabled."""
        resp = httpx.get(f"{e2e_server}/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "git_update_enabled" in data
        assert data["git_update_enabled"] is True


class TestGitUpdateSettingsUpdate:
    """E2E tests for updating git update settings."""

    def test_update_git_update_enabled(self, e2e_server):
        """Test updating git_update_enabled to false."""
        resp = httpx.put(
            f"{e2e_server}/api/settings",
            json={"git_update_enabled": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["git_update_enabled"] is False

    def test_update_check_interval(self, e2e_server):
        """Test updating check interval to 7200 seconds."""
        resp = httpx.put(
            f"{e2e_server}/api/settings",
            json={"git_update_check_interval_seconds": 7200},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["git_update_check_interval_seconds"] == 7200

    def test_update_auto_pull(self, e2e_server):
        """Test updating auto_pull to true."""
        resp = httpx.put(
            f"{e2e_server}/api/settings",
            json={"git_update_auto_pull": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["git_update_auto_pull"] is True
