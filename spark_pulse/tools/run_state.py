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
