"""Tests for runtime reconciliation on server restart."""

from __future__ import annotations

import pytest

from spark_pulse.tools.reconciliation import (
    ReconciliationResult,
    _parse_worker_ips,
    _parse_bool,
    _reconstruct_cluster_state,
    _reconstruct_deployment,
    reconcile_clusters,
    reconcile_deployments,
    reconcile_all,
)


class TestParseWorkerIPs:
    def test_comma_separated(self):
        assert _parse_worker_ips("10.0.0.1,10.0.0.2,10.0.0.3") == [
            "10.0.0.1", "10.0.0.2", "10.0.0.3"
        ]

    def test_single_ip(self):
        assert _parse_worker_ips("10.0.0.1") == ["10.0.0.1"]

    def test_empty_string(self):
        assert _parse_worker_ips("") == []

    def test_whitespace(self):
        assert _parse_worker_ips(" 10.0.0.1 , 10.0.0.2 ") == [
            "10.0.0.1", "10.0.0.2"
        ]


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
            "spark_pulse.cluster": "test-cluster",
            "spark_pulse.head_ip": "10.0.0.1",
            "spark_pulse.worker_ips": "10.0.0.2,10.0.0.3",
            "spark_pulse.ray_enabled": "true",
            "spark_pulse.ray_ready": "true",
            "spark_pulse.created_at": "2024-01-01T00:00:00+00:00",
            "spark_pulse.image": "vllm:latest",
            "spark_pulse.container_name": "test-cluster-head",
        }
        result = _reconstruct_cluster_state(labels)
        assert result is not None
        assert result["name"] == "test-cluster"
        assert result["head_ip"] == "10.0.0.1"
        assert result["worker_ips"] == ["10.0.0.2", "10.0.0.3"]
        assert result["ray_enabled"] is True
        assert result["ray_ready"] is True

    def test_missing_cluster_label(self):
        labels = {"spark_pulse.head_ip": "10.0.0.1"}
        assert _reconstruct_cluster_state(labels) is None

    def test_partial_labels(self):
        labels = {
            "spark_pulse.cluster": "partial-cluster",
            "spark_pulse.head_ip": "10.0.0.1",
        }
        result = _reconstruct_cluster_state(labels)
        assert result is not None
        assert result["name"] == "partial-cluster"
        assert result["worker_ips"] == []


class TestReconstructDeployment:
    def test_full_labels(self):
        labels = {
            "spark_pulse.deployment": "test-deployment",
            "spark_pulse.container_name": "test-container",
            "spark_pulse.image": "vllm:latest",
        }
        result = _reconstruct_deployment(labels)
        assert result is not None
        assert result["id"] == "test-deployment"
        assert result["container_name"] == "test-container"

    def test_missing_deployment_label(self):
        labels = {"spark_pulse.container_name": "test-container"}
        assert _reconstruct_deployment(labels) is None


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
