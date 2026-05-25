"""Git update operations for spark-vllm-docker repository.

Provides functions to check for updates, fetch, and pull changes
from the spark-vllm-docker git repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def _run_git(spark_path: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in the spark-vllm-docker directory."""
    return subprocess.run(
        ["git", *args],
        cwd=spark_path,
        capture_output=True,
        text=True,
        timeout=60,
    )


def is_git_available(spark_path: str) -> bool:
    """Check if git binary is installed and accessible."""
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def is_git_repo(spark_path: str) -> bool:
    """Check if the given path is a git repository."""
    if not Path(spark_path).is_dir():
        return False
    try:
        result = _run_git(spark_path, "rev-parse", "--is-inside-work-tree")
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.TimeoutExpired, OSError):
        return False


def get_local_version(spark_path: str) -> str | None:
    """Get the current local commit hash (short form).

    Returns None if the path is not a git repo.
    """
    if not is_git_repo(spark_path):
        return None
    try:
        result = _run_git(spark_path, "rev-parse", "--short=7", "HEAD")
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def get_remote_version(spark_path: str) -> str | None:
    """Get the remote HEAD commit hash (short form).

    Returns None if the remote is unreachable.
    """
    if not is_git_repo(spark_path):
        return None
    try:
        result = _run_git(spark_path, "rev-parse", "--short=7", "origin/HEAD")
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def get_commit_timestamp(spark_path: str, ref: str) -> str | None:
    """Get the timestamp of a commit in ISO format.

    Returns None if the ref is unknown.
    """
    try:
        result = _run_git(spark_path, "show", "-s", "--format=%cI", ref)
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def has_uncommitted_changes(spark_path: str) -> bool:
    """Check if there are uncommitted changes in the working directory."""
    if not is_git_repo(spark_path):
        return False
    try:
        result = _run_git(spark_path, "status", "--porcelain")
        return result.returncode == 0 and bool(result.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        return False


def check_updates(spark_path: str) -> dict[str, Any]:
    """Check for available updates by comparing local and remote versions.

    Performs a fetch first, then compares commit hashes.

    Returns:
        Dict with keys:
            - available: bool (whether new commits are available)
            - local_version: str | None (local commit hash)
            - remote_version: str | None (remote commit hash)
            - local_date: str | None (ISO timestamp of local commit)
            - remote_date: str | None (ISO timestamp of remote commit)
            - has_uncommitted_changes: bool
    """
    if not is_git_repo(spark_path):
        return {
            "available": False,
            "local_version": None,
            "remote_version": None,
            "local_date": None,
            "remote_date": None,
            "has_uncommitted_changes": False,
        }

    # Fetch latest from remote
    fetch_result = fetch(spark_path)

    local_version = get_local_version(spark_path)
    remote_version = get_remote_version(spark_path)

    available = False
    if local_version and remote_version and local_version != remote_version:
        available = True

    return {
        "available": available,
        "local_version": local_version,
        "remote_version": remote_version,
        "local_date": (
            get_commit_timestamp(spark_path, "HEAD") if local_version else None
        ),
        "remote_date": (
            get_commit_timestamp(spark_path, "origin/HEAD") if remote_version else None
        ),
        "has_uncommitted_changes": has_uncommitted_changes(spark_path),
        "last_fetch_ok": fetch_result.get("success", False),
    }


def fetch(spark_path: str) -> dict[str, Any]:
    """Run git fetch in the spark-vllm-docker directory.

    Returns:
        Dict with keys:
            - success: bool
            - error: str | None
    """
    if not is_git_repo(spark_path):
        return {"success": False, "error": "Not a git repository"}

    try:
        result = _run_git(spark_path, "fetch", "origin")
        if result.returncode == 0:
            return {"success": True, "error": None}
        return {"success": False, "error": result.stderr.strip()[:500]}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "git fetch timed out (60s)"}
    except OSError as e:
        return {"success": False, "error": str(e)}


def pull(spark_path: str) -> dict[str, Any]:
    """Run git pull in the spark-vllm-docker directory.

    Returns:
        Dict with keys:
            - success: bool
            - error: str | None
    """
    if not is_git_repo(spark_path):
        return {"success": False, "error": "Not a git repository"}

    try:
        result = _run_git(spark_path, "pull", "origin")
        if result.returncode == 0:
            return {"success": True, "error": None}
        return {"success": False, "error": result.stderr.strip()[:500]}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "git pull timed out (60s)"}
    except OSError as e:
        return {"success": False, "error": str(e)}


def get_git_status(spark_path: str) -> dict[str, Any]:
    """Get comprehensive git status for the spark-vllm-docker repository.

    Returns:
        Dict with keys:
            - git_available: bool (whether git binary is installed)
            - is_repo: bool (whether path is a git repo)
            - local_version: str | None
            - version_available: bool (whether updates are available)
            - has_uncommitted_changes: bool
            - remote_version: str | None
            - local_date: str | None
            - remote_date: str | None
    """
    git_available = is_git_available(spark_path)

    if not git_available:
        return {
            "git_available": False,
            "is_repo": False,
            "local_version": None,
            "version_available": False,
            "has_uncommitted_changes": False,
        }

    repo = is_git_repo(spark_path)

    if not repo:
        return {
            "git_available": True,
            "is_repo": False,
            "local_version": None,
            "version_available": False,
            "has_uncommitted_changes": False,
        }

    updates = check_updates(spark_path)

    return {
        "git_available": True,
        "is_repo": True,
        "local_version": updates["local_version"],
        "version_available": updates["available"],
        "has_uncommitted_changes": updates["has_uncommitted_changes"],
        "remote_version": updates["remote_version"],
        "local_date": updates["local_date"],
        "remote_date": updates["remote_date"],
    }
