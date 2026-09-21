"""The event log: what the Runs panel's retention line has always promised.

The panel said "Events older than 30 days are archived. At most 1000 events
per resource." under a list that held only what the open browser tab had seen,
because nothing was stored at all. These are the tests for the store that
makes the sentence true — that every published deployment event is written,
that both halves of the retention are applied, and that a database which
cannot be written still lets the deploy proceed.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from spark_pulse.tools import event_log
from spark_pulse.tools.events import DeploymentEvent, EventType, severity_of


def _event(
    resource: str = "dep-1",
    event_type: EventType = EventType.DEPLOYMENT_PLANNED,
    message: str = "planned",
    when: datetime | None = None,
    metadata: dict | None = None,
) -> DeploymentEvent:
    return DeploymentEvent(
        event_type=event_type,
        resource=resource,
        resource_type="deployment",
        message=message,
        metadata=metadata or {},
        **({"timestamp": when} if when is not None else {}),
    )


@pytest.fixture(autouse=True)
def _forget_the_sweep_throttle(monkeypatch):
    """Each test gets a process that has never swept.

    The throttle is a module global, so without this the first test to sweep
    would suppress every later one and the age test would pass or fail
    depending on the order pytest happened to run them in.
    """
    monkeypatch.setattr(event_log, "_last_sweep", 0.0)


# ── Recording ───────────────────────────────────────────────────────────────


def test_an_event_is_written_and_read_back_in_the_stream_shape():
    """The row answers in the same shape the SSE frame carries.

    One parser on the page, not two: the panel seeds itself from this endpoint
    and then appends frames straight off the stream.
    """
    event = _event(message="planned qwen3 on vllm", metadata={"node": "node-b"})
    assert event_log.record(event) is True

    page = event_log.history("dep-1")

    assert page["total"] == 1
    assert page["events"] == [
        {
            "event_id": event.event_id,
            "timestamp": event.timestamp.isoformat(),
            "type": "deployment_planned",
            "message": "planned qwen3 on vllm",
            "resource": "dep-1",
            "resource_type": "deployment",
            "node": "node-b",
            "severity": "info",
        }
    ]


def test_the_event_carries_its_own_id_so_history_and_stream_agree():
    """The id is minted on the event, not on the row or in the browser.

    It is what lets the page tell "a frame I already have from the history"
    from "a frame that just happened" — the dedupe the seeded panel needs.
    """
    event = _event()
    event_log.record(event)

    stored = event_log.history("dep-1")["events"][0]

    assert stored["event_id"] == event.event_id == event.to_dict()["event_id"]


def test_a_failure_event_is_stored_as_an_error():
    """Severity is derived, so no publisher can forget to set it."""
    event_log.record(_event(event_type=EventType.DEPLOYMENT_ERROR, message="died"))

    assert event_log.history("dep-1")["events"][0]["severity"] == "error"
    assert severity_of(EventType.IMAGE_PULL_CANCELLED) == "warning"
    assert severity_of(EventType.DEPLOYMENT_READY) == "info"


def test_events_come_back_newest_first():
    for index in range(5):
        event_log.record(_event(message=f"event {index}"))

    messages = [e["message"] for e in event_log.history("dep-1")["events"]]

    assert messages == ["event 4", "event 3", "event 2", "event 1", "event 0"]


def test_one_run_never_sees_another_run_s_events():
    event_log.record(_event(resource="dep-1", message="mine"))
    event_log.record(_event(resource="dep-2", message="theirs"))

    page = event_log.history("dep-1")

    assert page["total"] == 1
    assert page["events"][0]["message"] == "mine"


def test_a_run_with_no_events_is_an_empty_page_not_an_error():
    assert event_log.history("never-deployed") == {
        "resource": "never-deployed",
        "events": [],
        "total": 0,
        "limit": event_log.DEFAULT_LIMIT,
    }


# ── Paging ──────────────────────────────────────────────────────────────────


def test_limit_bounds_the_page_but_not_the_total():
    for index in range(10):
        event_log.record(_event(message=f"event {index}"))

    page = event_log.history("dep-1", limit=3)

    assert [e["message"] for e in page["events"]] == ["event 9", "event 8", "event 7"]
    assert page["total"] == 10, "the count chip shows everything held, not one page"


def test_before_walks_backwards_through_the_timeline():
    # Inside the retention window on purpose: an event older than thirty days
    # is dropped by the sweep its own write triggers, which is the behaviour
    # the retention tests below pin.
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for index in range(4):
        event_log.record(
            _event(message=f"event {index}", when=base + timedelta(minutes=index))
        )

    older = event_log.history("dep-1", before=(base + timedelta(minutes=2)).isoformat())

    assert [e["message"] for e in older["events"]] == ["event 1", "event 0"]


def test_a_cursor_that_is_not_an_instant_returns_the_newest_page():
    """A truncated cursor should show the newest events, not fail the panel."""
    event_log.record(_event(message="only"))

    assert event_log.history("dep-1", before="not-a-timestamp")["total"] == 1


def test_a_limit_beyond_the_cap_is_capped():
    event_log.record(_event())

    assert event_log.history("dep-1", limit=10_000)["limit"] == event_log.MAX_LIMIT
    assert event_log.history("dep-1", limit=0)["limit"] == event_log.DEFAULT_LIMIT


# ── Retention ───────────────────────────────────────────────────────────────


def test_at_most_a_thousand_events_per_run_and_the_oldest_go_first(monkeypatch):
    """The footer's second sentence, at a size a test can afford."""
    monkeypatch.setattr(event_log, "MAX_PER_RESOURCE", 5)

    for index in range(8):
        event_log.record(_event(message=f"event {index}"))

    page = event_log.history("dep-1")

    assert page["total"] == 5
    assert [e["message"] for e in page["events"]] == [
        "event 7",
        "event 6",
        "event 5",
        "event 4",
        "event 3",
    ]


def test_trimming_one_run_leaves_another_run_alone(monkeypatch):
    monkeypatch.setattr(event_log, "MAX_PER_RESOURCE", 2)

    for index in range(4):
        event_log.record(_event(resource="dep-1", message=f"mine {index}"))
    event_log.record(_event(resource="dep-2", message="theirs"))

    assert event_log.history("dep-1")["total"] == 2
    assert event_log.history("dep-2")["total"] == 1


def test_nothing_older_than_thirty_days_survives_the_sweep():
    """The footer's first sentence."""
    now = datetime.now(timezone.utc)
    # The throttle is held closed so the writes themselves do not sweep: this
    # test is about what the sweep removes, not about when it runs.
    event_log._last_sweep = time.time()
    event_log.record(_event(message="ancient", when=now - timedelta(days=31)))
    event_log.record(_event(message="recent", when=now - timedelta(days=1)))

    assert event_log.sweep() == 1
    assert [e["message"] for e in event_log.history("dep-1")["events"]] == ["recent"]


def test_the_sweep_runs_on_a_write_rather_than_waiting_for_a_restart():
    """An operator reading the promise is owed it without a restart."""
    event_log._last_sweep = time.time()
    event_log.record(
        _event(message="ancient", when=datetime.now(timezone.utc) - timedelta(days=40))
    )
    assert event_log.history("dep-1")["total"] == 1

    event_log._last_sweep = 0.0
    event_log.record(_event(message="now"))

    assert [e["message"] for e in event_log.history("dep-1")["events"]] == ["now"]


def test_the_age_sweep_is_throttled_so_a_chatty_deploy_pays_once():
    moment = time.time()
    assert event_log.sweep_if_due(moment) == 0  # first call sweeps, finds nothing
    old = datetime.now(timezone.utc) - timedelta(days=90)
    event_log.record(_event(message="ancient", when=old))

    assert event_log.sweep_if_due(moment + 1) == 0, "swept again inside the interval"
    assert event_log.sweep_if_due(moment + event_log.AGE_SWEEP_INTERVAL + 1) == 1


def test_forgetting_a_run_drops_its_timeline_and_nothing_else():
    event_log.record(_event(resource="dep-1"))
    event_log.record(_event(resource="dep-2"))

    assert event_log.forget("dep-1") == 1
    assert event_log.history("dep-1")["total"] == 0
    assert event_log.history("dep-2")["total"] == 1


# ── A store that cannot be written ──────────────────────────────────────────


def test_a_database_that_refuses_the_write_does_not_raise(monkeypatch):
    """The deploy is the point; its history is not allowed to stop it."""

    def explode(*_args, **_kwargs):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(event_log, "session_scope", explode)

    assert event_log.record(_event()) is False


def test_retention_that_fails_still_leaves_the_event_recorded(monkeypatch):
    """A trim that cannot run loses a sweep, not the event somebody deployed."""

    def explode(*_args, **_kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(event_log, "trim", explode)

    assert event_log.record(_event()) is True
    assert event_log.history("dep-1")["total"] == 1


def test_publishing_still_broadcasts_when_the_log_cannot_be_written(monkeypatch):
    """`publish_event` records first and broadcasts regardless of the result."""
    import importlib

    native = importlib.import_module("spark_pulse.tools.native_runtime")

    def explode(*_args, **_kwargs):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(event_log, "session_scope", explode)

    # No listener, so there is nothing to deliver to — the assertion is that
    # publishing a deployment event over a broken log is not an exception.
    native.publish_event(EventType.DEPLOYMENT_READY, "dep-1", "serving")


def test_every_published_deployment_event_reaches_the_log():
    import importlib

    native = importlib.import_module("spark_pulse.tools.native_runtime")

    native.publish_event(EventType.DEPLOYMENT_PLANNED, "dep-9", "planned")
    native.publish_event(
        EventType.DEPLOYMENT_CONTAINER_STARTED,
        "dep-9",
        "container up",
        {"node": "node-b"},
    )
    native.publish_event(EventType.DEPLOYMENT_READY, "dep-9", "serving")

    page = event_log.history("dep-9")

    assert page["total"] == 3
    assert [e["type"] for e in page["events"]] == [
        "deployment_ready",
        "deployment_container_started",
        "deployment_planned",
    ]
    assert page["events"][1]["node"] == "node-b"
