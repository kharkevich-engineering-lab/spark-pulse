"""Compact each node's free memory before a run's first rank starts.

A node that has been up for days holds most of its free memory in small
fragments, and an engine that wants large contiguous mappings can start badly
(or not at all) beside gigabytes reported free. Dropping the page cache and
asking the kernel to compact (``CompactMemory`` on the node's agent) folds them
back; one operator measured free memory in blocks of 2 MiB or more going from
25 GiB to 80 GiB on a stopped node.

Three rules, all of them about not making a launch worse:

* **A node somebody else is serving on is skipped.** Dropping caches and
  compacting stall every process on the node, and a serving engine on it would
  feel that as a hiccup. "Occupied" is :func:`placement._occupied_nodes` — the
  one rule the port choice and the memory budget read — less this run's own
  record, which is written before it starts.
* **It never fails the deploy.** A node whose agent is too old, whose sudoers
  rule is missing, that times out or cannot be asked gets an event saying why
  and the run goes on. The setting is an optimisation; refusing a launch for it
  would be the optimisation making things worse.
* **Every node answers for itself, over its own agent.** Nothing here touches
  this machine's ``/proc``; the control node is asked over loopback like any
  peer. The nodes are asked in parallel — compaction takes seconds each.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from spark_pulse.tools import memory_shape, placement
from spark_pulse.tools.backends.docker import state
from spark_pulse.tools.events import EventType

logger = logging.getLogger(__name__)

GIB = 1024**3


def _gib(value: int) -> str:
    return f"{value / GIB:.1f} GiB"


def _reading(buddy: Any) -> dict[str, Any] | None:
    """:func:`memory_shape.summarize` over a ``BuddyInfo`` message, if it has one."""
    if buddy is None or not getattr(buddy, "page_size_bytes", 0):
        return None
    return memory_shape.summarize(buddy.page_size_bytes, list(buddy.free_blocks))


def _failure_reason(exc: BaseException) -> str:
    """Why a node could not be asked, in words an operator can act on."""
    text = str(exc)
    if "carries no op" in text:
        return "this node's agent is too old to compact memory; update it"
    return text[:300] or exc.__class__.__name__


def describe(label: str, result: Any) -> tuple[str, dict[str, Any]]:
    """The event message and metadata for one node's answer."""
    before = _reading(result.before)
    after = _reading(result.after)
    meta: dict[str, Any] = {
        "node": label,
        "compacted": bool(result.compacted),
        "dropped_caches": bool(result.dropped_caches),
        "detail": result.detail,
    }
    if before:
        meta["before_contiguous_bytes"] = before["contiguous_bytes"]
        meta["before_free_bytes"] = before["free_bytes"]
    if after:
        meta["after_contiguous_bytes"] = after["contiguous_bytes"]
        meta["after_free_bytes"] = after["free_bytes"]

    detail = (result.detail or "").strip()
    if "sudo rule missing" in detail:
        return (
            f"{label}: memory was not compacted; the agent's sudoers rule is "
            "missing, reinstall the agent",
            meta,
        )
    if "no new privileges" in detail or "is owned by uid 65534" in detail:
        # The control node's agent inherits the spark-pulse service's sandbox,
        # and units written before 2026-11 blocked sudo; rewriting the unit
        # is the whole fix.
        return (
            f"{label}: memory was not compacted; the spark-pulse service unit "
            "blocks sudo for the agent it runs; reinstall it with "
            "`spark-pulse install` (`--user` for a user service)",
            meta,
        )
    if not result.compacted:
        suffix = f" ({detail})" if detail else ""
        return f"{label}: memory was not compacted{suffix}", meta
    if before and after:
        return (
            f"{label}: {_gib(before['contiguous_bytes'])} → "
            f"{_gib(after['contiguous_bytes'])} free in blocks of 2 MiB or more",
            meta,
        )
    return f"{label}: memory compacted", meta


def compact_nodes(
    services: Callable[[str], Any],
    plan_obj: state.DeployPlan,
    busy: set[str],
    labels: Callable[[str], str],
) -> list[dict[str, Any]]:
    """Compact every distinct node of the run that nothing else is using.

    ``busy`` is the set of node keys (``placement._node_key``) other runs
    occupy; ``labels`` names a node for the event. Returns one dict per node
    (``node`` and a ``status`` of ``compacted``, ``failed`` or ``skipped``) so
    a caller or a test can read what happened. Publishes an event per node and
    raises nothing from asking a node.
    """
    dep_id = plan_obj.deployment_id
    addresses: list[str] = []
    for rank_plan in plan_obj.rank_plans:
        if rank_plan.node not in addresses:
            addresses.append(rank_plan.node)

    outcomes: list[dict[str, Any]] = []
    ask: list[str] = []
    for address in addresses:
        if placement._node_key(address) in busy:
            label = labels(address)
            state.publish_event(
                EventType.DEPLOYMENT_MEMORY_COMPACTED,
                dep_id,
                f"{label}: memory not compacted, another run is on this node",
                {"node": label, "compacted": False, "skipped": True},
            )
            outcomes.append({"node": address, "status": "skipped"})
        else:
            ask.append(address)

    def _one(address: str) -> tuple[str, Any, BaseException | None]:
        try:
            return address, services(address).compact_memory(True), None
        except Exception as exc:  # noqa: BLE001 — never fails the deploy
            return address, None, exc

    if not ask:
        return outcomes
    with ThreadPoolExecutor(max_workers=len(ask)) as pool:
        answers = list(pool.map(_one, ask))
    for address, result, exc in answers:
        label = labels(address)
        if exc is not None:
            logger.warning("could not compact memory on %s: %s", label, exc)
            reason = _failure_reason(exc)
            state.publish_event(
                EventType.DEPLOYMENT_MEMORY_COMPACTED,
                dep_id,
                f"{label}: memory was not compacted ({reason})",
                {"node": label, "compacted": False, "detail": reason},
            )
            outcomes.append({"node": address, "status": "failed"})
            continue
        message, meta = describe(label, result)
        state.publish_event(
            EventType.DEPLOYMENT_MEMORY_COMPACTED, dep_id, message, meta
        )
        outcomes.append(
            {
                "node": address,
                "status": "compacted" if result.compacted else "failed",
            }
        )
    return outcomes


def run(services: Callable[[str], Any], plan_obj: state.DeployPlan) -> None:
    """The deploy's call: read who else is where, name the nodes, compact.

    Wrapped whole so that nothing it does — reading the records, naming a node
    — can fail a launch.
    """
    try:
        busy = placement.occupied_by_others(plan_obj.deployment_id)
        names, control = placement._node_labels()

        def label(address: str) -> str:
            return names.get(address) or address or control

        compact_nodes(services, plan_obj, busy, label)
    except Exception:  # noqa: BLE001
        logger.exception("memory compaction before %s failed", plan_obj.deployment_id)
