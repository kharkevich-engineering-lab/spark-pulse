"""The thread that makes a recorded intent true.

A delete used to tear every rank down on the request's own thread: head first,
one node at a time, and on a node that had stopped answering, for as long as
the transport waits. The decision was made the moment the request arrived —
what the browser held a spinner over was somebody else's timeout.

So the request records the intent and the reconciler converges. What these
tests hold is the part that makes that safe rather than merely fast:

* a record is never dropped while a rank might still be holding its ports;
* a node that cannot be asked leaves the record alone and says so, rather than
  having a state inferred from its silence;
* the loop survives a sweep that raises, because a background thread that dies
  on one bad record stops converging every other one.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from spark_pulse import tools
from spark_pulse.tools import reconciler as rc


@pytest.fixture
def records(tmp_path):
    """A private record store, and no process-wide reconciler running."""
    rc.stop_reconciler()
    yield
    rc.stop_reconciler()


def _record(**fields):
    """Write one deployment record straight to the store."""
    base = {
        "id": "d1",
        "name": "test",
        "recipe_id": "qwen3-8b",
        "runtime": "native",
        "status": "running",
        "container_name": "spark-pulse-d1-r0-g1",
    }
    base.update(fields)
    tools.deployment_records.upsert(base)
    return base


# ── The two states, kept apart ───────────────────────────────────────────────


class TestSyncState:
    def test_a_record_written_before_this_existed_is_settled(self):
        """No `sync` field means whatever was done to it finished inline."""
        assert rc.sync_state({"id": "d1", "status": "running"}) == rc.SYNC_OK

    def test_a_value_nobody_recognises_is_read_as_settled(self):
        """A record from a newer build must not park the sweep on a state this
        one cannot advance."""
        assert rc.sync_state({"id": "d1", "sync": "quantum"}) == rc.SYNC_OK

    @pytest.mark.parametrize("state", rc.SYNC_STATES)
    def test_every_named_state_survives_a_round_trip(self, state, records):
        _record()

        rc.mark("d1", state)

        assert rc.sync_state(tools.deployment_records.get("d1")) == state

    def test_marking_a_state_nobody_defined_is_refused(self, records):
        _record()

        with pytest.raises(ValueError, match="unknown sync state"):
            rc.mark("d1", "half-deleted")

    def test_lifecycle_and_convergence_are_different_questions(self, records):
        """*running · deleting* is a real situation, and a UI that collapses
        the two into one word says "stopped" about a container still holding
        90 GB of VRAM."""
        _record(status="running")

        rc.mark("d1", rc.SYNC_DELETING, "removal requested")

        record = tools.deployment_records.get("d1")
        assert record["status"] == "running"
        assert record["sync"] == "deleting"
        assert record["sync_reason"] == "removal requested"


# ── Converging ───────────────────────────────────────────────────────────────


class TestSweep:
    def test_a_settled_record_is_left_alone(self, records):
        _record()

        assert rc.Reconciler().sweep() == []

    def test_a_deleting_record_is_torn_down_and_dropped(self, records):
        _record()
        rc.mark("d1", rc.SYNC_DELETING)

        touched = rc.Reconciler().sweep()

        assert touched == [{"id": "d1", "deleted": True}]
        assert tools.deployment_records.get("d1") is None

    def test_a_rank_that_could_not_be_confirmed_gone_keeps_the_record(self, records):
        """Dropping it would free that node's ports on inference while a
        container still holds them — the orphan bug the plan warns about."""
        _record()
        rc.mark("d1", rc.SYNC_DELETING)

        with patch.object(
            tools.deploy_dispatch, "delete_deployment", return_value=False
        ):
            rc.Reconciler().sweep()

        record = tools.deployment_records.get("d1")
        assert record is not None
        assert record["sync"] == rc.SYNC_DELETING
        assert "could not be confirmed gone" in record["sync_reason"]

    def test_a_node_that_will_not_answer_is_reported_not_inferred(self, records):
        _record()
        rc.mark("d1", rc.SYNC_DELETING)

        with patch.object(
            tools.deploy_dispatch,
            "delete_deployment",
            side_effect=RuntimeError("node 10.0.0.2 is unreachable"),
        ):
            rc.Reconciler().sweep()

        record = tools.deployment_records.get("d1")
        assert record["sync"] == rc.SYNC_DELETING
        assert "unreachable" in record["sync_reason"]

    def test_the_sweep_keeps_going_after_one_bad_record(self, records):
        """One deployment nobody can converge must not stop the others."""
        _record(id="bad")
        _record(id="good")
        rc.mark("bad", rc.SYNC_DELETING)
        rc.mark("good", rc.SYNC_DELETING)

        calls = []

        def _delete(deployment_id, *_a, **_kw):
            calls.append(deployment_id)
            if deployment_id == "bad":
                raise RuntimeError("no")
            return True

        with patch.object(tools.deploy_dispatch, "delete_deployment", _delete):
            touched = rc.Reconciler().sweep()

        assert sorted(calls) == ["bad", "good"], "the sweep stopped at the first"
        assert {"id": "good", "deleted": True} in touched
        # The one that raised keeps its state and says what it is waiting for,
        # so the next sweep tries again.
        assert tools.deployment_records.get("bad")["sync"] == rc.SYNC_DELETING

    def test_a_stop_ends_the_containers_and_keeps_the_record(self, records):
        """The distinction the router carries: a stop is not a removal.

        The record of a finished run is history an operator reads — which
        ports it held, which ranks came up, why it ended. Converging a stop by
        dropping it would delete the thing they asked to stop.
        """
        _record(status="running")
        rc.mark("d1", rc.SYNC_IN_PROGRESS, "stop requested", rc.INTENT_STOP)

        with patch.object(
            tools.deploy_dispatch, "stop_deployment", return_value={"id": "d1"}
        ) as stop:
            rc.Reconciler().sweep()

        stop.assert_called_once_with("d1")
        record = tools.deployment_records.get("d1")
        assert record is not None
        assert record["sync"] == rc.SYNC_OK
        # And the intent is spent: the next sweep must not stop it again.
        assert record["sync_intent"] == ""

    def test_a_stop_that_could_not_reach_a_node_keeps_its_intent(self, records):
        _record(status="running")
        rc.mark("d1", rc.SYNC_IN_PROGRESS, "stop requested", rc.INTENT_STOP)

        with patch.object(
            tools.deploy_dispatch,
            "stop_deployment",
            side_effect=RuntimeError("node 10.0.0.2 is unreachable"),
        ):
            rc.Reconciler().sweep()

        record = tools.deployment_records.get("d1")
        assert record["sync"] == rc.SYNC_IN_PROGRESS
        assert record["sync_intent"] == rc.INTENT_STOP
        assert "unreachable" in record["sync_reason"]

    def test_a_stop_of_a_record_that_went_away_is_not_an_error(self, records):
        _record(status="running")
        rc.mark("d1", rc.SYNC_IN_PROGRESS, "stop requested", rc.INTENT_STOP)

        with patch.object(tools.deploy_dispatch, "stop_deployment", return_value=None):
            touched = rc.Reconciler().sweep()

        assert touched == [{"id": "d1", "deleted": True}]

    def test_an_in_progress_record_settles_once_the_nodes_agree(self, records):
        _record(status="running")
        rc.mark("d1", rc.SYNC_IN_PROGRESS)

        rc.Reconciler().sweep()

        assert tools.deployment_records.get("d1")["sync"] == rc.SYNC_OK

    def test_a_record_still_pulling_is_left_to_the_pull_thread(self, records):
        """The download owns that record until it finishes; a second writer
        would race it."""
        _record(status="pulling")
        rc.mark("d1", rc.SYNC_IN_PROGRESS)

        rc.Reconciler().sweep()

        assert tools.deployment_records.get("d1")["sync"] == rc.SYNC_IN_PROGRESS

    def test_a_node_that_cannot_be_asked_leaves_the_record_unknown(self, records):
        _record(status="running")
        rc.mark("d1", rc.SYNC_IN_PROGRESS)

        with patch.object(
            tools.reconciliation,
            "reconcile_deployments",
            side_effect=RuntimeError("no route to host"),
        ):
            rc.Reconciler().sweep()

        record = tools.deployment_records.get("d1")
        assert record["sync"] == rc.SYNC_UNKNOWN
        assert "no route to host" in record["sync_reason"]


# ── The thread ───────────────────────────────────────────────────────────────


class TestTheLoop:
    def test_it_starts_and_stops(self, records):
        reconciler = rc.Reconciler(interval=0.05)

        reconciler.start()
        assert reconciler.running is True
        reconciler.stop()

        assert reconciler.running is False

    def test_starting_twice_runs_one_thread(self, records):
        reconciler = rc.Reconciler(interval=0.05)
        try:
            reconciler.start()
            reconciler.start()

            named = [t for t in threading.enumerate() if t.name == "reconciler"]
            assert len(named) == 1
        finally:
            reconciler.stop()

    def test_a_nudge_sweeps_without_waiting_for_the_tick(self, records):
        """A delete on one healthy node still has to look instant, or the
        asynchronous version feels slower than what it replaced."""
        reconciler = rc.Reconciler(interval=60)
        swept = threading.Event()
        original = reconciler.sweep

        def _sweep():
            result = original()
            swept.set()
            return result

        reconciler.sweep = _sweep  # type: ignore[method-assign]
        try:
            reconciler.start()
            swept.wait(timeout=2)
            swept.clear()

            reconciler.nudge()

            assert swept.wait(timeout=2), "a nudge did not wake the loop"
        finally:
            reconciler.stop()

    def test_a_sweep_that_raises_does_not_kill_the_loop(self, records):
        """A thread that dies on one bad record stops converging every other."""
        reconciler = rc.Reconciler(interval=0.05)
        attempts = []

        def _sweep():
            attempts.append(1)
            raise RuntimeError("boom")

        reconciler.sweep = _sweep  # type: ignore[method-assign]
        try:
            reconciler.start()
            for _ in range(100):
                if len(attempts) >= 2:
                    break
                threading.Event().wait(0.02)

            assert len(attempts) >= 2, "the loop stopped after one failure"
            assert reconciler.running is True
        finally:
            reconciler.stop()

    def test_nudging_when_nothing_runs_is_harmless(self):
        """A CLI process that marks a record has no reconciler; the next
        control plane to start sweeps what it left behind."""
        rc.stop_reconciler()

        rc.nudge()  # must not raise
