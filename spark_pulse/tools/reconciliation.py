"""Recovering deployment state from container labels after a restart.

A restart loses nothing the containers still know: each carries the deployment
it belongs to, which attempt created it, which rank it is and how many ranks
the gang has. Reconciliation reads that back, so a control plane that comes up
does not report a running deployment as gone.

There was a second half here that reconstructed *clusters* from the labels a
separate orchestrator wrote. That orchestrator was removed — a cluster is a
deployment of size N now — and the reconstruction went with it rather than
staying to recognise containers no build in circulation can create.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Docker label constants — the single ``spark-pulse.*`` namespace that
# DockerService writes. Re-exported so callers and tests have one place to
# import them from.
from spark_pulse.tools.labels import (
    CONTAINER_NAME_LABEL as CONTAINER_NAME_LABEL,
    CREATED_AT_LABEL as CREATED_AT_LABEL,
    DEPLOYMENT_LABEL as DEPLOYMENT_LABEL,
    GENERATION_LABEL as GENERATION_LABEL,
    IMAGE_LABEL as IMAGE_LABEL,
    NAME_LABEL as NAME_LABEL,
    RANK_LABEL as RANK_LABEL,
    WORLD_SIZE_LABEL as WORLD_SIZE_LABEL,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReconciliationResult:
    """Result of a reconciliation pass."""

    deployments_reconciled: int = 0
    orphaned_containers_cleaned: int = 0
    errors: list[str] = field(default_factory=list)


def _default_docker() -> Any | None:
    """Build the default local DockerService, or None when unavailable."""
    try:
        from spark_pulse.tools.docker import DockerService

        return DockerService()
    except Exception as e:  # pragma: no cover — import-time failure only
        logger.warning("Docker service unavailable: %s", e)
        return None


def _int_label(labels: dict[str, str], key: str, default: int) -> int:
    """One integer label, defaulted rather than trusted."""
    raw = str(labels.get(key, "")).strip()
    return int(raw) if raw.isdigit() else default


def _reconstruct_deployment(labels: dict[str, str]) -> dict[str, Any] | None:
    """Reconstruct a deployment rank from Docker container labels.

    Returns None if required labels are missing or malformed.

    A native container is one rank of one generation of a gang, and it says so
    in its labels. Reconciliation reads them back rather than inferring
    anything from the container name: the name is derived from the identity,
    never the other way round. A container written before ranks existed has
    none of them, and reads back as rank zero of a gang of one at generation
    zero — which is exactly what it was.
    """
    deployment_name = labels.get(DEPLOYMENT_LABEL)
    if not deployment_name:
        return None

    container_name = labels.get(CONTAINER_NAME_LABEL, "")
    image = labels.get(IMAGE_LABEL, "")
    created_at = labels.get(CREATED_AT_LABEL, "")

    now = datetime.now(timezone.utc).isoformat()

    return {
        "id": deployment_name,
        "container_name": container_name,
        "image": image,
        "created_at": created_at or now,
        "status": "running",  # Will be updated by caller based on container state
        "reconciled_at": now,
        "generation": _int_label(labels, GENERATION_LABEL, 0),
        "rank": _int_label(labels, RANK_LABEL, 0),
        "world_size": _int_label(labels, WORLD_SIZE_LABEL, 1),
    }


def reconcile_deployments(
    docker: Any = None,
) -> list[dict[str, Any]]:
    """Reconcile solo deployments from Docker labels.

    1. List all containers with label spark_pulse.deployment present
    2. For each container, check if deployment record exists
    3. If not, create deployment record from labels
    4. If yes, update status from container state

    Args:
        docker: DockerService instance (mock or real).
                If None, uses simulation mode.

    Returns:
        List of updated deployment dicts.
    """
    if os.environ.get("SIMULATION_MODE", "0") == "1":
        return _reconcile_deployments_mock()

    return _reconcile_deployments_real(docker)


def _reconcile_deployments_mock() -> list[dict[str, Any]]:
    """Mock reconciliation for simulation mode."""
    logger.info("[MOCK] Reconciling deployments from labels (simulation mode)")
    return []


def _reconcile_deployments_real(docker: Any = None) -> list[dict[str, Any]]:
    """Real reconciliation through the container service."""
    deployments_list: list[dict[str, Any]] = []

    try:
        service = docker or _default_docker()
        if service is None:
            return []
        containers = service.list_managed_containers({DEPLOYMENT_LABEL: ""})
    except Exception as e:
        logger.error("Failed to reconcile deployments: %s", e)
        return []

    for container in containers:
        labels = getattr(container, "labels", {}) or {}
        state = _reconstruct_deployment(labels)
        if state:
            state["status"] = getattr(container, "status", "")
            state["container_name"] = state["container_name"] or getattr(
                container, "name", ""
            )
            deployments_list.append(state)

    return deployments_list


def reconcile_all(docker: Any = None) -> ReconciliationResult:
    """Run full reconciliation pass.

    Called at server startup via app.py lifespan.

    Args:
        docker: DockerService for solo deployments.

    Returns:
        ReconciliationResult with counts and errors.
    """
    result = ReconciliationResult()

    # Reconcile deployments against what the nodes are actually running
    try:
        deployments_list = reconcile_deployments(docker)
        result.deployments_reconciled = len(deployments_list)
        logger.info(
            "Reconciled %d deployments",
            result.deployments_reconciled,
        )
    except Exception as e:
        error_msg = f"Deployment reconciliation failed: {e}"
        logger.error(error_msg)
        result.errors.append(error_msg)

    # Detect and clean orphaned containers
    try:
        if os.environ.get("SIMULATION_MODE", "0") == "1":
            result.orphaned_containers_cleaned = 0
        else:
            result.orphaned_containers_cleaned = _clean_orphaned_containers(docker)
    except Exception as e:
        error_msg = f"Orphan cleanup failed: {e}"
        logger.error(error_msg)
        result.errors.append(error_msg)

    logger.info(
        "Reconciliation complete: %d deployments, %d orphans cleaned",
        result.deployments_reconciled,
        result.orphaned_containers_cleaned,
    )

    return result


def _clean_orphaned_containers(docker: Any = None) -> int:
    """Remove exited containers that carry a spark-pulse deployment label.

    Returns:
        Number of orphaned containers cleaned.
    """
    cleaned = 0

    try:
        service = docker or _default_docker()
        if service is None:
            return 0
        containers = service.list_managed_containers()
    except Exception as e:
        logger.error("Failed to clean orphaned containers: %s", e)
        return 0

    for container in containers:
        labels = getattr(container, "labels", {}) or {}
        if not labels.get(DEPLOYMENT_LABEL):
            continue
        if getattr(container, "status", "") != "exited":
            continue
        try:
            logger.info("Cleaning orphaned container: %s", container.name)
            service.stop_container(container.name)
            cleaned += 1
        except Exception as e:
            logger.error("Failed to remove orphaned container %s: %s", container, e)

    return cleaned
