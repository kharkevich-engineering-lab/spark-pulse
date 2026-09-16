"""Tests for the /api/mods router and the simulation twin behind it.

The router reads: it lists mods and returns one. Applying a mod is the deploy
path's job, and ``POST /validate`` — the last write-shaped endpoint here — went
with the UI that called it.

These tests pin the router against the mock deliberately: the mock is what the
e2e suite drives, so it is the twin whose API has to keep up.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spark_pulse.app import create_app
from spark_pulse.mock import mods as mock_mods
from spark_pulse.routers import mods as mods_router


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
