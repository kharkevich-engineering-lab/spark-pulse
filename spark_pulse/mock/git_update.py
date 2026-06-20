"""Mock git update tools — spark-vllm-docker repository simulation.

Returns deterministic results without running git commands.
Mirrors the real git_update.py API exactly.
"""

from __future__ import annotations

from typing import Any

# Simulated git state
_GIT_STATE: dict[str, Any] = {
    "git_available": True,
    "is_repo": True,
    "local_version": "a1b2c3d",
    "remote_version": "e4f5g6h",
    "local_date": "2026-06-18T10:30:00+00:00",
    "remote_date": "2026-06-19T08:15:00+00:00",
    "has_uncommitted_changes": False,
    "updates_available": True,
}


def is_git_available(spark_path: str) -> bool:
    """Check if git binary is installed and accessible (simulated)."""
    return _GIT_STATE["git_available"]


def is_git_repo(spark_path: str) -> bool:
    """Check if the given path is a git repository (simulated)."""
    return _GIT_STATE["is_repo"]


def get_local_version(spark_path: str) -> str | None:
    """Get the current local commit hash (simulated)."""
    return _GIT_STATE["local_version"]


def get_remote_version(spark_path: str) -> str | None:
    """Get the remote HEAD commit hash (simulated)."""
    return _GIT_STATE["remote_version"]


def get_commit_timestamp(spark_path: str, ref: str) -> str | None:
    """Get the timestamp of a commit in ISO format (simulated)."""
    if ref == "HEAD":
        return _GIT_STATE["local_date"]
    if ref == "origin/HEAD":
        return _GIT_STATE["remote_date"]
    return None


def has_uncommitted_changes(spark_path: str) -> bool:
    """Check if there are uncommitted changes (simulated)."""
    return _GIT_STATE["has_uncommitted_changes"]


def check_updates(spark_path: str) -> dict[str, Any]:
    """Check for available updates (simulated).

    Returns dict with update status.
    """
    return {
        "available": _GIT_STATE["updates_available"],
        "local_version": _GIT_STATE["local_version"],
        "remote_version": _GIT_STATE["remote_version"],
        "local_date": _GIT_STATE["local_date"],
        "remote_date": _GIT_STATE["remote_date"],
        "has_uncommitted_changes": _GIT_STATE["has_uncommitted_changes"],
        "last_fetch_ok": True,
    }


def fetch(spark_path: str) -> dict[str, Any]:
    """Simulate git fetch (always succeeds)."""
    return {"success": True, "error": None}


def pull(spark_path: str) -> dict[str, Any]:
    """Simulate git pull (always succeeds)."""
    return {"success": True, "error": None}


def get_git_status(spark_path: str) -> dict[str, Any]:
    """Get comprehensive git status (simulated)."""
    updates = check_updates(spark_path)
    return {
        "git_available": _GIT_STATE["git_available"],
        "is_repo": _GIT_STATE["is_repo"],
        "local_version": _GIT_STATE["local_version"],
        "version_available": updates["available"],
        "has_uncommitted_changes": _GIT_STATE["has_uncommitted_changes"],
        "remote_version": _GIT_STATE["remote_version"],
        "local_date": _GIT_STATE["local_date"],
        "remote_date": _GIT_STATE["remote_date"],
    }
