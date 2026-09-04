"""Application configuration endpoint for SPA runtime config.

Serves the current app configuration to the frontend so it can
conditionally render UI elements (login button, features, etc.)
without needing separate builds.

See: https://kharkevich.org/2024/12/20/spa-runtime-config/
"""

from __future__ import annotations

from fastapi import APIRouter

from spark_pulse.config import config
from spark_pulse.tools import is_simulation

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
def get_config():
    """Return the current application configuration to the frontend."""
    return {
        "auth_enabled": config.auth_enabled,
        "mcp_enabled": config.mcp_enabled,
        "cluster_enabled": config.cluster_enabled,
        "cluster_experimental": config.cluster_experimental,
        "runtime": config.runtime,
        "benchmarking_enabled": config.benchmarking_enabled,
        "simulation_mode": is_simulation(),
    }
