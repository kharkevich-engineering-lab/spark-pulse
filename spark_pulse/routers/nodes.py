"""Node registry API — the persisted set of machines, and how peers are found.

Thin over :mod:`spark_pulse.tools.node_registry`, which owns every rule. Three
things about the shape are deliberate:

* ``POST`` never takes an id. Identity is minted server-side, so a client
  cannot name a node into existence twice or collide two machines onto one
  record. See the registry module's docstring for why that matters here.
* ``/discover`` and ``/diagnostics`` are declared before ``/{node_id}``.
  FastAPI matches in declaration order, so the literal routes have to come
  first or ``discover`` would arrive as a node id.
* ``/discover`` cannot fail. mDNS being unavailable is reported in the payload
  as ``mdns_available: false`` with an empty peer list, because "no peers
  found" is the honest answer and adding a node by address always works.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from spark_pulse import tools

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


def _node_payload(node: Any) -> dict[str, Any]:
    return node.to_dict()


def _peer_payload(peer: Any) -> dict[str, Any]:
    return {
        "address": peer.address,
        "port": peer.port,
        "service": peer.service,
        "hostname": peer.hostname,
        "instance": peer.instance,
        "node_id": peer.node_id,
        "version": peer.version,
        "is_spark_pulse": peer.is_spark_pulse,
    }


# ── Literal routes, before the parameterised one ─────────────────────────────


@router.get("/discover")
def discover(timeout: float = Query(3.0, ge=0.1, le=15.0)):
    """Browse the LAN for peers over mDNS.

    Returns both ``_spark-pulse._tcp`` responders, which identify themselves,
    and ``_ssh._tcp`` ones, which are what a Spark advertises before it has
    ever run Spark Pulse. ``registered`` marks the peers already in the
    registry so the UI does not offer to add them twice.
    """
    known = {node.address for node in tools.node_registry.list_nodes()}
    peers = tools.discovery.browse_peers(timeout=timeout)
    return {
        "mdns_available": tools.discovery.mdns_available(),
        "peers": [
            {**_peer_payload(peer), "registered": peer.address in known}
            for peer in peers
        ],
    }


@router.get("/diagnostics")
def diagnostics():
    """Findings about the cluster's identity and networking, with remedies.

    Never an error list: everything here is a condition the cluster runs with
    and that costs an afternoon when it is not named.
    """
    findings = tools.node_registry.diagnose()
    return {"findings": [finding.to_dict() for finding in findings]}


# ── The registry ─────────────────────────────────────────────────────────────


@router.get("")
def list_nodes():
    """Every registered node, control plane first."""
    return [_node_payload(node) for node in tools.node_registry.list_nodes()]


@router.post("")
def add_node(body: dict[str, Any] = Body(...)):
    """Register a node. The id is minted here and is not a client's to choose."""
    if "id" in body:
        raise HTTPException(
            status_code=400,
            detail="a node id is minted by the server and cannot be supplied",
        )
    try:
        node = tools.node_registry.add_node(
            name=str(body.get("name") or ""),
            address=str(body.get("address") or ""),
            ssh_user=str(body.get("ssh_user") or ""),
            ssh_key_path=str(body.get("ssh_key_path") or ""),
            ethernet_interface=str(body.get("ethernet_interface") or ""),
            infiniband_interfaces=[
                str(name) for name in (body.get("infiniband_interfaces") or [])
            ],
            state=str(body.get("state") or "unknown"),
            machine_id=str(body.get("machine_id") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _node_payload(node)


@router.get("/{node_id}")
def get_node(node_id: str):
    """One node by its minted id."""
    node = tools.node_registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"No such node: {node_id}")
    return _node_payload(node)


@router.patch("/{node_id}")
def update_node(node_id: str, body: dict[str, Any] = Body(...)):
    """Change a node's editable fields.

    ``id`` and ``is_control_plane`` are not among them: identity does not move,
    and which machine we are running on is not an editable opinion.
    """
    try:
        node = tools.node_registry.update_node(node_id, **body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No such node: {node_id}") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _node_payload(node)


@router.delete("/{node_id}")
def remove_node(node_id: str):
    """Forget a node.

    This is *forget* — it drops what we know about a machine that is already
    gone. Wiping a node's identity and uninstalling its agent while keeping
    that identity are separate actions, and they arrive with the agent.
    """
    try:
        node = tools.node_registry.remove_node(node_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No such node: {node_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"removed": True, "node": _node_payload(node)}
