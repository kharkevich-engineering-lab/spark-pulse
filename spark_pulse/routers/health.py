"""Health monitoring API endpoints.

Provides endpoints for checking deployment health status. A cluster is a
deployment of size N, so it is checked here like any other deployment.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from spark_pulse.tools import health

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("/deployment/{deployment_id}")
def check_deployment_health(deployment_id: str) -> dict[str, Any]:
    """Check health of a deployment.

    Returns DeploymentHealth as dict.
    """
    try:
        health.get_health_monitor()
        # For API endpoint, do a direct check rather than using the monitor.
        # The module-level helper is the one the simulation switch replaces;
        # ``docker.DockerService`` is the real class in both packages (the mock
        # subclasses it), so constructing it here reached a real daemon even in
        # simulation.
        from spark_pulse.tools import docker

        # Look up deployment to get container name
        from spark_pulse import tools

        dep = tools.deployment_records.get(deployment_id)
        if dep is None:
            return {
                "deployment_id": deployment_id,
                "container_status": "not_found",
                "error": "Deployment not found",
            }

        container_name = dep.get("container_name", "")
        container_status = "unknown"
        if container_name:
            status = docker.get_container_status(container_name)
            container_status = status.get("status", "unknown") if status else "unknown"

        return {
            "deployment_id": deployment_id,
            "container_status": container_status,
            "process_status": "alive" if container_status == "running" else "dead",
        }
    except Exception as e:
        return {
            "deployment_id": deployment_id,
            "container_status": "error",
            "error": str(e),
        }


@router.post("/monitor/start")
def start_health_monitor() -> dict[str, Any]:
    """Start the background health monitor."""
    try:
        health.start_health_monitor()
        return {"started": True, "message": "Health monitor started"}
    except Exception as e:
        return {"started": False, "error": str(e)}


@router.post("/monitor/stop")
def stop_health_monitor() -> dict[str, Any]:
    """Stop the background health monitor."""
    try:
        health.stop_health_monitor()
        return {"stopped": True, "message": "Health monitor stopped"}
    except Exception as e:
        return {"stopped": False, "error": str(e)}


@router.post("/monitor/track/deployment")
def track_deployment(body: dict[str, Any]) -> dict[str, Any]:
    """Track a deployment for health monitoring."""
    try:
        deployment_id = body.get("deployment_id", "")
        deployment_info = body.get("info", {})
        monitor = health.get_health_monitor()
        monitor.track_deployment(deployment_id, deployment_info)
        return {"tracked": True, "deployment_id": deployment_id}
    except Exception as e:
        return {"tracked": False, "error": str(e)}


@router.post("/monitor/untrack")
def untrack(body: dict[str, Any]) -> dict[str, Any]:
    """Untrack a deployment."""
    try:
        identifier = body.get("identifier", "")
        monitor = health.get_health_monitor()
        monitor.untrack(identifier)
        return {"untracked": True, "identifier": identifier}
    except Exception as e:
        return {"untracked": False, "error": str(e)}
