"""A run's one state, derived once, on the server (#205).

A run's record carries two independent fields, and both are kept: ``status``
is the lifecycle (pulling, starting, running, stopped, error) and ``sync`` with
``sync_intent`` is convergence (a stop or a removal the nodes have not caught
up with, or a node that could not be asked). The split is deliberate — a page
must never say *stopped* about a container still holding 90 GB — but nothing
defined which combinations mean what, so every page combined them itself and
one of them showed **Running** and **Stopping…** side by side for one fact.

:func:`phase_of` is that combination, made once: every deployment the API
returns carries ``phase``, and a page renders it rather than the pair.

========== ==========================================================
phase      when
========== ==========================================================
queued     accepted and waiting (``pending``), nothing on a node yet
pulling    the image is on its way to a node
starting   containers launched, readiness not yet observed
running    readiness observed
stopping   a stop was asked for and the nodes have not confirmed it
stopped    the run ended and its containers are gone
deleting   the record itself is being cleared
error      the run failed; ``error_message`` says why
unknown    the lifecycle cannot be read (a state this build never wrote)
========== ==========================================================

A node that could not be asked (``sync: unknown``) does not change the phase —
the last thing the nodes said is still the best reading of the run — but the
phase is then *unverified*, and ``phase_reason`` says why. Unknown is not idle,
and it is not stopped either.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DELETING",
    "ERROR",
    "IllegalTransition",
    "TRANSITIONS",
    "check_transition",
    "transition_allowed",
    "PHASES",
    "PULLING",
    "QUEUED",
    "RUNNING",
    "SETTLING",
    "STARTING",
    "STOPPED",
    "STOPPING",
    "UNKNOWN",
    "phase_fields",
    "phase_of",
]

QUEUED = "queued"
PULLING = "pulling"
STARTING = "starting"
RUNNING = "running"
STOPPING = "stopping"
STOPPED = "stopped"
DELETING = "deleting"
ERROR = "error"
UNKNOWN = "unknown"

PHASES = (
    QUEUED,
    PULLING,
    STARTING,
    RUNNING,
    STOPPING,
    STOPPED,
    DELETING,
    ERROR,
    UNKNOWN,
)

#: Phases in which a change is already under way, so an action that would only
#: ask for it again is shut.
SETTLING = frozenset({STOPPING, DELETING})

#: The lifecycle's own words, and the phase each one is when nothing is in
#: flight. ``pending`` is what a run accepted but not yet started says.
_FROM_STATUS = {
    "pending": QUEUED,
    "queued": QUEUED,
    "pulling": PULLING,
    "starting": STARTING,
    "running": RUNNING,
    "stopped": STOPPED,
    "error": ERROR,
}

# The reconciler's vocabulary, repeated rather than imported: this module is
# pure and the reconciler starts threads. `tests/test_run_state.py` holds the
# two to the same strings.
_SYNC_IN_PROGRESS = "in_progress"
_SYNC_DELETING = "deleting"
_SYNC_UNKNOWN = "unknown"
_INTENT_STOP = "stop"


def phase_of(record: dict[str, Any]) -> str:
    """The one state of a run, from its record. Pure."""
    sync = str(record.get("sync") or "")
    if sync == _SYNC_DELETING:
        return DELETING
    if (
        sync == _SYNC_IN_PROGRESS
        and str(record.get("sync_intent") or "") == _INTENT_STOP
    ):
        return STOPPING
    status = str(record.get("status") or "").lower()
    return _FROM_STATUS.get(status, UNKNOWN)


def phase_fields(record: dict[str, Any]) -> dict[str, Any]:
    """``phase``, whether the nodes confirmed it, and the reason when not.

    ``phase_reason`` is the record's own words: the sync reason while a change
    is in flight or a node could not be asked, the error message for an error,
    and empty otherwise.
    """
    phase = phase_of(record)
    sync = str(record.get("sync") or "")
    verified = sync != _SYNC_UNKNOWN
    if sync in (_SYNC_IN_PROGRESS, _SYNC_DELETING, _SYNC_UNKNOWN):
        reason = str(record.get("sync_reason") or "")
    elif phase == ERROR:
        reason = str(record.get("error_message") or "")
    else:
        reason = ""
    return {"phase": phase, "phase_verified": verified, "phase_reason": reason}


# ── Transitions ──────────────────────────────────────────────────────────────
#
# Every write of a deployment record goes through `deployment_records._apply`
# (an existing row) or `_row_of` (a new one), and both ask `check_transition`
# whether the phase the write produces may follow the one stored. Staying in a
# phase is always allowed: most writes change a field that is not the phase.
#
# `unknown` goes both ways: a lifecycle this build cannot read may become
# anything, and anything may become one (a record adopted from a container
# nobody recorded, an older control plane's state).

#: Where a run may go from each phase. ``None`` is a record being created.
TRANSITIONS: dict[str | None, frozenset[str]] = {
    # Created as whatever it already is: queued or pulling for a new deploy,
    # starting or running for one adopted from its containers at startup,
    # stopped or error for a run whose containers were found gone or failed.
    None: frozenset({QUEUED, PULLING, STARTING, RUNNING, STOPPED, ERROR}),
    QUEUED: frozenset({PULLING, STARTING, RUNNING, STOPPING, STOPPED, ERROR}),
    PULLING: frozenset({STARTING, RUNNING, STOPPING, STOPPED, ERROR}),
    # Or back to pulling: the record is written as starting, and the image
    # then turns out not to be on the node.
    STARTING: frozenset({PULLING, RUNNING, STOPPING, STOPPED, ERROR}),
    # A run that is up goes down — asked to, or found gone — or fails; or it is
    # seen starting again, which is a new generation of the same run.
    RUNNING: frozenset({STARTING, STOPPING, STOPPED, ERROR}),
    # A stop the nodes finished, or failed; or one that turned out to have
    # nothing to stop (the run had already ended or failed on its own).
    STOPPING: frozenset({STOPPED, ERROR}),
    # A finished run is history: it is cleared, or a removal is asked of it.
    STOPPED: frozenset({DELETING, ERROR}),
    ERROR: frozenset({DELETING, STOPPED, STOPPING}),
    # A removal that could not finish leaves the record as it was.
    DELETING: frozenset({STOPPED, ERROR}),
}


class IllegalTransition(ValueError):
    """A write that would move a run between two phases the table does not join."""

    def __init__(self, deployment_id: str, before: str | None, after: str):
        self.deployment_id = deployment_id
        self.before = before
        self.after = after
        super().__init__(
            f"run {deployment_id or '?'} cannot go from "
            f"{before or 'nothing'} to {after}"
        )


def transition_allowed(before: str | None, after: str) -> bool:
    """Whether a run may go from phase ``before`` to ``after``. Pure.

    A removal may be asked of a run in any phase — *running · deleting* is a
    real situation, the containers going with the record — so ``deleting`` is
    reachable from every existing record rather than listed under each.
    """
    if before == after or UNKNOWN in (before, after):
        return True
    if after == DELETING and before is not None:
        return True
    return after in TRANSITIONS.get(before, frozenset())


def check_transition(
    before: dict[str, Any] | None, after: dict[str, Any]
) -> IllegalTransition | None:
    """The refusal for writing ``after`` over ``before``, or ``None``.

    Answers rather than raises, because what to do about one is the store's
    decision: refuse it under test, record it on a running control plane.
    """
    old = phase_of(before) if before is not None else None
    new = phase_of(after)
    if transition_allowed(old, new):
        return None
    return IllegalTransition(str(after.get("id") or ""), old, new)
