"""Cluster orchestration REST API router.

Provides endpoints for multi-node cluster lifecycle management:
start, stop, status, validate, and rollback.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from spark_pulse.tools.cluster import (
    ClusterOrchestrator,
    ModDeployment,
)
from spark_pulse.tools.cluster_health import ValidationResult
from spark_pulse.tools.remote_docker import RemoteDockerService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cluster", tags=["cluster"])

# Module-level orchestrator instance (created on first use)
_orchestrator: ClusterOrchestrator | None = None


def _get_orchestrator() -> ClusterOrchestrator:
    """Get or create the default cluster orchestrator."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = ClusterOrchestrator()
    return _orchestrator


# ── Cluster lifecycle endpoints ──────────────────────────────────────────────


@router.post("/start")
def start_cluster(body: dict[str, Any]):
    """Start a multi-node cluster.

    Request body:
        name: str — Cluster name
        image: str — Docker image
        head_ip: str — Head node IP
        worker_ips: list[str] — Worker node IPs
        env: dict — Environment variables
        docker_config: dict — Docker configuration
        mod_deployments: list[dict] — Mods to deploy (optional)
        no_ray: bool — Skip Ray startup (optional)
    """
    try:
        orchestrator = _get_orchestrator()

        name = body.get("name", "")
        image = body.get("image", "")
        head_ip = body.get("head_ip", "")
        worker_ips = body.get("worker_ips", [])
        env_vars = body.get("env", {})
        docker_config = body.get("docker_config", {})
        mod_deployments = body.get("mod_deployments")
        no_ray = body.get("no_ray", False)

        # Parse mod deployments
        mods = None
        if mod_deployments:
            mods = [
                ModDeployment(path=m["path"], target=m["target"])
                for m in mod_deployments
            ]

        state = orchestrator.start_cluster(
            name=name,
            image=image,
            head_ip=head_ip,
            worker_ips=worker_ips,
            env_vars=env_vars,
            docker_config=docker_config,
            mod_deployments=mods,
            no_ray=no_ray,
        )

        return {
            "name": state.name,
            "head": {
                "ip": state.head.ip,
                "container": state.head.container_name,
                "status": state.head.status,
                "ray_ready": state.head.ray_ready,
                "gpu_count": state.head.gpu_count,
            },
            "workers": [
                {
                    "ip": w.ip,
                    "container": w.container_name,
                    "status": w.status,
                    "ray_ready": w.ray_ready,
                    "gpu_count": w.gpu_count,
                }
                for w in state.workers
            ],
            "ray_enabled": state.ray_enabled,
            "ray_ready": state.ray_ready,
            "total_nodes": state.total_nodes,
            "healthy": state.healthy,
        }

    except Exception as e:
        logger.error("Failed to start cluster: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stop")
def stop_cluster(body: dict[str, Any]):
    """Stop a cluster.

    Request body:
        name: str — Cluster name
    """
    try:
        orchestrator = _get_orchestrator()
        name = body.get("name", "")
        orchestrator.stop_cluster(name)
        return {"name": name, "status": "stopped"}

    except Exception as e:
        logger.error("Failed to stop cluster: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
def get_cluster_status(name: str):
    """Get cluster status.

    Query parameter:
        name: str — Cluster name
    """
    try:
        orchestrator = _get_orchestrator()
        state = orchestrator.get_cluster_status(name)

        return {
            "name": state.name,
            "head": {
                "ip": state.head.ip,
                "container": state.head.container_name,
                "status": state.head.status,
                "ray_ready": state.head.ray_ready,
                "gpu_count": state.head.gpu_count,
            },
            "workers": [
                {
                    "ip": w.ip,
                    "container": w.container_name,
                    "status": w.status,
                    "ray_ready": w.ray_ready,
                    "gpu_count": w.gpu_count,
                }
                for w in state.workers
            ],
            "ray_enabled": state.ray_enabled,
            "ray_ready": state.ray_ready,
            "total_nodes": state.total_nodes,
            "total_gpus": state.total_gpus,
            "healthy": state.healthy,
        }

    except Exception as e:
        logger.error("Failed to get cluster status: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/validate")
def validate_cluster(body: dict[str, Any]):
    """Validate cluster health.

    Request body:
        name: str — Cluster name
    """
    try:
        orchestrator = _get_orchestrator()
        name = body.get("name", "")
        state = orchestrator.get_cluster_status(name)

        validation = orchestrator._docker is not None and hasattr(
            orchestrator._docker,
            "_local",
        ) and not (
            state is not None
        )

        # Use the remote_docker from orchestrator
        from spark_pulse.tools.cluster_health import validate_cluster as _validate
        validation = _validate(state, orchestrator._docker)

        return {
            "healthy": validation.healthy,
            "warnings": validation.warnings,
            "errors": validation.errors,
        }

    except Exception as e:
        logger.error("Failed to validate cluster: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rollback")
def rollback_cluster(body: dict[str, Any]):
    """Rollback a failed cluster deployment.

    Request body:
        name: str — Cluster name
        head_ip: str — Head node IP
        worker_ips: list[str] — Worker node IPs
    """
    try:
        orchestrator = _get_orchestrator()
        name = body.get("name", "")
        head_ip = body.get("head_ip", "")
        worker_ips = body.get("worker_ips", [])

        orchestrator.rollback_cluster(name, head_ip, worker_ips)
        return {"name": name, "status": "rolled_back"}

    except Exception as e:
        logger.error("Failed to rollback cluster: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list")
def list_clusters():
    """List all known clusters by scanning Docker labels.
    
    Returns a list of all cluster states found via container labels.
    """
    try:
        orchestrator = _get_orchestrator()
        # List all containers with cluster labels
        all_containers = orchestrator._docker.list_managed_containers(
            "", {"spark-pulse.cluster": ""}
        )
        
        # Group by cluster name
        clusters: dict[str, list] = {}
        for container in all_containers:
            labels = container.labels or {}
            cluster_name = labels.get("spark-pulse.cluster", "")
            if not cluster_name:
                continue
            if cluster_name not in clusters:
                clusters[cluster_name] = []
            clusters[cluster_name].append({
                "name": container.name,
                "role": labels.get("spark-pulse.role", "unknown"),
                "status": container.status or "stopped",
                "ip": labels.get("spark-pulse.head_ip", ""),
            })
        
        result = []
        for name, nodes in clusters.items():
            head = next((n for n in nodes if n["role"] == "head"), None)
            workers = [n for n in nodes if n["role"] == "worker"]
            if head:
                result.append({
                    "name": name,
                    "head": {
                        "ip": head.get("ip", ""),
                        "container": head["name"],
                        "status": "running" if "running" in head["status"] else "stopped",
                        "ray_ready": False,
                        "gpu_count": 0,
                    },
                    "workers": [{
                        "ip": w.get("ip", ""),
                        "container": w["name"],
                        "status": "running" if "running" in w["status"] else "stopped",
                        "ray_ready": False,
                        "gpu_count": 0,
                    } for w in workers],
                    "ray_enabled": True,
                    "ray_ready": False,
                    "total_nodes": 1 + len(workers),
                    "healthy": all("running" in n["status"] for n in nodes),
                })
        
        return result

    except Exception as e:
        logger.error("Failed to list clusters: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
