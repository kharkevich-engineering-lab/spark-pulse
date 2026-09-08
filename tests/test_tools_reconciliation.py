"""Tests for runtime reconciliation on server restart."""

from __future__ import annotations

from spark_pulse.tools.reconciliation import (
    ReconciliationResult,
    _reconstruct_deployment,
    reconcile_all,
)


class TestReconstructDeployment:
    def test_full_labels(self):
        labels = {
            "spark-pulse.deployment": "test-deployment",
            "spark-pulse.name": "test-container",
            "spark-pulse.image": "vllm:latest",
        }
        result = _reconstruct_deployment(labels)
        assert result is not None
        assert result["id"] == "test-deployment"
        assert result["container_name"] == "test-container"

    def test_missing_deployment_label(self):
        labels = {"spark-pulse.name": "test-container"}
        assert _reconstruct_deployment(labels) is None

    def test_rank_identity_is_read_from_the_labels(self):
        """Which rank of which attempt, read back rather than parsed out."""
        labels = {
            "spark-pulse.deployment": "dep1",
            "spark-pulse.name": "spark-pulse-dep1-r2-g3",
            "spark-pulse.generation": "3",
            "spark-pulse.rank": "2",
            "spark-pulse.world_size": "4",
        }

        result = _reconstruct_deployment(labels)

        assert result["generation"] == 3
        assert result["rank"] == 2
        assert result["world_size"] == 4

    def test_a_container_from_before_ranks_is_a_lone_rank_zero(self):
        result = _reconstruct_deployment({"spark-pulse.deployment": "old"})

        assert (result["generation"], result["rank"], result["world_size"]) == (0, 0, 1)

    def test_a_malformed_identity_label_does_not_crash_reconciliation(self):
        """An unreadable label must not turn a recovery pass into an exception."""
        labels = {
            "spark-pulse.deployment": "dep1",
            "spark-pulse.generation": "not-a-number",
            "spark-pulse.rank": "",
            "spark-pulse.world_size": "-1",
        }

        result = _reconstruct_deployment(labels)

        assert (result["generation"], result["rank"], result["world_size"]) == (0, 0, 1)


class TestReconciliationResult:
    def test_default_values(self):
        result = ReconciliationResult()
        assert result.deployments_reconciled == 0
        assert result.orphaned_containers_cleaned == 0
        assert result.errors == []

    def test_with_values(self):
        result = ReconciliationResult(
            deployments_reconciled=3,
            orphaned_containers_cleaned=1,
            errors=["error1"],
        )
        assert result.deployments_reconciled == 3
        assert result.orphaned_containers_cleaned == 1
        assert result.errors == ["error1"]


class TestReconcileAll:
    def test_simulation_mode(self):
        result = reconcile_all()
        assert isinstance(result, ReconciliationResult)
        assert result.deployments_reconciled == 0
