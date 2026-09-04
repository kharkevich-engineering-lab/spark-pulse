"""Mock reconciliation provider for simulation mode."""

from __future__ import annotations

from typing import Any

from spark_pulse.tools.reconciliation import (
    ReconciliationResult,
)


class MockReconciler:
    """Mock reconciliation for simulation mode.

    Scenarios:
    - "default": all healthy, no orphans
    - "orphaned": has orphaned containers to clean
    - "partial": some reconciliation failures
    """

    def __init__(self, scenario: str = "default"):
        self.scenario = scenario

    def reconcile_clusters(self, cluster_service: Any = None) -> list[dict]:
        """Mock cluster reconciliation."""
        if self.scenario == "partial":
            return []  # Simulate failure
        return []  # No clusters in simulation mode

    def reconcile_deployments(self, docker: Any = None) -> list[dict]:
        """Mock deployment reconciliation."""
        if self.scenario == "partial":
            return []  # Simulate failure
        return []  # No deployments in simulation mode

    def reconcile_all(
        self, docker: Any = None, cluster_service: Any = None
    ) -> ReconciliationResult:
        """Mock full reconciliation."""
        if self.scenario == "orphaned":
            return ReconciliationResult(
                clusters_reconciled=0,
                deployments_reconciled=0,
                orphaned_containers_cleaned=3,
                errors=[],
            )
        if self.scenario == "partial":
            return ReconciliationResult(
                clusters_reconciled=0,
                deployments_reconciled=0,
                orphaned_containers_cleaned=0,
                errors=[
                    "Cluster reconciliation failed (mock)",
                    "Deployment reconciliation failed (mock)",
                ],
            )
        return ReconciliationResult(
            clusters_reconciled=0,
            deployments_reconciled=0,
            orphaned_containers_cleaned=0,
            errors=[],
        )


# ── Module-level API (mirrors spark_pulse.tools.reconciliation) ──────────────

_reconciler = MockReconciler()


def reconcile_clusters(cluster_service: Any = None) -> list[dict]:
    """Mock cluster reconciliation."""
    return _reconciler.reconcile_clusters(cluster_service)


def reconcile_deployments(docker: Any = None) -> list[dict]:
    """Mock deployment reconciliation."""
    return _reconciler.reconcile_deployments(docker)


def reconcile_all(
    docker: Any = None, cluster_service: Any = None
) -> ReconciliationResult:
    """Mock full reconciliation."""
    return _reconciler.reconcile_all(docker, cluster_service)
