"""What a run has actually been seen to hold, so the next plan of it knows.

The memory budget (:mod:`memory_budget`) works from what a run *claims* — its
fraction of the node — and an estimate (:mod:`vram`) for the engine that
claims none. Both are guesses about the future. A run that has been up is a
measurement: what its containers held on the node, at their largest. This
module keeps that measurement, keyed by the run's ``intent_hash`` — what the
run *is*, whatever it was called (:mod:`intent`) — so a second deploy of the
same thing is planned against what the first one held rather than against
arithmetic.

**The key is the intent and the node's size, not the node.** A fraction is a
fraction of the node's total, and an engine with a fixed footprint holds the
same bytes on any node of the same size, so a measurement taken on one Spark
is the right answer for its twin. A node of another size is another key:
``0.8`` of 128 GiB is not ``0.8`` of 24. The size is rounded to whole GiB
because ``MemTotal`` moves by a page or two across reboots.

**A peak, never a mean.** An engine that allocates as it goes (llama.cpp's KV
cache, a vLLM run's activations under load) holds most when it is busiest,
and a budget that planned for the quiet hour would co-locate a run into the
busy one. The figure only rises; a run that now holds less does not lower it,
because the next time it may not.

**Sampled from the nodes, through their agents.** :class:`FootprintSampler`
asks each node a running run occupies for what its containers hold —
:func:`node_stats.memory_by_deployment`, the same question the budget asks —
once a minute. A node that cannot be asked is skipped, not recorded as zero.

Real-only, like :mod:`event_log`: a database table and a thread that reaches
every node through the switch, so it is the same code in both modes.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from sqlalchemy import BigInteger, Float, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from spark_pulse.db import Base, session_scope

logger = logging.getLogger(__name__)

GIB = 1024**3

#: How often the sampler asks. A footprint settles within a minute of a run
#: being ready, and a peak under load lasts longer than one.
SAMPLE_INTERVAL_SECONDS = 60.0

__all__ = [
    "SAMPLE_INTERVAL_SECONDS",
    "FootprintRow",
    "FootprintSampler",
    "lookup",
    "observe",
    "sample_once",
    "size_key",
    "start_sampler",
    "stop_sampler",
]


class FootprintRow(Base):
    """The largest a run of one intent has held on a node of one size."""

    __tablename__ = "run_footprints"

    intent_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: The node's total memory in whole GiB (:func:`size_key`).
    node_gib: Mapped[int] = mapped_column(Integer, primary_key=True)
    peak_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    samples: Mapped[int] = mapped_column(Integer, default=0)
    #: The run the peak was seen on, for the operator who asks where a number
    #: came from.
    deployment_id: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[float] = mapped_column(Float, default=0.0)


def size_key(total_bytes: int | None) -> int | None:
    """A node's total as the key a footprint is filed under: whole GiB."""
    if not total_bytes or total_bytes <= 0:
        return None
    return int(round(total_bytes / GIB))


def observe(
    intent_hash: str,
    total_bytes: int | None,
    held_bytes: int | None,
    deployment_id: str = "",
) -> int | None:
    """Record that a run of ``intent_hash`` held ``held_bytes``; return the peak.

    A held figure that is missing or zero is not a measurement — a node that
    reports no memory for a process is unknown, and a zero would pull the peak
    of nothing. Returns the stored peak after the write, or ``None`` when
    nothing could be recorded.
    """
    node_gib = size_key(total_bytes)
    if not intent_hash or node_gib is None or not held_bytes or held_bytes <= 0:
        return None
    with session_scope() as db:
        row = db.get(FootprintRow, (intent_hash, node_gib))
        if row is None:
            row = FootprintRow(
                intent_hash=intent_hash,
                node_gib=node_gib,
                peak_bytes=int(held_bytes),
                samples=1,
                deployment_id=deployment_id,
                updated_at=time.time(),
            )
            db.add(row)
            return int(held_bytes)
        row.samples = int(row.samples or 0) + 1
        if int(held_bytes) > int(row.peak_bytes or 0):
            row.peak_bytes = int(held_bytes)
            row.deployment_id = deployment_id or row.deployment_id
            row.updated_at = time.time()
        return int(row.peak_bytes)


def lookup(intent_hash: str | None, total_bytes: int | None) -> int | None:
    """The peak a run of ``intent_hash`` was seen holding on a node this size."""
    node_gib = size_key(total_bytes)
    if not intent_hash or node_gib is None:
        return None
    try:
        with session_scope() as db:
            row = db.execute(
                select(FootprintRow.peak_bytes).where(
                    FootprintRow.intent_hash == intent_hash,
                    FootprintRow.node_gib == node_gib,
                )
            ).first()
    except Exception:  # noqa: BLE001 — a lookup is advice; the budget goes on
        logger.debug("footprint lookup for %s failed", intent_hash, exc_info=True)
        return None
    return int(row[0]) if row and row[0] else None


def _running_records() -> list[dict[str, Any]]:
    """Every run that has been seen serving and is not finished."""
    from spark_pulse.tools.deployment_records import load as load_records

    return [
        r
        for r in load_records()
        if r.get("ready_at")
        and r.get("intent_hash")
        and r.get("status") not in ("stopped", "error")
    ]


def _nodes_of(record: dict[str, Any]) -> list[str]:
    """The addresses a run's ranks are on, as its services are resolved."""
    ranks = record.get("ranks")
    if isinstance(ranks, list) and ranks:
        nodes = [str(r.get("node") or "") for r in ranks if isinstance(r, dict)]
    else:
        nodes = [str(n) for n in (record.get("nodes") or [])] or [""]
    seen: list[str] = []
    for node in nodes:
        if node not in seen:
            seen.append(node)
    return seen


def sample_once(services: Callable[[str], Any] | None = None) -> int:
    """Ask every node a running run is on what its runs hold; record peaks.

    Returns how many measurements were recorded. Each node is asked once,
    however many runs share it.
    """
    from spark_pulse import tools

    if services is None:
        services = tools.native_runtime.rank_services(None)
    records = _running_records()
    by_node: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        for node in _nodes_of(record):
            by_node.setdefault(node, []).append(record)

    recorded = 0
    for node, runs in by_node.items():
        try:
            total, held = tools.node_stats.memory_by_deployment(services(node))
        except Exception as exc:  # noqa: BLE001 — a silent node is skipped
            logger.debug("footprint sample of %s skipped: %s", node or "local", exc)
            continue
        for record in runs:
            dep_id = str(record.get("id") or "")
            if observe(
                str(record.get("intent_hash") or ""),
                total,
                held.get(dep_id),
                dep_id,
            ):
                recorded += 1
    return recorded


class FootprintSampler:
    """A thread that calls :func:`sample_once` every interval until stopped."""

    def __init__(self, interval: float = SAMPLE_INTERVAL_SECONDS) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="footprint-sampler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                sample_once()
            except Exception:  # noqa: BLE001 — one bad sweep is not the last
                logger.exception("footprint sample failed")


_sampler: FootprintSampler | None = None
_sampler_lock = threading.Lock()


def start_sampler() -> FootprintSampler:
    global _sampler
    with _sampler_lock:
        if _sampler is None:
            _sampler = FootprintSampler()
        sampler = _sampler
    sampler.start()
    return sampler


def stop_sampler() -> None:
    global _sampler
    with _sampler_lock:
        sampler, _sampler = _sampler, None
    if sampler is not None:
        sampler.stop()
