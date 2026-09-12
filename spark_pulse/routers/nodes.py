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
* ``/{node_id}/host-key`` then ``/{node_id}/install`` is how an agent gets onto
  a registered machine from the browser. Two calls, because the operator must
  see the host key's fingerprint before any secret is sent, and the install
  carries the fingerprint that was shown so a key that changed in between is
  refused. The credentials in the install body — a password, a private key
  and its passphrase, a sudo password — are used for that one call and stored
  nowhere: what the registry keeps afterwards is the SSH user, and what the
  node keeps is the control plane's public key.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from spark_pulse import tools
from spark_pulse.agent import onboarding
from spark_pulse.agent import runtime as agent_runtime
from spark_pulse.agent.bootstrap import ExistingIdentity
from spark_pulse.agent.bootstrap_transport import (
    AuthFailed,
    BootstrapError,
    HostKeyDeclined,
    Unreachable,
    UnusableKey,
)

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


def _node_payload(node: Any) -> dict[str, Any]:
    """The record, with what the agent transport knows laid over it.

    The registry's ``state`` is what was last *written*; the hub knows what is
    true now. When the transport is up and the node is enrolled, ``state`` is
    the hub's liveness, and ``agent`` says whether the machine has an agent at
    all — which is the difference between "add one" and "it is down".
    """
    data = node.to_dict()
    runtime = agent_runtime.current()
    if runtime is None:
        data["agent"] = {"enrolled": False, "connected": False}
        return data
    node_id = runtime.control_node_id if node.is_control_plane else node.id
    enrolled = bool(node_id) and runtime.server.ledger.get(node_id) is not None
    connected = enrolled and runtime.hub.is_connected(node_id)
    data["agent"] = {"enrolled": enrolled, "connected": connected}
    if enrolled:
        data["state"] = runtime.hub.liveness(node_id).value
    return data


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
        # Every address and service this machine answered on. One host
        # advertises once per address family per interface, so these are what
        # let the UI show it once rather than six times.
        "addresses": list(getattr(peer, "addresses", ()) or [peer.address]),
        "services": list(getattr(peer, "services", ()) or [peer.service]),
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
            {
                **_peer_payload(peer),
                # Match on every address it answered on: a node registered by
                # its IPv6 address is still the same machine.
                "registered": bool(
                    known & set(getattr(peer, "addresses", ()) or [peer.address])
                ),
            }
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
            fabric_mode=str(body.get("fabric_mode") or ""),
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


@router.get("/{node_id}/host-key")
async def node_host_key(node_id: str, port: int = Query(22, ge=1, le=65535)):
    """The SSH host key the node offers, for the operator to confirm.

    Nothing is sent to the node here — no username, no secret — so this is
    safe to call on an address that turns out to be the wrong machine.
    """
    node = _peer_or_404(node_id)
    try:
        key = await onboarding.host_key_of(node.address, port)
    except Unreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except BootstrapError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "host": key.host,
        "port": key.port,
        "algorithm": key.algorithm,
        "fingerprint": key.fingerprint,
    }


@router.post("/{node_id}/install")
async def install_node_agent(node_id: str, body: dict[str, Any] = Body(...)):
    """Install, enrol and start the agent on a registered node.

    The body carries the SSH user, how to authenticate (``password``, ``key``
    with an optional ``passphrase``, or ``control_plane_key``), an optional
    ``sudo_password`` for a node that needs one, and the
    ``host_key_fingerprint`` the operator confirmed. The answer is the
    installer's report: what was probed, what scope was chosen and why, every
    privileged call, and whether the agent has dialled home.
    """
    node = _peer_or_404(node_id)
    runtime = agent_runtime.current()
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail="the agent transport is not running, so no node can enrol",
        )
    try:
        request = onboarding.parse_request(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    control_host = request.control_host or _control_address()
    if not control_host:
        raise HTTPException(
            status_code=400,
            detail=(
                "the control node has no address for the new node to dial; set "
                "one on the control plane's own entry, or pass control_host"
            ),
        )
    try:
        report = await onboarding.onboard(
            runtime.server,
            request,
            host=node.address,
            control_host=control_host,
            name=node.name or node.address,
            node_id=node.id,
        )
    except UnusableKey as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HostKeyDeclined as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ExistingIdentity as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AuthFailed as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Unreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except BootstrapError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    changes: dict[str, Any] = {"ssh_user": request.username}
    if report.get("connected"):
        changes["state"] = "healthy"
        changes["last_seen"] = datetime.now(timezone.utc).isoformat()
    tools.node_registry.update_node(node.id, **changes)
    report["node"] = _node_payload(tools.node_registry.get_node(node.id))
    return report


def _peer_or_404(node_id: str) -> Any:
    node = tools.node_registry.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"No such node: {node_id}")
    if node.is_control_plane:
        raise HTTPException(
            status_code=400,
            detail="the control node runs its own agent; there is nothing to install",
        )
    if not node.address:
        raise HTTPException(status_code=400, detail=f"{node.name} has no address")
    return node


def _control_address() -> str:
    """The address peers dial: the control plane's own registry entry."""
    control = tools.node_registry.self_node()
    return str(getattr(control, "address", "") or "")


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
