"""The ConnectX fabric across the cluster: what it is, what it should be, apply.

``GET /api/fabric`` reads every registered node's ports from what its agent
reported on its last heartbeat — no login — and returns the plan for all of
them at once, so the page can show the file each node would get before any
of it is written. ``POST /api/fabric/apply`` writes the proposed nodes' files
over SSH with the control plane's key, applies them, and reads each port back.
The only secret in the request is an optional sudo password, used for that
call and kept nowhere.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from spark_pulse import tools
from spark_pulse.agent import runtime as agent_runtime

router = APIRouter(prefix="/api/fabric", tags=["fabric"])


def _facts_for(runtime: Any, node: Any) -> Any | None:
    node_id = runtime.control_node_id if node.is_control_plane else node.id
    connection = runtime.hub.get(node_id) if node_id else None
    return connection.facts if connection is not None else None


#: The wired 10G port a mesh coordinates over, by the name a Spark gives it.
WIRED_MANAGEMENT = "enP7s7"


def _node_fabrics(runtime: Any | None) -> list[Any]:
    nodes = tools.node_registry.list_nodes()
    fabrics = []
    for node in nodes:
        facts = _facts_for(runtime, node) if runtime is not None else None
        fabric = (
            tools.discovery.fabric_from_facts(facts, node_count=len(nodes))
            if facts is not None
            else None
        )
        mtus = (
            {i.name: int(i.mtu) for i in facts.interfaces if i.name}
            if facts is not None
            else {}
        )
        wired: bool | None = None
        if facts is not None:
            for interface in facts.interfaces:
                if interface.name == WIRED_MANAGEMENT:
                    wired = bool(interface.is_up)
        fabrics.append(
            tools.fabric_plan.NodeFabric(
                node_id=node.id,
                name=node.name or node.address,
                is_control_plane=bool(node.is_control_plane),
                fabric=fabric,
                mtus=mtus,
                wired_management_up=wired,
            )
        )
    return fabrics


def _pin_record(node_plan: Any, mode: str) -> dict[str, Any]:
    """Write what a deploy pins NCCL with onto the node's registry record.

    A deploy takes ``ethernet_interface``, ``infiniband_interfaces`` and
    ``fabric_mode`` from the registry — ``register_self`` fills them for the
    control node and nothing filled them for a peer — so a peer whose fabric
    was verified here still deployed unpinned. This is the missing write.
    """
    assignments = list(node_plan.assignments)
    if not assignments:
        return {}
    lowercase = [a for a in assignments if "P" not in a.netdev]
    changes = {
        "ethernet_interface": (lowercase or assignments)[0].netdev,
        "infiniband_interfaces": [a.hca for a in assignments],
        "fabric_mode": mode,
    }
    tools.node_registry.update_node(node_plan.node_id, **changes)
    return changes


def _current(fabrics: list[Any]) -> list[dict[str, Any]]:
    """What each node's ports look like now, for the page's table."""
    rows = []
    for entry in fabrics:
        fabric = entry.fabric
        rows.append(
            {
                "node_id": entry.node_id,
                "name": entry.name,
                "is_control_plane": entry.is_control_plane,
                "reported": fabric is not None,
                "mode": fabric.mode if fabric else "",
                "ports": (
                    [
                        {
                            "hca": p.hca,
                            "netdev": p.netdev,
                            "is_up": p.is_up,
                            "cidr": fabric.addresses.get(p.netdev, ""),
                            "mtu": entry.mtus.get(p.netdev, 0),
                        }
                        for p in fabric.ports
                    ]
                    if fabric
                    else []
                ),
                "ib_hca": fabric.ib_hca_value if fabric else "",
                "errors": list(fabric.errors) if fabric else [],
                "warnings": list(fabric.warnings) if fabric else [],
                "wired_management_up": entry.wired_management_up,
                "pinned": _pinned(entry.node_id),
            }
        )
    return rows


def _pinned(node_id: str) -> dict[str, Any]:
    """What the registry would pin a deploy on this node with, right now."""
    node = tools.node_registry.get_node(node_id)
    if node is None:
        return {}
    return {
        "ethernet_interface": node.ethernet_interface,
        "infiniband_interfaces": list(node.infiniband_interfaces),
        "fabric_mode": node.fabric_mode,
    }


@router.get("")
def read_fabric(override: bool = False):
    """Every node's fabric as its agent last reported it, and the plan."""
    runtime = agent_runtime.current()
    fabrics = _node_fabrics(runtime)
    plan = tools.fabric_plan.plan_fabric(fabrics, override=override)
    return {
        "transport": runtime is not None,
        "nodes": _current(fabrics),
        "plan": plan.to_dict(),
    }


@router.post("/apply")
async def apply_fabric(body: dict[str, Any] = Body(default={})):
    """Apply the plan on the proposed nodes through each node's own agent.

    ``node_ids`` narrows it; ``override`` re-addresses already-valid nodes too.
    Each node's agent drives ``nmcli`` — no SSH, and the control node is
    reached over its own agent like any peer, so it needs no SSH user. A node
    whose agent lacks the nmcli sudoers grant reports that, rather than the
    control plane logging in on its behalf.
    """
    runtime = agent_runtime.current()
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail="the agent transport is not running, so no node's ports are known",
        )
    override = bool(body.get("override"))
    wanted = {str(n) for n in (body.get("node_ids") or [])}
    plan = tools.fabric_plan.plan_fabric(_node_fabrics(runtime), override=override)
    by_id = {n.node_id: n for n in plan.nodes}
    targets = [
        by_id[n.node_id] for n in plan.proposed if not wanted or n.node_id in wanted
    ]
    configured = [
        n
        for n in plan.nodes
        if n.status == tools.fabric_plan.STATUS_CONFIGURED
        and (not wanted or n.node_id in wanted)
    ]
    if not targets and not configured:
        raise HTTPException(
            status_code=400,
            detail="nothing to apply: no node is proposed a change"
            + (f" — {'; '.join(plan.problems)}" if plan.problems else ""),
        )

    reports = [
        await _apply_through_agent(node_plan, plan.mode) for node_plan in targets
    ]

    # A node already configured by hand has a fabric a deploy can use only if
    # the registry says so; pin those too, no agent call needed.
    pinned = {}
    for node_plan in configured:
        pinned[node_plan.node_id] = _pin_record(node_plan, plan.mode)
    return {"mode": plan.mode, "reports": reports, "pinned": pinned}


def _connection_name(netdev: str) -> str:
    """The NetworkManager connection the agent creates for a fabric port."""
    return f"spark-pulse-{netdev}"


async def _apply_through_agent(node_plan: Any, mode: str) -> dict[str, Any]:
    """Configure one node's fabric over its agent, and shape the report.

    Runs the blocking node-service call in a worker thread: the agent client
    is synchronous and must not be awaited on the control plane's loop.
    """
    import asyncio

    node = tools.node_registry.get_node(node_plan.node_id)
    base = {
        "node_id": node_plan.node_id,
        "name": node_plan.name,
        "applied": False,
        "verified": False,
        "steps": [],
        "errors": [],
        "readback": {},
        "pings": [],
    }
    if node is None:
        base["errors"] = ["the node is no longer in the registry"]
        return base

    interfaces = [
        (a.netdev, _connection_name(a.netdev), a.cidr, a.mtu)
        for a in node_plan.assignments
        if a.cidr
    ]
    peers = [
        (a.netdev, address) for a in node_plan.assignments for _peer, address in a.peers
    ]
    if not interfaces:
        base["errors"] = ["the plan assigned this node no addresses"]
        return base

    from spark_pulse.tools.node_service import NoAgent, node_for, service_for

    # node_for decides control-node-vs-peer by address; service_for then reaches
    # that node's agent (the control node over loopback, a peer over its stream).
    target = node_for(node.address, ssh_user=node.ssh_user)
    try:
        service = service_for(target)
        result = await asyncio.to_thread(service.configure_fabric, interfaces, peers)
    except NoAgent as exc:
        base["errors"] = [str(exc)]
        return base
    except Exception as exc:  # the agent ran it and it failed — reachable, definite
        base["errors"] = [str(exc)]
        base["applied"] = True
        return base

    ports = {
        p.netdev: {
            "cidr": p.cidr,
            "address_ok": p.address_ok,
            "mtu": str(p.mtu),
            "mtu_ok": p.mtu_ok,
        }
        for p in result.ports
    }
    pings = [
        {"netdev": p.netdev, "address": p.address, "reachable": p.reachable}
        for p in result.pings
    ]
    addresses_ok = all(p.address_ok for p in result.ports)
    peers_ok = all(p.reachable for p in result.pings)
    verified = addresses_ok and peers_ok
    errors = []
    for p in result.ports:
        if not p.address_ok:
            errors.append(
                f"{p.netdev} did not come up with {ports[p.netdev]['cidr'] or 'an address'}"
            )
    for ping in result.pings:
        if not ping.reachable:
            errors.append(
                f"{ping.address} does not answer over {ping.netdev}; either that "
                "node is not applied yet, or the cable does not go where the plan assumed"
            )
    report = {
        **base,
        "applied": True,
        "verified": verified,
        "steps": list(result.steps),
        "errors": errors,
        "readback": ports,
        "pings": pings,
    }
    if verified:
        report["pinned"] = _pin_record(node_plan, mode)
    return report
