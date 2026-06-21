"""Health monitoring API endpoints.

Provides endpoints for checking deployment and cluster health status.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from spark_pulse.tools import health

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("/deployment/{deployment_id}")
def check_deployment_health(deployment_id: str) -> dict[str, Any]:
    """Check health of a solo deployment.

    Returns DeploymentHealth as dict.
    """
    try:
        health.get_health_monitor()
        # For API endpoint, do a direct check rather than using the monitor
        from spark_pulse.tools import docker

        docker_service = docker.DockerService()
        # Look up deployment to get container name
        from spark_pulse.tools import deployments

        dep = deployments.get_deployment(deployment_id)
        if dep is None:
            return {
                "deployment_id": deployment_id,
                "container_status": "not_found",
                "error": "Deployment not found",
            }

        container_name = dep.get("container_name", "")
        container_status = "unknown"
        if container_name:
            status = docker_service.get_container_status(name=container_name)
            container_status = status.get("status", "unknown") if status else "unknown"

        return {
            "deployment_id": deployment_id,
            "container_status": container_status,
            "ray_status": "n/a",
            "process_status": "alive" if container_status == "running" else "dead",
        }
    except Exception as e:
        return {
            "deployment_id": deployment_id,
            "container_status": "error",
            "error": str(e),
        }


@router.get("/cluster/{cluster_name}")
def check_cluster_health(cluster_name: str) -> dict[str, Any]:
    """Check health of a cluster deployment.

    Returns ClusterHealth as dict.
    """
    try:
        from spark_pulse.tools import cluster as cluster_tool

        state = cluster_tool.get_cluster_status(name=cluster_name)
        if state is None:
            return {
                "cluster_name": cluster_name,
                "healthy": False,
                "errors": ["Cluster state not found"],
            }

        return {
            "cluster_name": cluster_name,
            "healthy": state.healthy,
            "head_status": state.head.status,
            "worker_statuses": [w.status for w in state.workers],
            "ray_ready": state.ray_ready,
            "warnings": [],
            "errors": (
                [
                    f"Worker {w.ip} is {w.status}"
                    for w in state.workers
                    if w.status != "running"
                ]
                if not state.healthy
                else []
            ),
        }
    except Exception as e:
        return {
            "cluster_name": cluster_name,
            "healthy": False,
            "errors": [str(e)],
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


@router.post("/monitor/track/cluster")
def track_cluster(body: dict[str, Any]) -> dict[str, Any]:
    """Track a cluster for health monitoring."""
    try:
        cluster_name = body.get("cluster_name", "")
        cluster_info = body.get("info", {})
        monitor = health.get_health_monitor()
        monitor.track_cluster(cluster_name, cluster_info)
        return {"tracked": True, "cluster_name": cluster_name}
    except Exception as e:
        return {"tracked": False, "error": str(e)}


@router.post("/monitor/untrack")
def untrack(body: dict[str, Any]) -> dict[str, Any]:
    """Untrack a deployment or cluster."""
    try:
        identifier = body.get("identifier", "")
        monitor = health.get_health_monitor()
        monitor.untrack(identifier)
        return {"untracked": True, "identifier": identifier}
    except Exception as e:
        return {"untracked": False, "error": str(e)}
