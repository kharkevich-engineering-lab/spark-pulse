"""Application configuration endpoint for SPA runtime config.

Serves the current app configuration to the frontend so it can
conditionally render UI elements (login button, features, etc.)
without needing separate builds.

See: https://kharkevich.org/2024/12/20/spa-runtime-config/
"""

from __future__ import annotations

from fastapi import APIRouter

from spark_pulse.config import config

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
def get_config():
    """Return the current application configuration to the frontend."""
    return {
        "auth_enabled": config.auth_enabled,
        "mcp_enabled": config.mcp_enabled,
        "cluster_enabled": config.cluster_enabled,
        "git_update_enabled": config.git_update_enabled,
        "benchmarking_enabled": config.benchmarking_enabled,
        "simulation_mode": True,  # TODO: derive from spark_pulse.tools.is_simulation()
    }
