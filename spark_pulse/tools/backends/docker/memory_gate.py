"""Check, just before the engine starts, that the memory it will ask for is free.

The plan's memory budget (:mod:`memory_budget`) is judged when the deploy is
planned, and the engine starts minutes later — after the image pull, which on
a Spark fills the unified pool with page cache. vLLM reads free memory once,
at startup, and refuses with *"Free memory on device … is less than desired
GPU memory utilization"*; the operator then reads a traceback whose cause is
two hundred lines further up. An embedding run at 0.12 beside a 0.8 SGLang run
ended exactly that way on the cluster, straight after an 8 GB pull, and the
same command started cleanly twenty minutes later.

So the launch asks again, of every node the run is on, through its agent:

* **What the run needs** is the plan's claim on that node — its fraction of
  the node's total, or what a run of the same intent was seen holding. A run
  that claims nothing knowable (an engine that takes no fraction and has never
  been seen) is not gated: there is no number to compare.
* **What is free** is the node's own figure (``MemAvailable`` on a unified
  pool, the card's free VRAM on a discrete one), less what co-tenants that are
  *still starting* have claimed and not yet taken — their memory is not
  missing from the free figure yet, and will be.
* **Short of it, the page cache goes first**: one ``CompactMemory`` asking
  only to drop the caches on a node another run is serving on (compaction
  would stall it), and to compact too on one nobody is. Then it waits, polling,
  for ``memory.launch_wait_seconds`` — a run beside one that is stopping gets
  the memory a moment later — and fails with the numbers and the holders when
  it does not come.

**Unknown is not short.** A node that cannot be asked, or reports no free
figure, is let through with an event saying so: the engine's own check is
still there, and refusing a launch because a reading failed would make the
gate the thing that breaks deploys.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from spark_pulse import tools
from spark_pulse.tools import memory_budget, placement
from spark_pulse.tools.backends.docker import state
from spark_pulse.tools.events import EventType

logger = logging.getLogger(__name__)

GIB = 1024**3

#: How often the gate asks again while it waits.
POLL_INTERVAL_SECONDS = 5.0

#: Statuses of a co-tenant whose claim the node's free figure does not show
#: yet: it has been planned onto the node and has not finished mapping.
_STARTING = ("pending", "pulling", "starting")


def _gib(value: int | None) -> str:
    return f"{(value or 0) / GIB:.1f} GiB"


def _entry_for(plan_obj: state.DeployPlan, key: str) -> dict[str, Any] | None:
    for entry in plan_obj.memory_budget or []:
        if isinstance(entry, dict) and str(entry.get("key") or "") == key:
            return entry
    return None


def _claim(
    plan_obj: state.DeployPlan, key: str, total: int | None
) -> tuple[int | None, str]:
    """This run's claim on the node, in bytes, and where it came from."""
    entry = _entry_for(plan_obj, key) or {}
    fraction = plan_obj.gpu_memory_utilization
    best: int | None = None
    source = ""
    if fraction is not None and total:
        best, source = int(fraction * total), "fraction"
    observed = tools.footprints.lookup(plan_obj.intent_hash, total)
    if observed and (best is None or observed > best):
        best, source = int(observed), "observed"
    if best is None and entry.get("claim_bytes"):
        best, source = int(entry["claim_bytes"]), str(entry.get("claim_source") or "")
    return best, source


def _co_tenants(key: str, exclude: str) -> list[dict[str, Any]]:
    """The other runs on the node, by the occupancy rule the budget uses."""
    found = []
    for record in placement._load_records():
        if record.get("id") == exclude:
            continue
        nodes = placement._occupied_nodes(record)
        if nodes is not None and key in nodes:
            found.append(record)
    return found


def _pending_bytes(
    tenants: list[dict[str, Any]], total: int | None, held: dict[str, int | None]
) -> int:
    """What co-tenants still starting have claimed and not yet taken."""
    pending = 0
    for record in tenants:
        if record.get("ready_at") or record.get("status") not in _STARTING:
            continue
        fraction = memory_budget.record_fraction(record)
        if fraction is None or not total:
            continue
        taken = held.get(str(record.get("id") or "")) or 0
        pending += max(0, int(fraction * total) - int(taken))
    return pending


def _holders_text(
    tenants: list[dict[str, Any]],
    held: dict[str, int | None],
    total: int | None,
    free: int | None,
) -> str:
    parts = []
    ours = 0
    for record in tenants:
        dep_id = str(record.get("id") or "")
        name = str(record.get("name") or dep_id)
        bytes_held = held.get(dep_id)
        if bytes_held:
            ours += int(bytes_held)
            parts.append(f"run {name} holds {_gib(bytes_held)}")
        else:
            parts.append(f"run {name} is starting")
    if total and free is not None:
        outside = max(0, total - free - ours)
        if outside >= 2 * GIB:
            parts.append(f"{_gib(outside)} is held outside any run")
    return "; ".join(parts) or "no run of ours is on it"


def wait_for_room(
    services: Callable[[str], Any],
    plan_obj: state.DeployPlan,
    timeout: float,
    cancelled: Callable[[], bool] = lambda: False,
    labels: Callable[[str], str] | None = None,
    interval: float = POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """Wait until every node has room for the run; the reason when one never does.

    Returns ``None`` when the run may launch — it fits, the gate could not
    tell, or ``cancelled()`` turned true (the caller re-checks teardown) — and
    the failure message otherwise. Publishes an event per node it had to act
    or wait on.
    """
    if labels is None:
        names, control = placement._node_labels()

        def labels(address: str) -> str:
            return names.get(address) or address or control

    dep_id = plan_obj.deployment_id
    addresses: list[str] = []
    for rank_plan in plan_obj.rank_plans:
        if rank_plan.node not in addresses:
            addresses.append(rank_plan.node)

    deadline = clock() + max(0.0, timeout)
    for address in addresses:
        key = placement._node_key(address)
        label = labels(address)
        dropped = False
        announced = False
        while True:
            if cancelled():
                return None
            try:
                memory = tools.node_stats.node_memory(services(address))
            except Exception as exc:  # noqa: BLE001 — unknown is not short
                state.publish_event(
                    EventType.DEPLOYMENT_MEMORY_WAIT,
                    dep_id,
                    f"{label}: could not read free memory before the launch "
                    f"({str(exc)[:200]}); the engine's own check decides",
                    {"node": label, "checked": False},
                )
                break
            need, source = _claim(plan_obj, key, memory.total_bytes)
            if need is None or memory.free_bytes is None:
                break
            tenants = _co_tenants(key, dep_id)
            pending = _pending_bytes(tenants, memory.total_bytes, memory.held)
            room = memory.free_bytes - pending
            if need <= room:
                if announced:
                    state.publish_event(
                        EventType.DEPLOYMENT_MEMORY_WAIT,
                        dep_id,
                        f"{label}: {_gib(room)} is free now, enough for this "
                        f"run's {_gib(need)}",
                        {"node": label, "free_bytes": room, "need_bytes": need},
                    )
                break
            if not dropped:
                dropped = True
                busy = bool(tenants)
                try:
                    services(address).compact_memory(True, not busy)
                    what = "dropped the page cache" + ("" if busy else " and compacted")
                except Exception as exc:  # noqa: BLE001 — best effort
                    logger.debug("drop caches on %s failed: %s", label, exc)
                    what = "could not drop the page cache"
                state.publish_event(
                    EventType.DEPLOYMENT_MEMORY_WAIT,
                    dep_id,
                    f"{label}: {_gib(room)} free, this run needs {_gib(need)}; "
                    f"{what}",
                    {"node": label, "free_bytes": room, "need_bytes": need},
                )
                continue
            if clock() >= deadline:
                total = memory.total_bytes
                asks = (
                    f"{plan_obj.gpu_memory_utilization:.2f} of {_gib(total)} "
                    f"({_gib(need)})"
                    if source == "fraction" and plan_obj.gpu_memory_utilization
                    else f"{_gib(need)}"
                    + (" (what it held last time)" if source == "observed" else "")
                )
                fits = max(0.0, int(room / total * 100) / 100) if total else None
                remedy = (
                    f"lower gpu_memory_utilization to {fits:.2f} or less, or "
                    "stop a run on this node"
                    if fits is not None and source == "fraction"
                    else "stop a run on this node or free the memory"
                )
                pending_text = (
                    f", {_gib(pending)} of it already promised to runs still "
                    "starting"
                    if pending
                    else ""
                )
                return (
                    f"not enough free memory on {label} to start the engine: "
                    f"it asks {asks} and {_gib(memory.free_bytes)} is free"
                    f"{pending_text} after waiting {timeout:g}s "
                    f"({_holders_text(tenants, memory.held, total, memory.free_bytes)})"
                    f". To start it, {remedy}"
                )
            if not announced:
                announced = True
                state.publish_event(
                    EventType.DEPLOYMENT_MEMORY_WAIT,
                    dep_id,
                    f"{label}: waiting up to {timeout:g}s for {_gib(need)} to "
                    f"be free ({_gib(room)} is)",
                    {"node": label, "free_bytes": room, "need_bytes": need},
                )
            sleep(interval)
    return None


def check(
    services: Callable[[str], Any],
    plan_obj: state.DeployPlan,
    cancelled: Callable[[], bool] = lambda: False,
) -> str | None:
    """The deploy's call, wrapped so a bug in the gate cannot fail a launch."""
    from spark_pulse.config import config

    if not config.memory_check_before_launch:
        return None
    try:
        return wait_for_room(
            services,
            plan_obj,
            config.memory_launch_wait_seconds,
            cancelled=cancelled,
        )
    except Exception:  # noqa: BLE001
        logger.exception("the memory check before %s failed", plan_obj.deployment_id)
        return None
