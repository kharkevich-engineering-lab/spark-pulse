"""Tests for runtime reconciliation on server restart."""

from __future__ import annotations

from spark_pulse.tools.reconciliation import (
    ReconciliationResult,
    _parse_bool,
    _parse_worker_ips,
    _reconstruct_cluster_state,
    _reconstruct_deployment,
    reconcile_all,
)


class TestParseWorkerIPs:
    def test_comma_separated(self):
        assert _parse_worker_ips("10.0.0.1,10.0.0.2,10.0.0.3") == [
            "10.0.0.1",
            "10.0.0.2",
            "10.0.0.3",
        ]

    def test_single_ip(self):
        assert _parse_worker_ips("10.0.0.1") == ["10.0.0.1"]

    def test_empty_string(self):
        assert _parse_worker_ips("") == []

    def test_whitespace(self):
        assert _parse_worker_ips(" 10.0.0.1 , 10.0.0.2 ") == ["10.0.0.1", "10.0.0.2"]


class TestParseBool:
    def test_true_values(self):
        assert _parse_bool("true") is True
        assert _parse_bool("True") is True
        assert _parse_bool("1") is True
        assert _parse_bool("yes") is True

    def test_false_values(self):
        assert _parse_bool("false") is False
        assert _parse_bool("False") is False
        assert _parse_bool("0") is False
        assert _parse_bool("no") is False


class TestReconstructClusterState:
    def test_full_labels(self):
        labels = {
            "spark-pulse.cluster": "test-cluster",
            "spark-pulse.head_ip": "10.0.0.1",
            "spark-pulse.worker_ips": "10.0.0.2,10.0.0.3",
            "spark-pulse.ray_enabled": "true",
            "spark-pulse.ray_ready": "true",
            "spark-pulse.created_at": "2024-01-01T00:00:00+00:00",
            "spark-pulse.image": "vllm:latest",
            "spark-pulse.name": "test-cluster-head",
        }
        result = _reconstruct_cluster_state(labels)
        assert result is not None
        assert result["name"] == "test-cluster"
        assert result["head_ip"] == "10.0.0.1"
        assert result["worker_ips"] == ["10.0.0.2", "10.0.0.3"]
        assert result["ray_enabled"] is True
        assert result["ray_ready"] is True

    def test_missing_cluster_label(self):
        labels = {"spark-pulse.head_ip": "10.0.0.1"}
        assert _reconstruct_cluster_state(labels) is None

    def test_partial_labels(self):
        labels = {
            "spark-pulse.cluster": "partial-cluster",
            "spark-pulse.head_ip": "10.0.0.1",
        }
        result = _reconstruct_cluster_state(labels)
        assert result is not None
        assert result["name"] == "partial-cluster"
        assert result["worker_ips"] == []


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
        assert result.clusters_reconciled == 0
        assert result.deployments_reconciled == 0
        assert result.orphaned_containers_cleaned == 0
        assert result.errors == []

    def test_with_values(self):
        result = ReconciliationResult(
            clusters_reconciled=2,
            deployments_reconciled=3,
            orphaned_containers_cleaned=1,
            errors=["error1"],
        )
        assert result.clusters_reconciled == 2
        assert result.deployments_reconciled == 3
        assert result.orphaned_containers_cleaned == 1
        assert result.errors == ["error1"]


class TestReconcileAll:
    def test_simulation_mode(self):
        result = reconcile_all()
        assert isinstance(result, ReconciliationResult)
        assert result.clusters_reconciled == 0
        assert result.deployments_reconciled == 0
