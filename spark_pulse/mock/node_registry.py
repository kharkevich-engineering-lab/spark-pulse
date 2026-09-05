"""Simulated node registry.

The real registry persists to ``~/.config/spark-pulse/nodes.json``. This one
holds the same records in memory for the life of the process, the way
``mock.registry`` holds one simulated container registry: simulation must not
write into a developer's real config directory, and a browser session or a test
still needs a node it added a moment ago to still be there.

Everything that is *logic* rather than storage is the real module's, imported
and reused: the record and finding shapes, the id minting, and the whole of
:func:`diagnose`. Only the reads and writes are replaced. Two seeded nodes
deliberately share a machine-id so the duplicate-machine-id diagnostic — the
known defect on this hardware — has something to find in simulation.
"""

from __future__ import annotations

from typing import Any, Iterable

from spark_pulse.tools.node_registry import (  # noqa: F401 — re-exported shapes
    NODE_STATES,
    Finding,
    NodeRecord,
    mint_node_id,
    registry_path,
)
from spark_pulse.tools import node_registry as _real

__all__ = [
    "NODE_STATES",
    "Finding",
    "NodeRecord",
    "add_node",
    "diagnose",
    "get_node",
    "list_nodes",
    "mint_node_id",
    "read_machine_id",
    "register_self",
    "registry_path",
    "remove_node",
    "reset",
    "self_node",
    "update_node",
]

#: The machine-id both seeded Sparks report. Real hardware does this; the
#: diagnostic that names it is the whole reason identity is a minted id.
_SHARED_MACHINE_ID = "0f5c9e1a7b2d43c8a6e0f1928374b5c6"

_nodes: list[NodeRecord] = []


def _seed() -> list[NodeRecord]:
    return [
        NodeRecord(
            id="c0ntr01plane00000000000000000001",
            name="spark-01",
            address="192.168.1.100",
            is_control_plane=True,
            ethernet_interface="eth0",
            infiniband_interfaces=(),
            state="healthy",
            last_seen="2026-09-04T09:00:00+00:00",
            machine_id=_SHARED_MACHINE_ID,
        ),
        NodeRecord(
            id="9f3c1a2b4d5e6f708192a3b4c5d6e7f8",
            name="spark-02",
            address="10.0.0.11",
            ssh_user="spark",
            ethernet_interface="eth0",
            infiniband_interfaces=("ib0", "ib1"),
            state="unknown",
            last_seen="2026-09-04T08:41:00+00:00",
            machine_id=_SHARED_MACHINE_ID,
        ),
    ]


def reset() -> None:
    """Restore the seeded registry. Called between tests."""
    global _nodes
    _nodes = _seed()


reset()


def _sorted(nodes: Iterable[NodeRecord]) -> list[NodeRecord]:
    return sorted(nodes, key=lambda n: (not n.is_control_plane, n.label.lower()))


def list_nodes() -> list[NodeRecord]:
    """Mock: every node, control plane first."""
    return _sorted(_nodes)


def get_node(node_id: str) -> NodeRecord | None:
    """Mock: the node with ``node_id``, or ``None``."""
    for node in _nodes:
        if node.id == node_id:
            return node
    return None


def self_node() -> NodeRecord | None:
    """Mock: the control-plane record."""
    for node in _nodes:
        if node.is_control_plane:
            return node
    return None


def add_node(
    *,
    name: str = "",
    address: str = "",
    is_control_plane: bool = False,
    ssh_user: str = "",
    ssh_key_path: str = "",
    ethernet_interface: str = "",
    infiniband_interfaces: Iterable[str] = (),
    fabric_mode: str = "",
    state: str = "unknown",
    machine_id: str = "",
) -> NodeRecord:
    """Mock: add a node with a freshly minted id."""
    address = address.strip()
    name = name.strip()
    if not address and not is_control_plane:
        raise ValueError("a node needs an address")
    if state not in NODE_STATES:
        raise ValueError(f"state must be one of {', '.join(NODE_STATES)}")
    if address and any(n.address == address for n in _nodes):
        raise ValueError(f"a node with address {address} is already registered")
    if is_control_plane and any(n.is_control_plane for n in _nodes):
        raise ValueError("the control plane is already registered")

    node = NodeRecord(
        id=mint_node_id(),
        name=name or address,
        address=address,
        is_control_plane=is_control_plane,
        ssh_user=ssh_user.strip(),
        ssh_key_path=ssh_key_path.strip(),
        ethernet_interface=ethernet_interface.strip(),
        infiniband_interfaces=tuple(infiniband_interfaces),
        fabric_mode=_real._valid_fabric_mode(fabric_mode),
        state=state,
        machine_id=machine_id,
    )
    _nodes.append(node)
    return node


def update_node(node_id: str, **changes: Any) -> NodeRecord:
    """Mock: apply ``changes`` to one node."""
    from dataclasses import replace

    unknown = set(changes) - _real._UPDATABLE
    if unknown:
        raise ValueError(f"cannot change: {', '.join(sorted(unknown))}")
    if "state" in changes and changes["state"] not in NODE_STATES:
        raise ValueError(f"state must be one of {', '.join(NODE_STATES)}")
    if "infiniband_interfaces" in changes:
        changes["infiniband_interfaces"] = tuple(changes["infiniband_interfaces"])
    if "fabric_mode" in changes:
        raw = changes["fabric_mode"]
        changes["fabric_mode"] = _real._valid_fabric_mode(raw)
        if raw and not changes["fabric_mode"]:
            from spark_pulse.tools.discovery import FABRIC_MODES

            raise ValueError(
                f"fabric_mode must be one of {', '.join(FABRIC_MODES)}, or "
                "empty when it is not known"
            )

    for index, node in enumerate(_nodes):
        if node.id != node_id:
            continue
        updated = replace(node, **changes)
        if updated.address and any(
            other.address == updated.address and other.id != node_id for other in _nodes
        ):
            raise ValueError(
                f"a node with address {updated.address} is already registered"
            )
        _nodes[index] = updated
        return updated
    raise KeyError(node_id)


def remove_node(node_id: str) -> NodeRecord:
    """Mock: forget a node. The control plane cannot forget itself."""
    for index, node in enumerate(_nodes):
        if node.id != node_id:
            continue
        if node.is_control_plane:
            raise ValueError("the control plane cannot be removed from the registry")
        return _nodes.pop(index)
    raise KeyError(node_id)


def read_machine_id() -> str:
    """Mock: the seeded machine-id. Diagnostic only, never identity."""
    return _SHARED_MACHINE_ID


def register_self(*, name: str = "") -> NodeRecord:
    """Mock: the control node is seeded, so this only refreshes observations.

    Idempotent and blank-filling, exactly like the real one — an operator's
    edit to the seeded record survives a restart of the simulated app.
    """
    existing = self_node()
    if existing is None:
        return add_node(
            name=name or "spark-01",
            address="192.168.1.100",
            is_control_plane=True,
            ethernet_interface="eth0",
            state="healthy",
            machine_id=_SHARED_MACHINE_ID,
        )
    return update_node(existing.id, state="healthy", machine_id=_SHARED_MACHINE_ID)


def diagnose(nodes: list[NodeRecord] | None = None) -> list[Finding]:
    """Mock: the real checks, run against the simulated nodes and interfaces.

    The checks themselves are pure — they read a node list and the discovery
    module, and in simulation both of those are already the mock ones — so
    there is nothing here worth reimplementing.
    """
    return _real.diagnose(list_nodes() if nodes is None else nodes)
