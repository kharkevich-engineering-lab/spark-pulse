"""Docker container lifecycle REST API router.

Provides deployment-centric endpoints for managing Docker containers
that power vLLM inference deployments.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from spark_pulse.tools import is_simulation
from spark_pulse.tools.docker import (
    ContainerInfo,
    ContainerMetadata,
    DockerService,
    get_container_by_deployment,
    get_container_status,
    list_managed_containers,
    run_container,
    stop_container,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/docker", tags=["docker"])


# ── Deployment endpoints ─────────────────────────────────────────────────────


@router.post("/deployments/{name}/run")
def run_deployment(
    name: str,
    body: dict[str, Any],
):
    """Run a deployment container.

    Request body:
        image: str — Docker image to use
        recipe: str — Recipe ID
        env: dict — Environment variables
        privileged: bool — Run in privileged mode (optional)
        memory_limit_gb: float — Memory limit in GB (optional)
        shm_size_gb: float — SHM size in GB (optional)
        cache_dirs: list[str] — Cache directories to mount (optional)
        port_mappings: list[str] — Port mappings (optional)
    """
    try:
        image = body.get("image", "")
        recipe = body.get("recipe", "")
        env_vars = body.get("env", {})
        privileged = body.get("privileged", config.docker_privileged)
        memory_limit_gb = body.get("memory_limit_gb", config.docker_memory_limit_gb)
        shm_size_gb = body.get("shm_size_gb", config.docker_shm_size_gb)
        cache_dirs = body.get("cache_dirs", config.docker_cache_dirs)
        port_mappings = body.get("port_mappings")

        if not image or not recipe:
            raise HTTPException(status_code=400, detail="image and recipe are required")

        metadata = ContainerMetadata(
            deployment=name,
            recipe=recipe,
            image=image,
            mode="solo",
            memory_limit_gb=memory_limit_gb,
            shm_size_gb=shm_size_gb,
            privileged=privileged,
        )

        result = run_container(
            image=image,
            name=name,
            env_vars=env_vars,
            metadata=metadata,
            privileged=privileged,
            memory_limit_gb=memory_limit_gb,
            shm_size_gb=shm_size_gb,
            cache_dirs=cache_dirs,
            port_mappings=port_mappings,
        )

        return {
            "status": "success",
            "container": {
                "id": result.id,
                "name": result.name,
                "status": result.status,
                "image": result.image,
            },
        }
    except HTTPException:
        raise
    except RuntimeError as exc:
        logger.error("Failed to run container: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        logger.error("Unexpected error running container: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/deployments/{name}/stop")
def stop_deployment(name: str):
    """Stop and remove a deployment container.

    Args:
        name: Container/deployment name.
    """
    try:
        success = stop_container(name)
        if not success:
            raise HTTPException(
                status_code=404,
                detail=f"Container '{name}' not found or already stopped",
            )
        return {"status": "success", "message": f"Container '{name}' stopped"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to stop container %s: %s", name, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/deployments")
def list_deployments():
    """List all managed deployments (containers with spark-pulse labels)."""
    try:
        containers = list_managed_containers()
        return [
            {
                "id": c.id,
                "name": c.name,
                "status": c.status,
                "image": c.image,
                "metadata": {
                    "deployment": c.metadata.deployment,
                    "recipe": c.metadata.recipe,
                    "mode": c.metadata.mode,
                    "created_at": c.metadata.created_at,
                    "privileged": c.metadata.privileged,
                },
            }
            for c in containers
        ]
    except Exception as exc:
        logger.error("Failed to list containers: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/deployments/{name}/status")
def get_deployment_status(name: str):
    """Get status of a specific deployment container.

    Args:
        name: Container/deployment name.
    """
    try:
        status = get_container_status(name)
        if status["status"] == "missing":
            raise HTTPException(
                status_code=404,
                detail=f"Container '{name}' not found",
            )
        return status
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to get status for %s: %s", name, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/deployments/{name}/exec")
def exec_in_deployment(
    name: str,
    body: dict[str, Any],
):
    """Execute a command inside a deployment container.

    Args:
        name: Container/deployment name.
        body: {"command": "ls -la"}
    """
    try:
        command = body.get("command", "")
        if not command:
            raise HTTPException(status_code=400, detail="command is required")

        # For simulation mode, return a mock response
        if is_simulation():
            return {
                "status": "success",
                "output": f"Simulated exec of: {command}",
            }

        # Real mode — use DockerService
        service = DockerService()
        output = service.exec_in_container(name, command)
        return {
            "status": "success",
            "output": output if isinstance(output, str) else "",
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to exec in %s: %s", name, exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Container management endpoints ───────────────────────────────────────────


@router.get("/containers")
def list_containers():
    """List all spark-pulse managed containers (alias for /deployments)."""
    return list_deployments()


@router.get("/containers/{name}/logs")
def get_container_logs(name: str, tail: int = 100):
    """Get recent logs from a container.

    Args:
        name: Container name.
        tail: Number of lines to return.
    """
    try:
        if is_simulation():
            return {
                "logs": [
                    f"[sim] Line {i}: Container {name} log entry",
                ] * min(tail, 10),
            }

        service = DockerService()
        status = service.get_container_status(name)
        if status["status"] != "running":
            raise HTTPException(
                status_code=400,
                detail=f"Container '{name}' is not running (status: {status['status']})",
            )

        # Get logs from Docker
        container = service.client.containers.get(name)
        logs = container.logs(tail=tail).decode()
        return {"logs": logs.splitlines()}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to get logs for %s: %s", name, exc)
        raise HTTPException(status_code=500, detail=str(exc))
