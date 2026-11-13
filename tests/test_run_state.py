"""A run's one phase, derived from the fields its record keeps apart (#205)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spark_pulse import tools
from spark_pulse.tools import reconciler, run_state
from spark_pulse.tools.run_state import (
    DELETING,
    ERROR,
    PHASES,
    PULLING,
    QUEUED,
    RUNNING,
    STARTING,
    STOPPED,
    STOPPING,
    UNKNOWN,
    phase_fields,
    phase_of,
)


@pytest.mark.parametrize(
    ("record", "phase"),
    [
        ({"status": "pending"}, QUEUED),
        ({"status": "pulling"}, PULLING),
        ({"status": "starting"}, STARTING),
        ({"status": "running"}, RUNNING),
        ({"status": "stopped"}, STOPPED),
        ({"status": "error"}, ERROR),
        ({"status": "Running"}, RUNNING),
        ({"status": "quiescing"}, UNKNOWN),
        ({}, UNKNOWN),
        # Settled records, and records from before `sync` existed, are their
        # lifecycle.
        ({"status": "running", "sync": "in_sync"}, RUNNING),
        # The case the lab showed as two chips: one fact, one phase.
        ({"status": "running", "sync": "in_progress", "sync_intent": "stop"}, STOPPING),
        ({"status": "pulling", "sync": "in_progress", "sync_intent": "stop"}, STOPPING),
        # The record being cleared, whatever its lifecycle said last.
        ({"status": "stopped", "sync": "deleting"}, DELETING),
        ({"status": "running", "sync": "deleting"}, DELETING),
        # In progress with no intent it names is still its lifecycle.
        ({"status": "running", "sync": "in_progress"}, RUNNING),
        # A node that could not be asked does not make a run stopped or idle.
        ({"status": "running", "sync": "unknown"}, RUNNING),
    ],
)
def test_every_combination_reads_as_one_phase(record, phase):
    assert phase_of(record) == phase
    assert phase in PHASES


def test_an_unverified_phase_says_so_and_why():
    fields = phase_fields(
        {
            "status": "running",
            "sync": "unknown",
            "sync_reason": "spark-02 did not answer",
        }
    )
    assert fields == {
        "phase": RUNNING,
        "phase_verified": False,
        "phase_reason": "spark-02 did not answer",
    }


def test_a_change_in_flight_carries_its_reason():
    fields = phase_fields(
        {
            "status": "running",
            "sync": "in_progress",
            "sync_intent": "stop",
            "sync_reason": "stop requested",
        }
    )
    assert fields["phase"] == STOPPING
    assert fields["phase_verified"] is True
    assert fields["phase_reason"] == "stop requested"


def test_an_error_carries_its_message_and_a_settled_run_nothing():
    assert (
        phase_fields({"status": "error", "error_message": "OOM"})["phase_reason"]
        == "OOM"
    )
    assert phase_fields({"status": "running"})["phase_reason"] == ""


def test_the_vocabulary_is_the_reconcilers():
    """Repeated rather than imported, so held to the same strings here."""
    assert run_state._SYNC_IN_PROGRESS == reconciler.SYNC_IN_PROGRESS
    assert run_state._SYNC_DELETING == reconciler.SYNC_DELETING
    assert run_state._SYNC_UNKNOWN == reconciler.SYNC_UNKNOWN
    assert run_state._INTENT_STOP == reconciler.INTENT_STOP


def test_settling_is_a_change_already_under_way():
    assert run_state.SETTLING == {STOPPING, DELETING}


class TestServedRecords:
    """Every deployment the API returns says its phase, old records included."""

    def test_the_listing_fills_the_phase_on_the_way_out(self, monkeypatch):
        stored = {
            "id": "a",
            "created_at": "1",
            "status": "running",
            "sync": "in_progress",
            "sync_intent": "stop",
        }
        monkeypatch.setattr(tools.native_runtime, "list_deployments", lambda: [stored])
        listed = tools.deploy_dispatch.list_deployments()
        assert listed[0]["phase"] == STOPPING
        # Derived, never written back.
        assert "phase" not in stored

    def test_a_record_from_before_sync_existed_is_its_lifecycle(self, monkeypatch):
        monkeypatch.setattr(tools.deployment_records, "get", lambda _id: {"id": "a"})
        monkeypatch.setattr(
            tools.native_runtime, "status", lambda _id: {"id": "a", "status": "stopped"}
        )
        assert tools.deploy_dispatch.get_deployment("a")["phase"] == STOPPED

    def test_the_api_returns_it(self, monkeypatch):
        from spark_pulse.app import create_app

        monkeypatch.setattr(
            tools.native_runtime,
            "list_deployments",
            lambda: [{"id": "a", "created_at": "1", "status": "pulling"}],
        )
        body = TestClient(create_app()).get("/api/deployments").json()
        rows = body if isinstance(body, list) else body.get("deployments", body)
        assert rows[0]["phase"] == PULLING
        assert rows[0]["phase_verified"] is True
