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
from spark_pulse.agent import fabric_apply, onboarding
from spark_pulse.agent import runtime as agent_runtime
from spark_pulse.agent.bootstrap import NodeAccess

router = APIRouter(prefix="/api/fabric", tags=["fabric"])


def _facts_for(runtime: Any, node: Any) -> Any | None:
    node_id = runtime.control_node_id if node.is_control_plane else node.id
    connection = runtime.hub.get(node_id) if node_id else None
    return connection.facts if connection is not None else None


def _node_fabrics(runtime: Any | None) -> list[Any]:
    fabrics = []
    for node in tools.node_registry.list_nodes():
        facts = _facts_for(runtime, node) if runtime is not None else None
        fabric = tools.discovery.fabric_from_facts(facts) if facts is not None else None
        mtus = (
            {i.name: int(i.mtu) for i in facts.interfaces if i.name}
            if facts is not None
            else {}
        )
        fabrics.append(
            tools.fabric_plan.NodeFabric(
                node_id=node.id,
                name=node.name or node.address,
                is_control_plane=bool(node.is_control_plane),
                fabric=fabric,
                mtus=mtus,
            )
        )
    return fabrics


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
            }
        )
    return rows


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
    """Write and apply the plan on the proposed nodes, then read them back.

    ``node_ids`` narrows it; ``override`` re-addresses already-valid nodes
    too; ``sudo_password`` is used only if a node needs it.
    """
    runtime = agent_runtime.current()
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail="the agent transport is not running, so no node's ports are known",
        )
    override = bool(body.get("override"))
    wanted = {str(n) for n in (body.get("node_ids") or [])}
    sudo_password = str(body.get("sudo_password") or "") or None
    plan = tools.fabric_plan.plan_fabric(_node_fabrics(runtime), override=override)
    targets = [n for n in plan.proposed if not wanted or n.node_id in wanted]
    if not targets:
        raise HTTPException(
            status_code=400,
            detail="nothing to apply: no node is proposed a change"
            + (f" — {'; '.join(plan.problems)}" if plan.problems else ""),
        )

    async def sudo(_question: str) -> str | None:
        return sudo_password

    reports = []
    for node_plan in targets:
        node = tools.node_registry.get_node(node_plan.node_id)
        if node is None or not node.address or not node.ssh_user:
            reports.append(
                {
                    "node_id": node_plan.node_id,
                    "name": node_plan.name,
                    "applied": False,
                    "verified": False,
                    "steps": [],
                    "errors": [
                        "the registry has no SSH user for this node; install its "
                        "agent from the Cluster page first, which records one"
                    ],
                    "readback": {},
                    "pings": [],
                    "privileged_calls": [],
                }
            )
            continue
        report = await fabric_apply.apply_node_plan(
            runtime.server,
            NodeAccess(host=node.address, username=node.ssh_user),
            node_plan,
            connector=onboarding.connector_factory(),
            sudo_password_prompt=sudo,
        )
        reports.append(report.to_dict())
    return {"mode": plan.mode, "reports": reports}
