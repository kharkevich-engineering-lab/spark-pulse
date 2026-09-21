"""The event log: a run's timeline, after the page that watched it went away.

The expanded row on Runs has always had an *Event stream* panel, and under it
a line promising that events older than thirty days go and that at most a
thousand are kept per run. Neither half was true: the events came from
``/sse/events/deployments`` and nowhere else, so the panel held exactly what
had arrived since the browser tab was opened. A run deployed, pulled, started
and benchmarked an hour ago showed "No events to display · 0 events", and the
retention line described a store that did not exist.

This is that store. Every deployment event published by
:func:`spark_pulse.tools.native_runtime.publish_event` — which is also where
the reconciler's convergence frames go — is written here on the way to the
broadcaster, and ``GET /api/deployments/{id}/events`` reads it back.

Three things are load-bearing:

* **Recording never fails a publish.** A deploy that cannot write its own
  history is still a deploy; every failure here is logged and swallowed, and
  the broadcast happens either way. The log is a record of the work, not a
  participant in it.
* **The id comes from the event.** ``DeploymentEvent.event_id`` is minted
  once, broadcast in the SSE frame and stored on the row, so the page can seed
  itself from history and then append live frames without showing the frames
  that were already in the history twice.
* **Retention is what the footer says.** At most :data:`MAX_PER_RESOURCE`
  events per run, oldest dropped first, and nothing older than
  :data:`RETENTION_DAYS`. Both are applied on write — a count on an indexed
  column and, at most every :data:`AGE_SWEEP_INTERVAL` seconds, one delete —
  because an operator who reads the promise is entitled to it on a control
  plane that has never restarted.

Real-only, like ``deployment_records`` and ``scheduled_deploys``: it is a
database table and nothing about it differs between a real deploy and a
simulated one, so simulation records its own events in its own database rather
than standing in for them.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Float, Index, Integer, String, Text, delete, func, select
from sqlalchemy.orm import Mapped, mapped_column

from spark_pulse.db import Base, session_scope
from spark_pulse.tools.events import DeploymentEvent

logger = logging.getLogger(__name__)

#: How many events are kept for one run. The oldest go first.
MAX_PER_RESOURCE = 1000

#: How long an event is kept, whatever the count.
RETENTION_DAYS = 30

#: How often the age sweep runs, in seconds. Per process, not per write: the
#: cutoff moves by a second a second, so sweeping on every event would pay for
#: an indexed delete a hundred times a deploy to remove the same nothing.
AGE_SWEEP_INTERVAL = 300.0

#: What a read returns when no ``limit`` is given.
DEFAULT_LIMIT = 200

#: The most a single read returns, however large a ``limit`` is asked for.
MAX_LIMIT = 1000

_sweep_lock = threading.Lock()
_last_sweep = 0.0


class DeploymentEventRow(Base):
    """One event, as a row.

    ``seq`` rather than the event id as the primary key, because the ordering
    *is* the timeline: two events published inside the same microsecond (a
    plan and its first container, on a fast node) carry the same ISO timestamp
    to the digit, and a history sorted by timestamp alone would show them in
    whichever order the database felt like. An insertion sequence has no ties.
    """

    __tablename__ = "deployment_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: The id the event was minted with — the same one the SSE frame carries.
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    #: The deployment (or cluster) this event is about.
    resource: Mapped[str] = mapped_column(String(128), default="", index=True)
    resource_type: Mapped[str] = mapped_column(String(32), default="")
    event_type: Mapped[str] = mapped_column(String(64), default="", index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    node: Mapped[str] = mapped_column(String(255), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    #: The event's own time, ISO-8601 — what the page renders.
    timestamp: Mapped[str] = mapped_column(String(64), default="")
    #: The same instant as epoch seconds — what the sweep and ``before``
    #: compare against, because a string comparison of two ISO timestamps is
    #: an ordering only when both were formatted identically.
    recorded_at: Mapped[float] = mapped_column(Float, default=0.0, index=True)

    __table_args__ = (
        Index("ix_deployment_events_resource_recorded", "resource", "recorded_at"),
    )


def _epoch(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


#: A UTC offset that arrived as a space where its ``+`` should be.
#:
#: The cursor is an ISO-8601 timestamp handed back from a previous page and
#: sent again as a query parameter, and ``+`` in a query string decodes to a
#: space. A client that encodes it properly never gets here; one that does not
#: would otherwise have its cursor silently ignored and be served the newest
#: page for ever, which reads as a timeline that will not scroll.
_SPACED_OFFSET = re.compile(r" (\d{2}:\d{2})$")


def _parse(moment: str) -> float | None:
    """An ISO-8601 instant as epoch seconds, or ``None`` if it is not one."""
    for candidate in (moment, _SPACED_OFFSET.sub(r"+\1", moment or "")):
        try:
            return _epoch(datetime.fromisoformat(candidate))
        except (TypeError, ValueError):
            continue
    return None


def frame_of(row: DeploymentEventRow) -> dict[str, Any]:
    """The shape the SSE stream carries, so the page has one parser."""
    return {
        "event_id": row.event_id,
        "timestamp": row.timestamp,
        "type": row.event_type,
        "message": row.message,
        "resource": row.resource,
        "resource_type": row.resource_type,
        "node": row.node,
        "severity": row.severity,
    }


def record(event: DeploymentEvent) -> bool:
    """Write ``event`` to the log. ``False`` if it could not be written.

    Never raises: the caller is on the deploy path, and a history that cannot
    be written must not become a deploy that cannot be made.
    """
    try:
        with session_scope() as db:
            db.add(
                DeploymentEventRow(
                    event_id=event.event_id,
                    resource=event.resource,
                    resource_type=event.resource_type,
                    event_type=event.event_type.value,
                    severity=event.severity,
                    node=event.node[:255],
                    message=event.message,
                    timestamp=event.timestamp.isoformat(),
                    recorded_at=_epoch(event.timestamp),
                )
            )
    except Exception as exc:
        logger.debug("could not record event %s: %s", event.event_id, exc)
        return False

    try:
        trim(event.resource)
        sweep_if_due()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("could not apply event retention for %s: %s", event.resource, exc)
    return True


def trim(resource: str) -> int:
    """Drop this resource's oldest events beyond :data:`MAX_PER_RESOURCE`."""
    if not resource:
        return 0
    with session_scope() as db:
        held = (
            db.execute(
                select(func.count())
                .select_from(DeploymentEventRow)
                .where(DeploymentEventRow.resource == resource)
            ).scalar()
            or 0
        )
        excess = held - MAX_PER_RESOURCE
        if excess <= 0:
            return 0
        doomed = (
            db.execute(
                select(DeploymentEventRow.seq)
                .where(DeploymentEventRow.resource == resource)
                .order_by(DeploymentEventRow.seq.asc())
                .limit(excess)
            )
            .scalars()
            .all()
        )
        if not doomed:
            return 0
        db.execute(delete(DeploymentEventRow).where(DeploymentEventRow.seq.in_(doomed)))
        return len(doomed)


def sweep(now: float | None = None) -> int:
    """Drop every event older than :data:`RETENTION_DAYS`. Returns how many."""
    cutoff = (time.time() if now is None else now) - RETENTION_DAYS * 86400
    with session_scope() as db:
        doomed = (
            db.execute(
                select(DeploymentEventRow.seq).where(
                    DeploymentEventRow.recorded_at < cutoff
                )
            )
            .scalars()
            .all()
        )
        if not doomed:
            return 0
        db.execute(delete(DeploymentEventRow).where(DeploymentEventRow.seq.in_(doomed)))
        return len(doomed)


def sweep_if_due(now: float | None = None) -> int:
    """The age sweep, at most once every :data:`AGE_SWEEP_INTERVAL` seconds."""
    global _last_sweep
    moment = time.time() if now is None else now
    with _sweep_lock:
        if _last_sweep and moment - _last_sweep < AGE_SWEEP_INTERVAL:
            return 0
        _last_sweep = moment
    return sweep(moment)


def history(
    resource: str,
    limit: int = DEFAULT_LIMIT,
    before: str | None = None,
) -> dict[str, Any]:
    """One run's events, newest first, with how many are held in all.

    ``before`` is an ISO-8601 instant: only events strictly older than it come
    back, which is how the page walks backwards through a long timeline. A
    value that is not an instant is ignored rather than refused — a truncated
    cursor should return the newest page, not an error.
    """
    capped = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    cutoff = _parse(before) if before else None

    with session_scope() as db:
        total = (
            db.execute(
                select(func.count())
                .select_from(DeploymentEventRow)
                .where(DeploymentEventRow.resource == resource)
            ).scalar()
            or 0
        )
        query = select(DeploymentEventRow).where(
            DeploymentEventRow.resource == resource
        )
        if cutoff is not None:
            query = query.where(DeploymentEventRow.recorded_at < cutoff)
        rows = (
            db.execute(query.order_by(DeploymentEventRow.seq.desc()).limit(capped))
            .scalars()
            .all()
        )
        return {
            "resource": resource,
            "events": [frame_of(row) for row in rows],
            "total": int(total),
            "limit": capped,
        }


def forget(resource: str) -> int:
    """Drop one resource's events. For when its record itself is cleared."""
    with session_scope() as db:
        result = db.execute(
            delete(DeploymentEventRow).where(DeploymentEventRow.resource == resource)
        )
        return int(result.rowcount or 0)
