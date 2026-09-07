"""Converging what the operator asked for with what the nodes have.

Every mutating call used to do the work on the request's own thread: a delete
stopped every rank, head first, waiting on each node in turn, and only then
answered. On one machine that is fast enough to look synchronous. On four it is
as slow as the slowest node, and on a node that has stopped answering it is as
slow as the transport timeout — with the browser holding a spinner over a
decision that was already made.

So the request records the *intent* and returns, and this thread makes it true.

Two states, kept apart on purpose:

* ``status`` is the lifecycle — running, pulling, stopped, error. It answers
  "what is this deployment".
* ``sync`` is convergence — ``in_sync``, ``in_progress``, ``deleting``,
  ``unknown``. It answers "has what you asked for happened yet".

An operator reads both: *running · in sync* and *running · deleting* are
different situations, and collapsing them into one word is what makes a UI
that says "stopped" about a container still holding 90 GB of VRAM.

``unknown`` is the third state this codebase keeps insisting on. A node that
cannot be asked has not said no. The reconciler leaves the record alone and
says so, rather than inferring a state from silence — the same rule
``reconcile_deployments`` follows for ranks it could not enumerate.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from spark_pulse import tools
from spark_pulse.tools.events import EventType

logger = logging.getLogger(__name__)


def _publish(event: EventType, deployment_id: str, message: str) -> None:
    """Emit on the deployment stream the UI is already listening to.

    Imported at call time rather than at module scope: ``native_runtime``
    imports this module's constants, and the publisher lives there.
    """
    from spark_pulse.tools.native_runtime import publish_event

    try:
        publish_event(event, deployment_id, message)
    except Exception as exc:  # pragma: no cover — a stream nobody is reading
        logger.debug("could not publish %s for %s: %s", event, deployment_id, exc)


#: Settled: the nodes hold what the record says they hold.
SYNC_OK = "in_sync"
#: A change is being applied — starting, stopping, pulling.
SYNC_IN_PROGRESS = "in_progress"
#: Removal was asked for; the containers are not gone yet.
SYNC_DELETING = "deleting"
#: A node could not be asked. Not a failure, and not a state to act on.
SYNC_UNKNOWN = "unknown"

SYNC_STATES = (SYNC_OK, SYNC_IN_PROGRESS, SYNC_DELETING, SYNC_UNKNOWN)

#: What an in-progress record is being changed *into*.
#:
#: Stopping a running deployment and clearing a finished one out of history are
#: different operations that happen to share a verb in the UI and a method on
#: the router. Only the second one drops the record, so the reconciler cannot
#: work out which was asked for from the state alone. A record marked
#: ``deleting`` needs no intent: that state only ever means the record goes.
INTENT_STOP = "stop"

#: How often the loop sweeps. Fast enough that a delete looks immediate, slow
#: enough that a four-node cluster is not being interrogated continuously.
SWEEP_INTERVAL_SECONDS = 5.0


def sync_state(record: dict[str, Any]) -> str:
    """The convergence state of one record, defaulting to settled.

    Records written before this existed carry no ``sync`` field. They are
    settled by definition: whatever the control plane last did to them
    finished on the request's own thread.
    """
    value = str(record.get("sync") or SYNC_OK)
    return value if value in SYNC_STATES else SYNC_OK


def mark(
    deployment_id: str,
    state: str,
    reason: str = "",
    intent: str | None = None,
) -> dict[str, Any] | None:
    """Record an intent and return at once.

    The caller has decided; this makes the decision visible to every reader
    before any node has been asked anything. Marking without an ``intent``
    clears whatever the last one was, which is how a settled record stops
    carrying the change it has finished making.
    """
    if state not in SYNC_STATES:
        raise ValueError(f"unknown sync state: {state}")
    updated = tools.deployment_records.update(
        deployment_id, sync=state, sync_reason=reason, sync_intent=intent or ""
    )
    if updated is not None:
        _publish(EventType.DEPLOYMENT_SYNC, deployment_id, reason or f"sync: {state}")
    return updated


class Reconciler:
    """One background thread converging every unsettled deployment.

    Modelled on :class:`~spark_pulse.tools.engine_metrics.MetricsSampler`,
    including the part that matters: it discovers its subjects on every sweep
    rather than being told about them, so nothing has to remember to register
    a deployment when it is created or unregister it when it is deleted.
    """

    def __init__(
        self,
        interval: float = SWEEP_INTERVAL_SECONDS,
        services: Callable[[str], Any] | None = None,
    ):
        self._interval = interval
        self._services = services
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        #: Set for tests and diagnostics: how many sweeps have completed.
        self.sweeps = 0

    # -- lifecycle ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="reconciler", daemon=True
            )
            self._thread.start()
        logger.info("Reconciler started (sweeping every %.0fs)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=5)

    def nudge(self) -> None:
        """Sweep now rather than at the next tick.

        A delete on one healthy node should still look instant. Waiting a full
        interval to start work the operator has already asked for would make
        the asynchronous version feel slower than the synchronous one it
        replaced, which is how a good change gets reverted.
        """
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception as exc:  # pragma: no cover — the loop must not die
                logger.warning("Reconciliation sweep failed: %s", exc)
            self._wake.wait(self._interval)
            self._wake.clear()

    # -- the work -------------------------------------------------------------

    def sweep(self) -> list[dict[str, Any]]:
        """Advance every record that is not settled. Returns what it touched."""
        touched: list[dict[str, Any]] = []
        for record in tools.deployment_records.load() or []:
            state = sync_state(record)
            if state == SYNC_OK:
                continue
            result = self._converge(record, state)
            if result is not None:
                touched.append(result)
        self.sweeps += 1
        return touched

    def _converge(self, record: dict[str, Any], state: str) -> dict[str, Any] | None:
        deployment_id = str(record.get("id") or "")
        if not deployment_id:
            return None
        if state == SYNC_DELETING:
            return self._finish_delete(deployment_id)
        if state == SYNC_IN_PROGRESS:
            if str(record.get("sync_intent") or "") == INTENT_STOP:
                return self._finish_stop(deployment_id)
            return self._settle(deployment_id, record)
        return None

    def _finish_stop(self, deployment_id: str) -> dict[str, Any] | None:
        """Stop the containers and keep the record.

        A stop is not a removal. The record of a finished run is history an
        operator reads — which ports it held, which ranks came up, why it
        ended — and dropping it here would delete the very thing they asked to
        stop.
        """
        try:
            result = tools.deploy_dispatch.stop_deployment(deployment_id)
        except Exception as exc:  # noqa: BLE001 — a node that would not answer
            logger.debug("stop of %s did not complete: %s", deployment_id, exc)
            return mark(
                deployment_id,
                SYNC_IN_PROGRESS,
                f"waiting on a node: {exc}",
                INTENT_STOP,
            )

        if result is None:
            # Removed underneath us — nothing left to converge.
            return {"id": deployment_id, "deleted": True}
        return mark(deployment_id, SYNC_OK)

    def _finish_delete(self, deployment_id: str) -> dict[str, Any] | None:
        """Tear the ranks down and drop the record once nothing is left.

        `delete_deployment` already refuses to drop a record whose ranks are
        outstanding — dropping it would free that node's ports on inference
        while a container still holds them. Here that refusal is not an error:
        it is why the sweep runs again.
        """
        try:
            gone = tools.deploy_dispatch.delete_deployment(deployment_id)
        except Exception as exc:  # noqa: BLE001 — a node that would not answer
            logger.debug("delete of %s did not complete: %s", deployment_id, exc)
            return mark(deployment_id, SYNC_DELETING, f"waiting on a node: {exc}")

        if gone:
            tools.engine_metrics.forget(deployment_id)
            _publish(
                EventType.DEPLOYMENT_DELETED, deployment_id, "removed from every node"
            )
            return {"id": deployment_id, "deleted": True}

        # Still here: some rank could not be confirmed gone. The record keeps
        # its ports and its orphan list, and says what it is waiting for.
        return mark(
            deployment_id,
            SYNC_DELETING,
            "waiting for a rank whose container could not be confirmed gone",
        )

    def _settle(
        self, deployment_id: str, record: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Ask the nodes what is actually running, then stop saying in-progress.

        The question is the one `reconcile_deployments` already asks at
        startup. What is new is asking it on a timer, so a deployment that
        finished starting stops reading as in-flight without an operator
        reloading the page.
        """
        try:
            tools.reconciliation.reconcile_deployments()
        except Exception as exc:  # noqa: BLE001 — an unreachable node
            logger.debug("could not settle %s: %s", deployment_id, exc)
            return mark(
                deployment_id, SYNC_UNKNOWN, f"a node could not be asked: {exc}"
            )

        current = tools.deployment_records.get(deployment_id)
        if current is None:
            return {"id": deployment_id, "deleted": True}
        if str(current.get("status")) in ("pulling", "pending"):
            # Still genuinely in flight: the pull thread owns this one and
            # will settle the record itself.
            return None
        return mark(deployment_id, SYNC_OK)


# ── The process-wide reconciler ──────────────────────────────────────────────

_reconciler: Reconciler | None = None
_reconciler_lock = threading.Lock()


def get_reconciler() -> Reconciler:
    global _reconciler
    with _reconciler_lock:
        if _reconciler is None:
            _reconciler = Reconciler()
        return _reconciler


def start_reconciler() -> Reconciler:
    reconciler = get_reconciler()
    reconciler.start()
    return reconciler


def stop_reconciler() -> None:
    global _reconciler
    with _reconciler_lock:
        reconciler = _reconciler
        _reconciler = None
    if reconciler is not None:
        reconciler.stop()


def nudge() -> None:
    """Ask the running reconciler to sweep now, if there is one.

    Safe to call when nothing is running: a CLI process that deletes a
    deployment has no reconciler, and the next control plane to start will
    sweep the record it left behind.
    """
    with _reconciler_lock:
        reconciler = _reconciler
    if reconciler is not None:
        reconciler.nudge()


__all__ = [
    "SWEEP_INTERVAL_SECONDS",
    "SYNC_DELETING",
    "SYNC_IN_PROGRESS",
    "SYNC_OK",
    "SYNC_STATES",
    "SYNC_UNKNOWN",
    "INTENT_STOP",
    "Reconciler",
    "get_reconciler",
    "mark",
    "nudge",
    "start_reconciler",
    "stop_reconciler",
    "sync_state",
]
