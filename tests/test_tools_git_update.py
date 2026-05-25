"""Unit tests for the git_update tool module."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spark_pulse.tools.git_update import (
    check_updates,
    fetch,
    get_commit_timestamp,
    get_git_status,
    get_local_version,
    get_remote_version,
    has_uncommitted_changes,
    is_git_available,
    is_git_repo,
    pull,
)


# ── Test: is_git_available ───────────────────────────────────────────────────


class TestIsGitAvailable:
    """Test git binary detection."""

    def test_git_installed(self):
        """When git is installed, should return True."""
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="git version 2.40.0"),
        ):
            assert is_git_available("/tmp/test") is True

    def test_git_not_installed(self):
        """When git is not installed, should return False."""
        with patch(
            "subprocess.run", side_effect=FileNotFoundError("git not found")
        ):
            assert is_git_available("/tmp/test") is False

    def test_git_timeout(self):
        """When git command times out, should return False."""
        with patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)
        ):
            assert is_git_available("/tmp/test") is False

    def test_git_os_error(self):
        """When OSError occurs, should return False."""
        with patch("subprocess.run", side_effect=OSError("Permission denied")):
            assert is_git_available("/tmp/test") is False


# ── Test: is_git_repo ────────────────────────────────────────────────────────


class TestIsGitRepo:
    """Test git repository detection."""

    def test_is_git_repo(self, tmp_path):
        """When path is a git repo, should return True."""
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        with patch("spark_pulse.tools.git_update._run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="true"
            )
            assert is_git_repo(str(repo_dir)) is True
            mock_run.assert_called_once_with(
                str(repo_dir), "rev-parse", "--is-inside-work-tree"
            )

    def test_not_a_git_repo(self, tmp_path):
        """When path is not a git repo, should return False."""
        with patch("spark_pulse.tools.git_update._run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="false"
            )
            assert is_git_repo(str(tmp_path)) is False

    def test_nonexistent_path(self, tmp_path):
        """When path doesn't exist, should return False."""
        bad_path = tmp_path / "does-not-exist"
        assert is_git_repo(str(bad_path)) is False

    def test_timeout_returns_false(self, tmp_path):
        """When git command times out, should return False."""
        with patch("spark_pulse.tools.git_update._run_git") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("git", 60)
            assert is_git_repo(str(tmp_path)) is False


# ── Test: get_local_version ──────────────────────────────────────────────────


class TestGetLocalVersion:
    """Test local commit hash retrieval."""

    def test_returns_short_hash(self, tmp_path):
        """Should return short commit hash when repo is valid."""
        with patch("spark_pulse.tools.git_update.is_git_repo", return_value=True):
            with patch(
                "spark_pulse.tools.git_update._run_git",
                return_value=MagicMock(returncode=0, stdout="abc1234\n"),
            ):
                result = get_local_version(str(tmp_path))
                assert result == "abc1234"

    def test_returns_none_for_non_repo(self):
        """Should return None when not a git repo."""
        assert get_local_version("/nonexistent") is None


# ── Test: get_remote_version ─────────────────────────────────────────────────


class TestGetRemoteVersion:
    """Test remote commit hash retrieval."""

    def test_returns_remote_hash(self, tmp_path):
        """Should return remote commit hash when reachable."""
        with patch("spark_pulse.tools.git_update.is_git_repo", return_value=True):
            with patch(
                "spark_pulse.tools.git_update._run_git",
                return_value=MagicMock(returncode=0, stdout="def5678\n"),
            ):
                result = get_remote_version(str(tmp_path))
                assert result == "def5678"

    def test_returns_none_on_timeout(self, tmp_path):
        """Should return None when remote is unreachable."""
        with patch("spark_pulse.tools.git_update.is_git_repo", return_value=True):
            with patch(
                "spark_pulse.tools.git_update._run_git",
                side_effect=subprocess.TimeoutExpired("git", 60),
            ):
                assert get_remote_version(str(tmp_path)) is None


# ── Test: get_commit_timestamp ───────────────────────────────────────────────


class TestGetCommitTimestamp:
    """Test commit timestamp retrieval."""

    def test_returns_iso_timestamp(self, tmp_path):
        """Should return ISO format timestamp."""
        with patch(
            "spark_pulse.tools.git_update._run_git",
            return_value=MagicMock(returncode=0, stdout="2025-01-15T10:30:00+00:00\n"),
        ):
            result = get_commit_timestamp(str(tmp_path), "HEAD")
            assert result == "2025-01-15T10:30:00+00:00"

    def test_returns_none_on_failure(self, tmp_path):
        """Should return None when ref is unknown."""
        with patch(
            "spark_pulse.tools.git_update._run_git",
            return_value=MagicMock(returncode=128, stdout=""),
        ):
            assert get_commit_timestamp(str(tmp_path), "HEAD") is None


# ── Test: has_uncommitted_changes ────────────────────────────────────────────


class TestHasUncommittedChanges:
    """Test uncommitted change detection."""

    def test_has_changes(self, tmp_path):
        """Should return True when there are uncommitted changes."""
        with patch("spark_pulse.tools.git_update.is_git_repo", return_value=True):
            with patch(
                "spark_pulse.tools.git_update._run_git",
                return_value=MagicMock(returncode=0, stdout="M README.md\n"),
            ):
                assert has_uncommitted_changes(str(tmp_path)) is True

    def test_no_changes(self, tmp_path):
        """Should return False when working tree is clean."""
        with patch("spark_pulse.tools.git_update.is_git_repo", return_value=True):
            with patch(
                "spark_pulse.tools.git_update._run_git",
                return_value=MagicMock(returncode=0, stdout=""),
            ):
                assert has_uncommitted_changes(str(tmp_path)) is False


# ── Test: fetch ──────────────────────────────────────────────────────────────


class TestFetch:
    """Test git fetch operation."""

    def test_successful_fetch(self, tmp_path):
        """Should return success=True on successful fetch."""
        with patch("spark_pulse.tools.git_update.is_git_repo", return_value=True):
            with patch(
                "spark_pulse.tools.git_update._run_git",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ):
                result = fetch(str(tmp_path))
                assert result["success"] is True
                assert result["error"] is None

    def test_fetch_failure(self, tmp_path):
        """Should return error message on failed fetch."""
        with patch("spark_pulse.tools.git_update.is_git_repo", return_value=True):
            with patch(
                "spark_pulse.tools.git_update._run_git",
                return_value=MagicMock(
                    returncode=1, stderr="fatal: couldn't find remote ref"
                ),
            ):
                result = fetch(str(tmp_path))
                assert result["success"] is False
                assert "remote ref" in result["error"]

    def test_not_a_repo(self, tmp_path):
        """Should return error when not a git repo."""
        with patch("spark_pulse.tools.git_update.is_git_repo", return_value=False):
            result = fetch(str(tmp_path))
            assert result["success"] is False
            assert "Not a git repository" in result["error"]


# ── Test: pull ───────────────────────────────────────────────────────────────


class TestPull:
    """Test git pull operation."""

    def test_successful_pull(self, tmp_path):
        """Should return success=True on successful pull."""
        with patch("spark_pulse.tools.git_update.is_git_repo", return_value=True):
            with patch(
                "spark_pulse.tools.git_update._run_git",
                return_value=MagicMock(returncode=0, stdout="Already up to date.", stderr=""),
            ):
                result = pull(str(tmp_path))
                assert result["success"] is True
                assert result["error"] is None

    def test_pull_failure(self, tmp_path):
        """Should return error message on failed pull."""
        with patch("spark_pulse.tools.git_update.is_git_repo", return_value=True):
            with patch(
                "spark_pulse.tools.git_update._run_git",
                return_value=MagicMock(
                    returncode=1,
                    stderr="CONFLICT (content): Merge conflict in README.md",
                ),
            ):
                result = pull(str(tmp_path))
                assert result["success"] is False
                assert "CONFLICT" in result["error"]


# ── Test: check_updates ──────────────────────────────────────────────────────


class TestCheckUpdates:
    """Test update detection logic."""

    def test_up_to_date(self, tmp_path):
        """Should return available=False when versions match."""
        with patch("spark_pulse.tools.git_update.is_git_repo", return_value=True):
            with patch(
                "spark_pulse.tools.git_update.fetch",
                return_value={"success": True},
            ):
                with patch(
                    "spark_pulse.tools.git_update.get_local_version",
                    return_value="abc1234",
                ):
                    with patch(
                        "spark_pulse.tools.git_update.get_remote_version",
                        return_value="abc1234",
                    ):
                        with patch(
                            "spark_pulse.tools.git_update.has_uncommitted_changes",
                            return_value=False,
                        ):
                            result = check_updates(str(tmp_path))
                            assert result["available"] is False
                            assert result["local_version"] == "abc1234"
                            assert result["remote_version"] == "abc1234"

    def test_update_available(self, tmp_path):
        """Should return available=True when remote has newer commits."""
        with patch("spark_pulse.tools.git_update.is_git_repo", return_value=True):
            with patch(
                "spark_pulse.tools.git_update.fetch",
                return_value={"success": True},
            ):
                with patch(
                    "spark_pulse.tools.git_update.get_local_version",
                    return_value="abc1234",
                ):
                    with patch(
                        "spark_pulse.tools.git_update.get_remote_version",
                        return_value="def5678",
                    ):
                        with patch(
                            "spark_pulse.tools.git_update.has_uncommitted_changes",
                            return_value=False,
                        ):
                            result = check_updates(str(tmp_path))
                            assert result["available"] is True
                            assert result["local_version"] == "abc1234"
                            assert result["remote_version"] == "def5678"

    def test_not_a_repo(self, tmp_path):
        """Should return safe defaults when not a git repo."""
        with patch("spark_pulse.tools.git_update.is_git_repo", return_value=False):
            result = check_updates(str(tmp_path))
            assert result["available"] is False
            assert result["local_version"] is None
            assert result["remote_version"] is None


# ── Test: get_git_status ────────────────────────────────────────────────────


class TestGetGitStatus:
    """Test comprehensive git status."""

    def test_git_not_available(self, tmp_path):
        """Should return safe defaults when git is not installed."""
        with patch("spark_pulse.tools.git_update.is_git_available", return_value=False):
            result = get_git_status(str(tmp_path))
            assert result["git_available"] is False
            assert result["is_repo"] is False
            assert result["local_version"] is None
            assert result["version_available"] is False

    def test_git_available_not_repo(self, tmp_path):
        """Should report git available but not a repo."""
        with patch("spark_pulse.tools.git_update.is_git_available", return_value=True):
            with patch("spark_pulse.tools.git_update.is_git_repo", return_value=False):
                result = get_git_status(str(tmp_path))
                assert result["git_available"] is True
                assert result["is_repo"] is False
                assert result["local_version"] is None
                assert result["version_available"] is False

    def test_git_available_is_repo_up_to_date(self, tmp_path):
        """Should report repo status when up to date."""
        with patch("spark_pulse.tools.git_update.is_git_available", return_value=True):
            with patch("spark_pulse.tools.git_update.is_git_repo", return_value=True):
                with patch(
                    "spark_pulse.tools.git_update.check_updates",
                    return_value={
                        "available": False,
                        "local_version": "abc1234",
                        "remote_version": "abc1234",
                        "local_date": "2025-01-15T10:30:00+00:00",
                        "remote_date": "2025-01-15T10:30:00+00:00",
                        "has_uncommitted_changes": False,
                    },
                ):
                    result = get_git_status(str(tmp_path))
                    assert result["git_available"] is True
                    assert result["is_repo"] is True
                    assert result["local_version"] == "abc1234"
                    assert result["version_available"] is False
                    assert result["has_uncommitted_changes"] is False

    def test_git_available_is_repo_update_available(self, tmp_path):
        """Should report update available when remote has newer commits."""
        with patch("spark_pulse.tools.git_update.is_git_available", return_value=True):
            with patch("spark_pulse.tools.git_update.is_git_repo", return_value=True):
                with patch(
                    "spark_pulse.tools.git_update.check_updates",
                    return_value={
                        "available": True,
                        "local_version": "abc1234",
                        "remote_version": "def5678",
                        "local_date": "2025-01-15T10:30:00+00:00",
                        "remote_date": "2025-01-16T08:00:00+00:00",
                        "has_uncommitted_changes": True,
                    },
                ):
                    result = get_git_status(str(tmp_path))
                    assert result["version_available"] is True
                    assert result["has_uncommitted_changes"] is True
                    assert result["remote_version"] == "def5678"
