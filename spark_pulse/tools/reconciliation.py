"""Runtime reconciliation for recovering deployment/cluster state on restart.

Provides mechanisms to reconstruct cluster and deployment state from Docker
labels after server restart, preventing visibility loss of running resources.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Docker label constants — the single ``spark-pulse.*`` namespace that
# DockerService actually writes. The cluster keys below are read-only now: the
# orchestrator that wrote them is deleted, and what is left is finding the
# containers an older build labelled. Re-exported so callers and tests have one
# place to import them from.
from spark_pulse.tools.labels import (
    CLUSTER_LABEL as CLUSTER_LABEL,
)
from spark_pulse.tools.labels import (
    CONTAINER_NAME_LABEL as CONTAINER_NAME_LABEL,
)
from spark_pulse.tools.labels import (
    CREATED_AT_LABEL as CREATED_AT_LABEL,
)
from spark_pulse.tools.labels import (
    DEPLOYMENT_LABEL as DEPLOYMENT_LABEL,
)
from spark_pulse.tools.labels import (
    HEAD_IP_LABEL as HEAD_IP_LABEL,
)
from spark_pulse.tools.labels import (
    IMAGE_LABEL as IMAGE_LABEL,
)
from spark_pulse.tools.labels import (
    NAME_LABEL as NAME_LABEL,
)
from spark_pulse.tools.labels import (
    RAY_ENABLED_LABEL as RAY_ENABLED_LABEL,
)
from spark_pulse.tools.labels import (
    RAY_READY_LABEL as RAY_READY_LABEL,
)
from spark_pulse.tools.labels import (
    WORKER_IPS_LABEL as WORKER_IPS_LABEL,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReconciliationResult:
    """Result of a reconciliation pass."""

    clusters_reconciled: int = 0
    deployments_reconciled: int = 0
    orphaned_containers_cleaned: int = 0
    errors: list[str] = field(default_factory=list)


def _parse_worker_ips(raw: str) -> list[str]:
    """Parse comma-separated worker IPs from Docker label."""
    if not raw:
        return []
    return [ip.strip() for ip in raw.split(",") if ip.strip()]


def _parse_bool(raw: str) -> bool:
    """Parse boolean from Docker label string."""
    return raw.lower() in ("true", "1", "yes")


def _default_docker() -> Any | None:
    """Build the default local DockerService, or None when unavailable."""
    try:
        from spark_pulse.tools.docker import DockerService

        return DockerService()
    except Exception as e:  # pragma: no cover — import-time failure only
        logger.warning("Docker service unavailable: %s", e)
        return None


def _default_cluster_service() -> Any | None:
    """The control node's container service, or None when unavailable.

    Reconciliation rebuilds state from container labels, and without the node
    registry the only daemon it can enumerate is this machine's. That used to
    be expressed as an empty host on a service that claimed to reach any node;
    it is now an explicit control-node resolution, so the limit is visible
    rather than accidental. Reconciling a peer's containers is registry work.
    """
    try:
        from spark_pulse.tools.node_service import control_node, service_for

        return service_for(control_node())
    except Exception as e:  # pragma: no cover — import-time failure only
        logger.warning("Container service unavailable: %s", e)
        return None


def _reconstruct_cluster_state(labels: dict[str, str]) -> dict[str, Any] | None:
    """Reconstruct cluster state from Docker container labels.

    Returns None if required labels are missing or malformed.
    """
    cluster_name = labels.get(CLUSTER_LABEL)
    if not cluster_name:
        return None

    head_ip = labels.get(HEAD_IP_LABEL, "")
    worker_ips = _parse_worker_ips(labels.get(WORKER_IPS_LABEL, ""))
    ray_enabled = _parse_bool(labels.get(RAY_ENABLED_LABEL, "true"))
    ray_ready = _parse_bool(labels.get(RAY_READY_LABEL, "false"))
    created_at = labels.get(CREATED_AT_LABEL, "")
    image = labels.get(IMAGE_LABEL, "")
    container_name = labels.get(CONTAINER_NAME_LABEL, "")

    now = datetime.now(timezone.utc).isoformat()

    return {
        "name": cluster_name,
        "head_ip": head_ip,
        "worker_ips": worker_ips,
        "ray_enabled": ray_enabled,
        "ray_ready": ray_ready,
        "created_at": created_at or now,
        "image": image,
        "container_name": container_name,
        "reconciled_at": now,
    }


def _reconstruct_deployment(labels: dict[str, str]) -> dict[str, Any] | None:
    """Reconstruct solo deployment state from Docker container labels.

    Returns None if required labels are missing or malformed.
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
    }


def reconcile_clusters(
    cluster_service: Any = None,
) -> list[dict[str, Any]]:
    """Reconstruct cluster state from Docker labels.

    1. List all containers with label spark_pulse.cluster present
    2. Group by cluster name
    3. For each group, reconstruct cluster state from labels
    4. Return list of cluster state dicts

    Args:
        cluster_service: Container service bound to the node whose containers
            are being reconciled. Defaults to the control node's.

    Returns:
        List of reconstructed cluster state dicts.
    """
    if os.environ.get("SIMULATION_MODE", "0") == "1":
        return _reconcile_clusters_mock()

    return _reconcile_clusters_real(cluster_service)


def _reconcile_clusters_mock() -> list[dict[str, Any]]:
    """Mock reconciliation for simulation mode."""
    logger.info("[MOCK] Reconciling clusters from labels (simulation mode)")
    return []


def _reconcile_clusters_real(cluster_service: Any = None) -> list[dict[str, Any]]:
    """Real reconciliation through the container service."""
    clusters: list[dict[str, Any]] = []

    try:
        service = cluster_service or _default_cluster_service()
        if service is None:
            return []
        containers = service.list_managed_containers({CLUSTER_LABEL: ""})
    except Exception as e:
        logger.error("Failed to reconcile clusters: %s", e)
        return []

    for container in containers:
        labels = getattr(container, "labels", {}) or {}
        state = _reconstruct_cluster_state(labels)
        if state:
            state["status"] = getattr(container, "status", "")
            state["container_name"] = state["container_name"] or getattr(
                container, "name", ""
            )
            clusters.append(state)

    return clusters


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


def reconcile_all(
    docker: Any = None,
    cluster_service: Any = None,
) -> ReconciliationResult:
    """Run full reconciliation pass.

    Called at server startup via app.py lifespan.

    Args:
        docker: DockerService for solo deployments.
        cluster_service: Container service for cluster deployments.

    Returns:
        ReconciliationResult with counts and errors.
    """
    result = ReconciliationResult()

    # Reconcile clusters
    try:
        clusters = reconcile_clusters(cluster_service)
        result.clusters_reconciled = len(clusters)
        logger.info(
            "Reconciled %d clusters",
            result.clusters_reconciled,
        )
    except Exception as e:
        error_msg = f"Cluster reconciliation failed: {e}"
        logger.error(error_msg)
        result.errors.append(error_msg)

    # Reconcile solo deployments
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
        "Reconciliation complete: %d clusters, %d deployments, %d orphans cleaned",
        result.clusters_reconciled,
        result.deployments_reconciled,
        result.orphaned_containers_cleaned,
    )

    return result


def _clean_orphaned_containers(docker: Any = None) -> int:
    """Remove exited containers that carry spark-pulse cluster/deployment labels.

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
        if not (labels.get(CLUSTER_LABEL) or labels.get(DEPLOYMENT_LABEL)):
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
