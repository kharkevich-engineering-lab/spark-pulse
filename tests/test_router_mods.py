"""Tests for the /api/mods router and the simulation twin behind it.

The router had no tests, and neither did ``mock/mods.py``. Between them sat
three defects that no test could have missed had either been exercised: the
mock never defined ``ModOrchestrator``/``ModDeployment`` at all, the router
reached past the simulation switch with a function-level
``from spark_pulse.tools.mods import ModDeployment`` (which rebinds
``tools.mods`` to the real module for the rest of the process), and it handed
the orchestrator a raw JSON dict where an object with ``.head``/``.workers``
was expected — so apply and rollback could not return 200 for any input.

These tests pin the router against the mock deliberately: the mock is what the
e2e suite drives, so it is the twin whose API has to keep up.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spark_pulse.app import create_app
from spark_pulse.mock import mods as mock_mods
from spark_pulse.routers import mods as mods_router

CLUSTER = {
    "head": {"ip": "10.0.0.1", "container_name": "head-container"},
    "workers": [
        {"ip": "10.0.0.2", "container_name": "worker0"},
        {"ip": "10.0.0.3", "container_name": "worker1"},
    ],
}


@pytest.fixture
def client(monkeypatch):
    """The router bound to the simulation twin, whatever else ran first.

    ``routers/mods.py`` captures ``tools.mods`` at import time, so which module
    it holds depends on collection order. Pinning it makes these tests say what
    they mean: this is the contract the mock has to satisfy.
    """
    monkeypatch.setattr(mods_router, "mods", mock_mods)
    with TestClient(create_app()) as test_client:
        yield test_client


# ── Listing and detail ───────────────────────────────────────────────────────


class TestListMods:
    def test_the_simulated_mods_are_listed(self, client):
        listed = client.get("/api/mods").json()

        assert [m["id"] for m in listed] == [
            "fix-qwen3.5-autoround",
            "tuning-benchmark",
            "nccl-optimization",
        ]

    def test_each_entry_carries_the_fields_the_page_renders(self, client):
        entry = client.get("/api/mods").json()[0]

        assert entry["description"]
        assert entry["has_patches"] is True
        assert {"name": "fix-quant.patch", "kind": "patch"} in entry["files"]


class TestGetMod:
    def test_detail_includes_the_script(self, client):
        detail = client.get("/api/mods/nccl-optimization").json()

        assert detail["id"] == "nccl-optimization"
        assert detail["script"].startswith("#!/bin/bash")

    def test_an_unknown_mod_is_a_404_naming_it(self, client):
        response = client.get("/api/mods/nope")

        assert response.status_code == 404
        assert "nope" in response.json()["detail"]


# ── Validation ───────────────────────────────────────────────────────────────


class TestValidateMod:
    def test_a_path_is_required(self, client):
        response = client.post("/api/mods/validate", json={})

        assert response.status_code == 400
        assert response.json()["detail"] == "path is required"

    def test_a_plain_mod_validates_clean(self, client):
        response = client.post("/api/mods/validate", json={"path": "/mods/plain"})

        assert response.status_code == 200
        assert response.json() == {"healthy": True, "warnings": [], "errors": []}

    def test_a_dangerous_mod_reports_its_errors_and_warnings(self, client):
        body = client.post(
            "/api/mods/validate", json={"path": "/mods/dangerous-thing"}
        ).json()

        assert body["healthy"] is False
        assert body["errors"] and body["warnings"]

    def test_a_mod_reaching_the_network_is_healthy_but_warned_about(self, client):
        body = client.post("/api/mods/validate", json={"path": "/mods/network"}).json()

        assert body["healthy"] is True
        assert body["warnings"] == ["run.sh uses network access (curl/wget)"]

    def test_a_validator_that_blows_up_becomes_a_500(self, client, monkeypatch):
        def boom(_path):
            raise RuntimeError("scanner unavailable")

        monkeypatch.setattr(mock_mods, "validate_mod_content", boom)

        response = client.post("/api/mods/validate", json={"path": "/mods/x"})

        assert response.status_code == 500
        assert response.json()["detail"] == "scanner unavailable"


# ── Apply ────────────────────────────────────────────────────────────────────


class TestApplyMod:
    def test_the_mod_is_applied_to_head_and_workers(self, client):
        body = client.post(
            "/api/mods/apply",
            json={
                "mod_name": "nccl",
                "mod_path": "/mods/nccl",
                "target": "all",
                "cluster_state": CLUSTER,
            },
        ).json()

        assert body["mod_name"] == "nccl"
        assert body["target"] == "all"
        assert body["completed_nodes"] == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
        assert body["failed_nodes"] == []

    def test_targeting_the_head_leaves_the_workers_alone(self, client):
        body = client.post(
            "/api/mods/apply",
            json={
                "mod_name": "nccl",
                "mod_path": "/mods/nccl",
                "target": "head",
                "cluster_state": CLUSTER,
            },
        ).json()

        assert body["completed_nodes"] == ["10.0.0.1"]

    def test_targeting_the_workers_leaves_the_head_alone(self, client):
        body = client.post(
            "/api/mods/apply",
            json={
                "mod_name": "nccl",
                "mod_path": "/mods/nccl",
                "target": "workers",
                "cluster_state": CLUSTER,
            },
        ).json()

        assert body["completed_nodes"] == ["10.0.0.2", "10.0.0.3"]

    @pytest.mark.parametrize(
        "body",
        [
            {"mod_path": "/mods/nccl"},
            {"mod_name": "nccl"},
            {"mod_name": "", "mod_path": ""},
        ],
    )
    def test_the_name_and_the_path_are_both_required(self, client, body):
        response = client.post(
            "/api/mods/apply", json=body | {"cluster_state": CLUSTER}
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "mod_name and mod_path are required"

    def test_an_unknown_target_is_refused(self, client):
        response = client.post(
            "/api/mods/apply",
            json={
                "mod_name": "n",
                "mod_path": "/p",
                "target": "everything",
                "cluster_state": CLUSTER,
            },
        )

        assert response.status_code == 400
        assert "head" in response.json()["detail"]

    def test_a_missing_cluster_state_is_a_400_rather_than_a_500(self, client):
        response = client.post(
            "/api/mods/apply", json={"mod_name": "n", "mod_path": "/p"}
        )

        assert response.status_code == 400
        assert response.json()["detail"] == (
            "cluster_state with a 'head' node is required"
        )

    def test_a_head_without_an_address_is_named_in_the_400(self, client):
        response = client.post(
            "/api/mods/apply",
            json={
                "mod_name": "n",
                "mod_path": "/p",
                "cluster_state": {"head": {"container_name": "c"}},
            },
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "cluster_state.head needs an 'ip'"

    def test_a_worker_without_an_address_is_named_by_index(self, client):
        response = client.post(
            "/api/mods/apply",
            json={
                "mod_name": "n",
                "mod_path": "/p",
                "cluster_state": {
                    "head": {"ip": "10.0.0.1"},
                    "workers": [{"ip": "10.0.0.2"}, {}],
                },
            },
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "cluster_state.workers[1] needs an 'ip'"

    def test_workers_must_be_a_list(self, client):
        response = client.post(
            "/api/mods/apply",
            json={
                "mod_name": "n",
                "mod_path": "/p",
                "cluster_state": {"head": {"ip": "10.0.0.1"}, "workers": "10.0.0.2"},
            },
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "cluster_state.workers must be a list"

    def test_an_orchestrator_that_blows_up_becomes_a_500(self, client, monkeypatch):
        class Exploding(mock_mods.ModOrchestrator):
            def apply_mod_cluster(self, mod_deployment, cluster_state):
                raise RuntimeError("ssh is down")

        monkeypatch.setattr(mock_mods, "ModOrchestrator", Exploding)

        response = client.post(
            "/api/mods/apply",
            json={"mod_name": "n", "mod_path": "/p", "cluster_state": CLUSTER},
        )

        assert response.status_code == 500
        assert response.json()["detail"] == "ssh is down"


# ── Rollback ─────────────────────────────────────────────────────────────────


class TestRollbackMod:
    def test_only_the_nodes_that_took_the_mod_are_rolled_back(self, client):
        body = client.post(
            "/api/mods/rollback",
            json={
                "mod_name": "nccl",
                "mod_path": "/mods/nccl",
                "target": "all",
                "completed_nodes": ["10.0.0.1", "10.0.0.3"],
                "cluster_state": CLUSTER,
            },
        ).json()

        assert body == {"rolled_back_nodes": ["10.0.0.1", "10.0.0.3"]}

    def test_nothing_completed_means_nothing_to_roll_back(self, client):
        body = client.post(
            "/api/mods/rollback",
            json={
                "mod_name": "nccl",
                "mod_path": "/mods/nccl",
                "cluster_state": CLUSTER,
            },
        ).json()

        assert body == {"rolled_back_nodes": []}

    def test_the_name_and_the_path_are_both_required(self, client):
        response = client.post(
            "/api/mods/rollback", json={"mod_name": "", "cluster_state": CLUSTER}
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "mod_name and mod_path are required"

    def test_an_unknown_target_is_refused(self, client):
        response = client.post(
            "/api/mods/rollback",
            json={
                "mod_name": "n",
                "mod_path": "/p",
                "target": "everything",
                "cluster_state": CLUSTER,
            },
        )

        assert response.status_code == 400

    def test_a_missing_cluster_state_is_a_400_rather_than_a_500(self, client):
        response = client.post(
            "/api/mods/rollback", json={"mod_name": "n", "mod_path": "/p"}
        )

        assert response.status_code == 400

    def test_an_orchestrator_that_blows_up_becomes_a_500(self, client, monkeypatch):
        class Exploding(mock_mods.ModOrchestrator):
            def rollback_mod(self, mod_deployment, cluster_state):
                raise RuntimeError("ssh is down")

        monkeypatch.setattr(mock_mods, "ModOrchestrator", Exploding)

        response = client.post(
            "/api/mods/rollback",
            json={"mod_name": "n", "mod_path": "/p", "cluster_state": CLUSTER},
        )

        assert response.status_code == 500
        assert response.json()["detail"] == "ssh is down"
