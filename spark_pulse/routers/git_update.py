"""Git update API endpoints.

Provides endpoints to check git status, trigger fetch/pull operations
for the spark-vllm-docker repository.
"""

from fastapi import APIRouter

from spark_pulse.config import config
from spark_pulse.tools.git_update import (
    check_updates,
    fetch,
    get_git_status,
    pull,
)

router = APIRouter(prefix="/api/git-update", tags=["git-update"])


@router.get("/status")
def git_update_status():
    """Return the current git status of the spark-vllm-docker repository."""
    return get_git_status(config.spark_vllm_path)


@router.post("/check")
def git_update_check():
    """Trigger a manual git update check.

    Fetches latest from remote and compares versions.
    """
    return check_updates(config.spark_vllm_path)


@router.post("/fetch")
def git_update_fetch():
    """Trigger a manual git fetch from the remote repository."""
    return fetch(config.spark_vllm_path)


@router.post("/pull")
def git_update_pull():
    """Trigger a manual git pull to apply remote changes."""
    return pull(config.spark_vllm_path)
