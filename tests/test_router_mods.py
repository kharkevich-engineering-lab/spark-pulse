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
