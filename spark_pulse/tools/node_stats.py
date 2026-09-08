"""What every node is doing, asked of every node the same way.

The monitoring page used to run ``nvidia-smi``, ``free`` and ``df`` in this
process and show the answers. On one machine that reads as "the cluster"; on
four it is one Spark out of four, and the page does not say which. There was no
node parameter anywhere in the chain — not in the API, not in the page — so an
operator with a cluster was looking at whichever machine the control plane
happened to be installed on and had no way to tell.

Now each node is asked for its own stats through its own agent, including the
machine this process runs on. That is the same rule the container operations
follow: ``service_for`` has no local branch, and the control node reaches its
own agent over loopback exactly as it reaches a peer. A node that cannot be
asked is *unreachable and unknown*, with the reason attached — never an empty
panel that reads as an idle machine.

Two conversions happen here and nowhere else:

* **Units.** The protocol is bytes and the page is what an operator reads:
  megabytes for memory, bytes for disks, percentages as given. The proto
  message is the wire format, not the view model.
* **Absence.** Every GPU measurement is optional, because a GB10 reports
  ``[N/A]`` for GPU memory — the pool is unified — and a zero there would make
  a full machine look empty. ``memory_supported`` is how the page already says
  that, so absence becomes that flag rather than a number.

Tracking is the third thing, and it is decided here for the same reason: the
node reports which container a process is in, and only the control plane knows
which containers are *its own*. Each node's managed-container list answers that
in one call, and it names the deployment too — so a held GPU says which
deployment holds it, rather than only whether somebody claims it.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from spark_pulse import tools
from spark_pulse.tools.labels import DEPLOYMENT_LABEL

logger = logging.getLogger(__name__)

#: Bytes in a megabyte, as `free -m` and the page both mean it.
MIB = 1024 * 1024

#: How many nodes are asked at once. A cluster is small and the call is short.
MAX_PARALLEL_NODES = 8


def _services(services: Any | None = None) -> Callable[[Any], Any]:
    """The resolver every node is reached through."""
    if services is not None:
        return services
    return tools.node_service.NodeServices()


# ── One node ─────────────────────────────────────────────────────────────────


def _gpu(stat: Any) -> dict[str, Any]:
    """One GPU, in the shape the page has always read.

    ``memory_supported`` is the honest rendering of an absent measurement: a
    GB10 publishes no per-GPU memory because the pool is unified, and the page
    says so in words rather than drawing a bar at zero.
    """
    supported = stat.HasField("memory_total_bytes") and stat.memory_total_bytes > 0
    return {
        "index": stat.index,
        "gpu": f"GPU {stat.index}",
        "uuid": stat.uuid,
        "name": stat.name,
        "memory_total": stat.memory_total_bytes // MIB if supported else 0,
        "memory_used": (
            stat.memory_used_bytes // MIB if stat.HasField("memory_used_bytes") else 0
        ),
        "memory_free": (
            stat.memory_free_bytes // MIB if stat.HasField("memory_free_bytes") else 0
        ),
        "memory_supported": supported,
        "temperature": (
            stat.temperature_celsius if stat.HasField("temperature_celsius") else None
        ),
        "utilization": (
            stat.utilization_percent if stat.HasField("utilization_percent") else None
        ),
        "power_draw": stat.power_watts if stat.HasField("power_watts") else None,
        "power_limit": (
            stat.power_limit_watts if stat.HasField("power_limit_watts") else None
        ),
    }


def _cpu(stats: Any) -> dict[str, Any]:
    """Host memory in megabytes, which is what `free -m` gave the page."""
    if not stats.HasField("memory"):
        return {"total": 0, "used": 0, "free": 0, "available": 0, "usage_percent": 0}
    memory = stats.memory
    total = memory.total_bytes // MIB
    used = memory.used_bytes // MIB
    available = memory.available_bytes // MIB
    return {
        "total": total,
        "used": used,
        # `free -m` reports free and available separately and the page shows
        # both; the protocol carries available, which is the one that matters.
        "free": available,
        "available": available,
        "usage_percent": round(used / total * 100, 1) if total else 0,
    }


def _disks(stats: Any) -> list[dict[str, Any]]:
    return [
        {
            "mount": disk.mount,
            "total": disk.total_bytes,
            "used": disk.used_bytes,
            "free": disk.free_bytes,
            "usage_percent": (
                round(disk.used_bytes / disk.total_bytes * 100, 1)
                if disk.total_bytes
                else 0.0
            ),
        }
        for disk in stats.disks
    ]


def _processes(stats: Any, owners: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """GPU processes, each said to be ours or not, and whose.

    ``owners`` maps a container id prefix to the deployment that container
    belongs to. The node cannot decide this — it does not know what the control
    plane started — and the control plane cannot see the process without the
    node. Each half is asked for what only it has.
    """
    rows = []
    for process in stats.processes:
        container = (process.container_id or "")[:12]
        owner = owners.get(container) if container else None
        rows.append(
            {
                "gpu_uuid": stats.gpus[0].uuid if stats.gpus else "",
                "pid": process.pid,
                "process_name": process.name,
                "used_memory": (
                    process.used_memory_bytes // MIB
                    if process.HasField("used_memory_bytes")
                    else 0
                ),
                "container_id": container,
                "is_tracked": owner is not None,
                "deployment": (owner or {}).get("deployment", ""),
                "container_name": (owner or {}).get("name", ""),
            }
        )
    return rows


def _owners(service: Any) -> dict[str, dict[str, str]]:
    """Container id -> what the control plane started it for, on one node.

    Managed containers only, which is exactly the question: a container this
    control plane labelled is one it started. A node that cannot answer leaves
    the map empty, so its processes read as unclaimed rather than the call
    failing — the stats are still worth showing.
    """
    owners: dict[str, dict[str, str]] = {}
    try:
        containers = service.list_managed_containers() or []
    except Exception as exc:  # noqa: BLE001 — a node that answered stats but not this
        logger.debug("could not list managed containers: %s", exc)
        return owners
    for container in containers:
        labels = getattr(container, "labels", {}) or {}
        owners[str(getattr(container, "id", ""))[:12]] = {
            "name": str(getattr(container, "name", "")),
            "deployment": str(labels.get(DEPLOYMENT_LABEL, "")),
        }
    return owners


def _empty_block(**fields: Any) -> dict[str, Any]:
    block = {
        "gpu": [],
        "cpu": {"total": 0, "used": 0, "free": 0, "available": 0, "usage_percent": 0},
        "disk": [],
        "processes": [],
        "unavailable": [],
        "reachable": False,
        "error": None,
    }
    block.update(fields)
    return block


def for_node(node: Any, services: Any | None = None) -> dict[str, Any]:
    """Ask one node what it is doing.

    An unreachable node is answered honestly rather than skipped: the row is
    there, it says it could not be asked, and it says why. A missing row and a
    quiet machine look the same on a page, and only one of them is fine.
    """
    resolve = _services(services)
    try:
        service = resolve(node)
        stats = service.get_node_stats()
    except Exception as exc:  # noqa: BLE001 — every node reaches this the same way
        return _empty_block(error=str(exc)[:500])

    return _empty_block(
        reachable=True,
        gpu=[_gpu(gpu) for gpu in stats.gpus],
        cpu=_cpu(stats),
        disk=_disks(stats),
        processes=_processes(stats, _owners(service)),
        unavailable=list(stats.unavailable),
        cpu_count=stats.cpu_count,
        load_average_1m=stats.load_average_1m,
    )


# ── Every node ───────────────────────────────────────────────────────────────


def _registry_nodes() -> list[Any]:
    """The registry's records, control plane first. Empty when unreadable."""
    try:
        return list(tools.node_registry.list_nodes())
    except Exception as exc:  # noqa: BLE001 — a registry we cannot read
        logger.warning("could not read the node registry: %s", exc)
        return []


def _node_for(record: Any) -> Any:
    """The node reference for a registry record."""
    if getattr(record, "is_control_plane", False):
        return tools.node_service.control_node(address=record.address or "")
    return tools.node_service.node_for(
        record.address, ssh_user=getattr(record, "ssh_user", "") or ""
    )


def collect(services: Any | None = None) -> dict[str, Any]:
    """Every registered node's live stats, control plane first.

    One shape, whatever the cluster size. There was a flat copy of the control
    node's own block beside this for readers written before the answer covered
    more than one machine; it went with them, because a payload that answers
    twice invites a page to read the wrong half and call it the cluster.
    """
    records = _registry_nodes()
    if not records:
        # No registry at all: this process still runs on a machine, and that
        # machine still has a GPU worth reporting.
        control = for_node(tools.node_service.control_node(), services)
        control.update({"id": "control", "name": "this node", "address": ""})
        control["is_control_plane"] = True
        return {"nodes": [control]}

    def _one(record: Any) -> dict[str, Any]:
        block = for_node(_node_for(record), services)
        block.update(
            {
                "id": getattr(record, "id", ""),
                "name": getattr(record, "name", "") or getattr(record, "address", ""),
                "address": getattr(record, "address", ""),
                "is_control_plane": bool(getattr(record, "is_control_plane", False)),
            }
        )
        return block

    with ThreadPoolExecutor(
        max_workers=max(1, min(MAX_PARALLEL_NODES, len(records)))
    ) as pool:
        blocks = list(pool.map(_one, records))

    return {"nodes": blocks}


# ── Ending a process ─────────────────────────────────────────────────────────


def terminate(
    pid: int,
    address: str = "",
    force: bool = False,
    services: Any | None = None,
) -> dict[str, Any]:
    """End one GPU process, on whichever node it is running on.

    Two ways, and which one is right is a fact about the process rather than
    about the machine it is on. A process inside a container this control plane
    started is ended by stopping that container: killing the process inside it
    leaves the container behind, and the runtime restarts it or the ports stay
    held. Anything else is signalled directly.

    The old implementation could only do this for the control node, because it
    was ``os.kill`` in this process. That made the button on every other node's
    rows a lie.
    """
    resolve = _services(services)
    node = tools.node_service.node_for(address)
    try:
        service = resolve(node)
    except Exception as exc:  # noqa: BLE001 — a node with no agent
        return {"killed": False, "pid": pid, "error": str(exc)[:500]}

    owner = _owner_of(service, pid)
    try:
        if owner is not None and owner.get("name"):
            stopped = bool(service.stop_container(owner["name"]))
            return {
                "killed": stopped,
                "pid": pid,
                "container": owner["name"],
                "deployment": owner.get("deployment", ""),
                "error": None if stopped else "the container did not stop",
            }
        termination = service.terminate_process(int(pid), bool(force))
    except Exception as exc:  # noqa: BLE001 — the node answered, or did not
        return {"killed": False, "pid": pid, "error": str(exc)[:500]}

    return {
        "killed": bool(termination.terminated),
        "pid": pid,
        "error": termination.detail or None,
    }


def _owner_of(service: Any, pid: int) -> dict[str, str] | None:
    """The managed container holding ``pid`` on this node, if any."""
    try:
        stats = service.get_node_stats()
    except Exception as exc:  # noqa: BLE001 — then signal it directly
        logger.debug("could not read stats before terminating %s: %s", pid, exc)
        return None
    container = next(
        (p.container_id for p in stats.processes if p.pid == int(pid)),
        "",
    )
    if not container:
        return None
    return _owners(service).get(container[:12])


__all__ = ["MIB", "collect", "for_node", "terminate"]
