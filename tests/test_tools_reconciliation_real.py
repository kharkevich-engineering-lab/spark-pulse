"""Tests for reconciliation against a real container service.

``tests/test_tools_reconciliation.py`` covers the label parsers and the
simulation short-circuit. Everything past that short-circuit — the code that
runs on the Spark at every restart, rebuilding what is running from container
labels and sweeping up the containers that died while the server was down —
had no tests. It is the code whose failure mode is "the Jobs page is empty
after a reboot", which nobody notices until it happens.

The module reads ``SIMULATION_MODE`` from the environment on every call, so
these tests turn it off and drive the real branches with a fake service.
"""

from __future__ import annotations

import pytest

import sys
from types import ModuleType

import spark_pulse.tools.reconciliation  # noqa: F401 — see below
from spark_pulse.tools.labels import (
    CLUSTER_LABEL,
    CONTAINER_NAME_LABEL,
    DEPLOYMENT_LABEL,
    HEAD_IP_LABEL,
    IMAGE_LABEL,
    WORKER_IPS_LABEL,
)

# Reaching the real module takes the same care conftest.py takes: under
# SIMULATION_MODE ``spark_pulse.tools.reconciliation`` is an *attribute* of the
# tools package pointing at the mock, and ``import ... as`` reads that
# attribute. sys.modules holds the submodule itself.
rec = sys.modules["spark_pulse.tools.reconciliation"]


@pytest.fixture(autouse=True)
def production_mode(monkeypatch):
    """Reconciliation as it runs on the machine, not in the simulator."""
    monkeypatch.setenv("SIMULATION_MODE", "0")


class _Container:
    def __init__(self, name, labels=None, status="running"):
        self.name = name
        self.labels = labels or {}
        self.status = status


class _Service:
    """A container service that answers from a canned list, or refuses."""

    def __init__(self, containers=(), error=None, stop_error=None):
        self._containers = list(containers)
        self._error = error
        self._stop_error = stop_error
        self.filters: list[dict | None] = []
        self.stopped: list[str] = []

    def list_managed_containers(self, filters=None):
        self.filters.append(filters)
        if self._error:
            raise self._error
        return list(self._containers)

    def stop_container(self, name):
        if self._stop_error:
            raise self._stop_error
        self.stopped.append(name)


# ── Default services ─────────────────────────────────────────────────────────


class TestDefaultServices:
    """Both defaults are resolved through a function-level import.

    Standing a fake module in ``sys.modules`` is what that import actually
    reads — patching an attribute on ``spark_pulse.tools.<name>`` would patch
    whichever twin the simulation switch happens to be holding.
    """

    def test_the_default_docker_service_is_the_local_one(self, monkeypatch):
        built = object()
        fake = ModuleType("spark_pulse.tools.docker")
        fake.DockerService = lambda: built
        monkeypatch.setitem(sys.modules, "spark_pulse.tools.docker", fake)

        assert rec._default_docker() is built

    def test_the_default_cluster_service_is_the_control_nodes(self, monkeypatch):
        service = object()
        fake = ModuleType("spark_pulse.tools.node_service")
        fake.control_node = lambda: "control-node"
        fake.service_for = lambda node: service if node == "control-node" else None
        monkeypatch.setitem(sys.modules, "spark_pulse.tools.node_service", fake)

        assert rec._default_cluster_service() is service


# ── Clusters ─────────────────────────────────────────────────────────────────


class TestReconcileClusters:
    def test_a_labelled_container_becomes_a_cluster(self):
        service = _Service(
            [
                _Container(
                    "spark-head",
                    {
                        CLUSTER_LABEL: "prod",
                        HEAD_IP_LABEL: "10.0.0.1",
                        WORKER_IPS_LABEL: "10.0.0.2, 10.0.0.3",
                        IMAGE_LABEL: "vllm:latest",
                    },
                )
            ]
        )

        clusters = rec.reconcile_clusters(service)

        assert len(clusters) == 1
        assert clusters[0]["name"] == "prod"
        assert clusters[0]["worker_ips"] == ["10.0.0.2", "10.0.0.3"]
        assert clusters[0]["status"] == "running"

    def test_only_containers_carrying_the_cluster_label_are_asked_for(self):
        service = _Service()

        rec.reconcile_clusters(service)

        assert service.filters == [{CLUSTER_LABEL: ""}]

    def test_a_container_without_the_label_is_skipped(self):
        service = _Service([_Container("stray", {IMAGE_LABEL: "vllm:latest"})])

        assert rec.reconcile_clusters(service) == []

    def test_the_container_name_fills_in_when_the_label_is_missing(self):
        service = _Service([_Container("spark-head", {CLUSTER_LABEL: "prod"})])

        assert rec.reconcile_clusters(service)[0]["container_name"] == "spark-head"

    def test_the_name_label_wins_over_the_container_name(self):
        service = _Service(
            [
                _Container(
                    "docker-assigned",
                    {CLUSTER_LABEL: "prod", CONTAINER_NAME_LABEL: "labelled"},
                )
            ]
        )

        assert rec.reconcile_clusters(service)[0]["container_name"] == "labelled"

    def test_no_service_at_all_reconciles_nothing_rather_than_raising(
        self, monkeypatch
    ):
        monkeypatch.setattr(rec, "_default_cluster_service", lambda: None)

        assert rec.reconcile_clusters() == []

    def test_a_daemon_that_will_not_answer_reconciles_nothing(self, caplog):
        service = _Service(error=RuntimeError("docker is down"))

        assert rec.reconcile_clusters(service) == []
        assert "docker is down" in caplog.text

    def test_simulation_mode_short_circuits_before_any_service(self, monkeypatch):
        monkeypatch.setenv("SIMULATION_MODE", "1")
        service = _Service(error=AssertionError("must not be reached"))

        assert rec.reconcile_clusters(service) == []


# ── Deployments ──────────────────────────────────────────────────────────────


class TestReconcileDeployments:
    def test_a_labelled_container_becomes_a_deployment(self):
        service = _Service(
            [
                _Container(
                    "vllm-node",
                    {DEPLOYMENT_LABEL: "dep-1", IMAGE_LABEL: "vllm:latest"},
                    status="exited",
                )
            ]
        )

        deployments = rec.reconcile_deployments(service)

        assert deployments[0]["id"] == "dep-1"
        assert deployments[0]["image"] == "vllm:latest"
        # The label says "running"; the container's own state overrides it.
        assert deployments[0]["status"] == "exited"

    def test_only_containers_carrying_the_deployment_label_are_asked_for(self):
        service = _Service()

        rec.reconcile_deployments(service)

        assert service.filters == [{DEPLOYMENT_LABEL: ""}]

    def test_a_container_without_the_label_is_skipped(self):
        service = _Service([_Container("stray", {CLUSTER_LABEL: "prod"})])

        assert rec.reconcile_deployments(service) == []

    def test_the_container_name_fills_in_when_the_label_is_missing(self):
        service = _Service([_Container("vllm-node", {DEPLOYMENT_LABEL: "dep-1"})])

        assert rec.reconcile_deployments(service)[0]["container_name"] == "vllm-node"

    def test_no_service_at_all_reconciles_nothing_rather_than_raising(
        self, monkeypatch
    ):
        monkeypatch.setattr(rec, "_default_docker", lambda: None)

        assert rec.reconcile_deployments() == []

    def test_a_daemon_that_will_not_answer_reconciles_nothing(self, caplog):
        service = _Service(error=RuntimeError("docker is down"))

        assert rec.reconcile_deployments(service) == []
        assert "docker is down" in caplog.text

    def test_simulation_mode_short_circuits_before_any_service(self, monkeypatch):
        monkeypatch.setenv("SIMULATION_MODE", "1")
        service = _Service(error=AssertionError("must not be reached"))

        assert rec.reconcile_deployments(service) == []


# ── Orphan sweep ─────────────────────────────────────────────────────────────


class TestCleanOrphanedContainers:
    def test_an_exited_managed_container_is_stopped(self):
        service = _Service(
            [
                _Container("dead", {DEPLOYMENT_LABEL: "dep-1"}, status="exited"),
                _Container("dead-cluster", {CLUSTER_LABEL: "prod"}, status="exited"),
            ]
        )

        assert rec._clean_orphaned_containers(service) == 2
        assert service.stopped == ["dead", "dead-cluster"]

    def test_a_running_container_is_left_alone(self):
        service = _Service(
            [_Container("alive", {DEPLOYMENT_LABEL: "dep-1"}, status="running")]
        )

        assert rec._clean_orphaned_containers(service) == 0
        assert service.stopped == []

    def test_a_container_nobody_here_manages_is_left_alone(self):
        service = _Service([_Container("someone-elses", {}, status="exited")])

        assert rec._clean_orphaned_containers(service) == 0
        assert service.stopped == []

    def test_the_sweep_asks_for_every_managed_container(self):
        service = _Service()

        rec._clean_orphaned_containers(service)

        assert service.filters == [None]

    def test_a_container_that_will_not_stop_is_not_counted(self, caplog):
        service = _Service(
            [_Container("stuck", {DEPLOYMENT_LABEL: "dep-1"}, status="exited")],
            stop_error=RuntimeError("device or resource busy"),
        )

        assert rec._clean_orphaned_containers(service) == 0
        assert "device or resource busy" in caplog.text

    def test_no_service_at_all_cleans_nothing(self, monkeypatch):
        monkeypatch.setattr(rec, "_default_docker", lambda: None)

        assert rec._clean_orphaned_containers() == 0

    def test_a_daemon_that_will_not_answer_cleans_nothing(self, caplog):
        service = _Service(error=RuntimeError("docker is down"))

        assert rec._clean_orphaned_containers(service) == 0
        assert "docker is down" in caplog.text


# ── The whole pass ───────────────────────────────────────────────────────────


class TestReconcileAll:
    def test_the_counts_come_from_the_three_passes(self, monkeypatch):
        monkeypatch.setattr(rec, "reconcile_clusters", lambda _s: [{"name": "prod"}])
        monkeypatch.setattr(
            rec, "reconcile_deployments", lambda _d: [{"id": "a"}, {"id": "b"}]
        )
        monkeypatch.setattr(rec, "_clean_orphaned_containers", lambda _d: 3)

        result = rec.reconcile_all()

        assert result.clusters_reconciled == 1
        assert result.deployments_reconciled == 2
        assert result.orphaned_containers_cleaned == 3
        assert result.errors == []

    def test_each_pass_that_fails_is_recorded_and_the_others_still_run(
        self, monkeypatch
    ):
        def boom(_arg):
            raise RuntimeError("no daemon")

        monkeypatch.setattr(rec, "reconcile_clusters", boom)
        monkeypatch.setattr(rec, "reconcile_deployments", boom)
        monkeypatch.setattr(rec, "_clean_orphaned_containers", boom)

        result = rec.reconcile_all()

        assert result.errors == [
            "Cluster reconciliation failed: no daemon",
            "Deployment reconciliation failed: no daemon",
            "Orphan cleanup failed: no daemon",
        ]
        assert result.clusters_reconciled == 0

    def test_one_failing_pass_does_not_stop_the_others(self, monkeypatch):
        def boom(_arg):
            raise RuntimeError("no daemon")

        monkeypatch.setattr(rec, "reconcile_clusters", boom)
        monkeypatch.setattr(rec, "reconcile_deployments", lambda _d: [{"id": "a"}])
        monkeypatch.setattr(rec, "_clean_orphaned_containers", lambda _d: 1)

        result = rec.reconcile_all()

        assert result.deployments_reconciled == 1
        assert result.orphaned_containers_cleaned == 1
        assert len(result.errors) == 1

    def test_simulation_mode_never_sweeps_containers(self, monkeypatch):
        monkeypatch.setenv("SIMULATION_MODE", "1")
        monkeypatch.setattr(
            rec,
            "_clean_orphaned_containers",
            lambda _d: pytest.fail("must not sweep in simulation"),
        )

        assert rec.reconcile_all().orphaned_containers_cleaned == 0


# ── The simulation twin ──────────────────────────────────────────────────────


class TestMockReconciler:
    """``mock/reconciliation.py`` is what the e2e suite restarts against."""

    def test_the_default_scenario_reconciles_nothing_and_reports_no_errors(self):
        from spark_pulse.mock import reconciliation as mock_rec

        result = mock_rec.reconcile_all()

        assert result.clusters_reconciled == 0
        assert result.deployments_reconciled == 0
        assert result.orphaned_containers_cleaned == 0
        assert result.errors == []
        assert mock_rec.reconcile_clusters() == []
        assert mock_rec.reconcile_deployments() == []

    def test_the_orphaned_scenario_reports_containers_it_swept(self):
        from spark_pulse.mock.reconciliation import MockReconciler

        result = MockReconciler("orphaned").reconcile_all()

        assert result.orphaned_containers_cleaned == 3
        assert result.errors == []

    def test_the_partial_scenario_reports_both_passes_as_failed(self):
        from spark_pulse.mock.reconciliation import MockReconciler

        reconciler = MockReconciler("partial")
        result = reconciler.reconcile_all()

        assert len(result.errors) == 2
        assert reconciler.reconcile_clusters() == []
        assert reconciler.reconcile_deployments() == []
