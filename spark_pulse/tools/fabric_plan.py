"""The ConnectX-7 fabric, planned for every node at once and written per node.

``spark-vllm-docker``'s ``NETWORKING.md`` configures the fabric by hand: one
netplan file per Spark, a static ``/24`` per cable with ``.11``, ``.12``,
``.13`` for the machines, MTU 9000, IPv6 link-local off. Everything in this
module is that page as a function: given what each node's agent reports about
its ports, produce the file each node should hold, and refuse — in upstream's
own terms — the shapes upstream refuses.

Pure, on purpose. Nothing here touches a machine; :mod:`spark_pulse.agent.fabric_apply`
is what writes the files, and the pre-flight is what reads the result back
through the agent. Keeping the plan pure is what makes it testable against
``NETWORKING.md``'s own examples, byte for byte.

**What the plan will and will not decide.**

* The *shape* is read from the cabling, exactly as :func:`build_fabric_config`
  reads it: one physical port up on every node is a pair or a switch, two is
  the three-node mesh. A cluster whose nodes disagree is refused.
* A node whose fabric is already valid — an address on the lowercase twin,
  the twins on different subnets, MTU 9000 — is left alone and reported as
  configured, whether or not its addresses follow upstream's scheme. Somebody
  chose those; the plan does not overrule them unless asked.
* A mesh is addressed from upstream's worked example (``NETWORKING.md``
  lines 162-252), which assumes the cabling that example draws. The apply step
  pings every peer over every link, so a cabling that differs from the drawing
  is a verification failure with the link named, not a silent mis-address.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

from spark_pulse.tools.discovery import (
    FABRIC_DIRECT,
    FABRIC_DUAL,
    FABRIC_MESH,
    FabricConfig,
    RoCEPort,
    _has_capital_p,
    _twin_key,
)

__all__ = [
    "FABRIC_MTU",
    "NETPLAN_PATH",
    "STATUS_CONFIGURED",
    "STATUS_PROPOSED",
    "STATUS_REFUSED",
    "STATUS_UNKNOWN",
    "Assignment",
    "FabricPlan",
    "NodeFabric",
    "NodePlan",
    "plan_fabric",
    "render_netplan",
]

#: NETWORKING.md line 104: jumbo frames on every fabric port.
FABRIC_MTU = 9000
#: NETWORKING.md line 95: the file each Spark holds.
NETPLAN_PATH = "/etc/netplan/40-cx7.yaml"
#: NETWORKING.md line 9: ``.11`` and ``.12`` for the two nodes, ``.13`` for a third.
FIRST_HOST = 11
#: The ``/24``s upstream uses, third octet only. A cable is a pair of subnets:
#: the lowercase twin takes the first, the capital-P twin the second.
PAIR_LINK = (177, 178)
#: The three cables of the mesh, in NETWORKING.md's own order. A pair with
#: both cables uses the first two: one per cable, on both nodes.
MESH_LINKS = ((177, 178), (187, 188), (197, 198))
#: NVIDIA's ring rule, said once so an operator cables it right first time.
MESH_CABLING = (
    "Cable the ring as NVIDIA's three-Spark playbook does: node 1 port 0 to "
    "node 2 port 1, node 2 port 0 to node 3 port 1, node 3 port 0 to node 1 "
    "port 1 — port 0 is the QSFP port next to the RJ-45. The apply step pings "
    "every peer over every cable, so a ring cabled differently is a named "
    "failure, not a silent mis-address."
)
MESH_MANAGEMENT = (
    "A ring carries every cable in NCCL's rings, so it coordinates over the "
    "10G RJ-45 port (enP7s7). NVIDIA's and upstream's launchers both pin it; "
    "Wi-Fi works with a warning and is slower."
)
SECOND_CABLE = (
    "One cable already carries both RoCE twins of its port (200G). NVIDIA "
    "allows a second cable between two Sparks with all four interfaces "
    "addressed; spark-vllm-docker measured no noticeable gain. Plug it in or "
    "not — either shape is planned."
)
#: Which cable each node's two up ports carry in the mesh drawing (lines
#: 56-80): node 0's ports carry cables 0 and 1, node 1's carry 2 and 0, node
#: 2's carry 1 and 2. That is the drawing; the apply step verifies it.
MESH_PORT_LINKS = ((0, 1), (2, 0), (1, 2))
SUBNET_PREFIX = "192.168"

STATUS_CONFIGURED = "configured"
STATUS_PROPOSED = "proposed"
STATUS_UNKNOWN = "unknown"
STATUS_REFUSED = "refused"


@dataclass(frozen=True)
class NodeFabric:
    """What one node's agent said about its ports. The plan's input."""

    node_id: str
    name: str
    is_control_plane: bool = False
    #: ``None`` when the node has no agent facts to read.
    fabric: FabricConfig | None = None
    #: Netdev name to its current MTU.
    mtus: dict[str, int] = field(default_factory=dict)
    #: Whether the wired 10G management port has link. ``None`` when unknown.
    #: A mesh coordinates over it; a pair does not need it.
    wired_management_up: bool | None = None


@dataclass(frozen=True)
class Assignment:
    """One fabric netdev and what it should carry."""

    netdev: str
    hca: str
    cidr: str
    mtu: int = FABRIC_MTU
    current_cidr: str = ""
    current_mtu: int = 0
    #: ``(node name, address)`` of every machine on this subnet — what the
    #: apply step pings to prove the cable goes where the plan thinks.
    peers: tuple[tuple[str, str], ...] = ()

    @property
    def changes(self) -> bool:
        return self.cidr != self.current_cidr or self.mtu != self.current_mtu

    def to_dict(self) -> dict[str, Any]:
        return {
            "netdev": self.netdev,
            "hca": self.hca,
            "cidr": self.cidr,
            "mtu": self.mtu,
            "current_cidr": self.current_cidr,
            "current_mtu": self.current_mtu,
            "changes": self.changes,
            "peers": [{"name": n, "address": a} for n, a in self.peers],
        }


@dataclass(frozen=True)
class NodePlan:
    node_id: str
    name: str
    status: str
    is_control_plane: bool = False
    assignments: tuple[Assignment, ...] = ()
    #: Why the status is what it is, in upstream's words where upstream has them.
    reasons: tuple[str, ...] = ()

    @property
    def netplan(self) -> str:
        return render_netplan(self.assignments) if self.assignments else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "status": self.status,
            "is_control_plane": self.is_control_plane,
            "assignments": [a.to_dict() for a in self.assignments],
            "reasons": list(self.reasons),
            "netplan": self.netplan,
            "netplan_path": NETPLAN_PATH,
        }


@dataclass(frozen=True)
class FabricPlan:
    mode: str
    nodes: tuple[NodePlan, ...] = ()
    problems: tuple[str, ...] = ()
    #: What an operator should know about this shape. Not problems: nothing
    #: here stops an apply.
    advice: tuple[str, ...] = ()

    @property
    def proposed(self) -> tuple[NodePlan, ...]:
        return tuple(n for n in self.nodes if n.status == STATUS_PROPOSED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "nodes": [n.to_dict() for n in self.nodes],
            "problems": list(self.problems),
            "advice": list(self.advice),
            "proposed": [n.node_id for n in self.proposed],
        }


# ── Rendering ────────────────────────────────────────────────────────────────


def render_netplan(assignments: tuple[Assignment, ...] | list[Assignment]) -> str:
    """The file NETWORKING.md lines 95-131 write by hand, for these assignments."""
    lines = [
        "# Managed by Spark Pulse — the ConnectX fabric.",
        "# A static /24 per cable, jumbo frames, and no IPv6 link-local so that",
        "# nothing but these addresses lives on the fabric. Edit this in the",
        "# fabric card; a re-apply overwrites the file.",
        "network:",
        "  version: 2",
        "  ethernets:",
    ]
    for assignment in assignments:
        lines += [
            f"    {assignment.netdev}:",
            "      dhcp4: false",
            "      dhcp6: false",
            "      link-local: []",
            f"      mtu: {assignment.mtu}",
            f"      addresses: [{assignment.cidr}]",
        ]
    return "\n".join(lines) + "\n"


# ── Planning ─────────────────────────────────────────────────────────────────


def _physical_ports(fabric: FabricConfig) -> list[tuple[RoCEPort, ...]]:
    """Up ports grouped by the QSFP port they share, in ``ibdev2netdev`` order."""
    groups: dict[str, list[RoCEPort]] = {}
    order: list[str] = []
    for port in fabric.up_ports:
        key = _twin_key(port.netdev)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(port)
    return [tuple(groups[key]) for key in order]


def _addressed_twin_first(twins: tuple[RoCEPort, ...]) -> tuple[RoCEPort, ...]:
    """The lowercase twin first: NETWORKING.md line 37 puts the address on it."""
    return tuple(sorted(twins, key=lambda p: _has_capital_p(p.netdev)))


def _already_valid(node: NodeFabric, fabric: FabricConfig) -> bool:
    """A fabric somebody configured, correctly, that the plan must not touch."""
    if fabric.errors or not fabric.mode:
        return False
    for port in fabric.up_ports:
        if port.netdev not in fabric.addresses:
            return False
        if node.mtus.get(port.netdev, 0) != FABRIC_MTU:
            return False
    return True


def _subnet(link: int) -> str:
    return f"{SUBNET_PREFIX}.{link}"


def plan_fabric(nodes: list[NodeFabric], *, override: bool = False) -> FabricPlan:
    """Plan every node's fabric from what its agent reported.

    ``override`` re-addresses nodes whose fabric is already valid too, so a
    cluster half on somebody's own scheme and half on upstream's can be made
    consistent — the one case where leaving a valid node alone is wrong.
    """
    problems: list[str] = []
    planned: list[NodePlan] = []
    shapes: dict[str, int] = {}

    for node in nodes:
        if node.fabric is None:
            planned.append(
                NodePlan(
                    node.node_id,
                    node.name,
                    STATUS_UNKNOWN,
                    node.is_control_plane,
                    reasons=(
                        "no agent has reported this node's ports; install or "
                        "reconnect its agent first",
                    ),
                )
            )
            continue
        shapes[node.node_id] = len(node.fabric.up_ports)

    counts = set(shapes.values())
    if len(counts) > 1:
        described = ", ".join(
            f"{n.name}: {shapes[n.node_id]} up" for n in nodes if n.node_id in shapes
        )
        problems.append(
            "the nodes are not cabled alike, so no one shape fits them all "
            f"({described}). A pair or a switch has two CX7 interfaces up per "
            "node — one cable, both twins — and the mesh has four."
        )
    advice: list[str] = []
    ports_up = next(iter(counts)) if len(counts) == 1 else 0
    if ports_up == 2:
        mode = FABRIC_DIRECT
        advice.append(SECOND_CABLE)
    elif ports_up == 4 and len(shapes) == 2:
        # Both cables between two Sparks: not the mesh, whatever the port
        # count says, and not given the mesh's NCCL settings.
        mode = FABRIC_DUAL
        advice.append(SECOND_CABLE)
    elif ports_up == 4:
        mode = FABRIC_MESH
        advice.append(MESH_CABLING)
        advice.append(MESH_MANAGEMENT)
        if len(shapes) != 3:
            problems.append(
                "four CX7 interfaces up per node is the switchless three-node "
                f"mesh, and there are {len(shapes)} node(s) reporting ports"
            )
        unwired = [
            n.name
            for n in nodes
            if n.node_id in shapes and n.wired_management_up is False
        ]
        if unwired:
            problems.append(
                f"{', '.join(unwired)}: the 10G RJ-45 port (enP7s7) has no "
                "link. A ring coordinates over it; cable every node's RJ-45 "
                "to the same switch, or accept Wi-Fi coordination with the "
                "warning it carries."
            )
    else:
        mode = ""
        if ports_up:
            # autodiscover.sh line 193, the refusal by number.
            problems.append(
                f"unexpected number of active CX7 interfaces ({ports_up}); "
                "expected 2 (one cable — a pair, or a QSFP switch) or 4 (the "
                "switchless three-node mesh). Check the cabling and that every "
                "intended interface is up."
            )

    # Addresses first, so peers can be cross-referenced afterwards.
    proposals: dict[str, list[Assignment]] = {}
    with_ports = [n for n in nodes if n.node_id in shapes]
    for index, node in enumerate(with_ports):
        fabric = node.fabric
        assert fabric is not None
        physical = _physical_ports(fabric)
        keep = _already_valid(node, fabric) and not override
        assignments: list[Assignment] = []
        for port_index, twins in enumerate(physical):
            if mode == FABRIC_DIRECT:
                links = PAIR_LINK
            elif mode == FABRIC_DUAL and port_index < len(MESH_LINKS):
                links = MESH_LINKS[port_index]
            elif mode == FABRIC_MESH and index < len(MESH_PORT_LINKS):
                links = MESH_LINKS[MESH_PORT_LINKS[index][port_index]]
            else:
                links = ()
            for twin_index, port in enumerate(_addressed_twin_first(twins)):
                current = fabric.addresses.get(port.netdev, "")
                current_mtu = node.mtus.get(port.netdev, 0)
                if keep:
                    cidr = current
                elif links and twin_index < len(links):
                    cidr = f"{_subnet(links[twin_index])}.{FIRST_HOST + index}/24"
                else:
                    cidr = ""
                assignments.append(
                    Assignment(
                        netdev=port.netdev,
                        hca=port.hca,
                        cidr=cidr,
                        mtu=current_mtu if keep else FABRIC_MTU,
                        current_cidr=current,
                        current_mtu=current_mtu,
                    )
                )
        proposals[node.node_id] = assignments

    # Peers: every other node with an address on the same subnet.
    def network(cidr: str) -> str:
        try:
            return str(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            return ""

    for node in with_ports:
        fabric = node.fabric
        assert fabric is not None
        assignments = proposals[node.node_id]
        finished: list[Assignment] = []
        for assignment in assignments:
            peers: list[tuple[str, str]] = []
            here = network(assignment.cidr)
            if here:
                for other in with_ports:
                    if other.node_id == node.node_id:
                        continue
                    for theirs in proposals[other.node_id]:
                        if theirs.cidr and network(theirs.cidr) == here:
                            peers.append((other.name, theirs.cidr.split("/")[0]))
            finished.append(
                Assignment(
                    netdev=assignment.netdev,
                    hca=assignment.hca,
                    cidr=assignment.cidr,
                    mtu=assignment.mtu,
                    current_cidr=assignment.current_cidr,
                    current_mtu=assignment.current_mtu,
                    peers=tuple(peers),
                )
            )
        keep = _already_valid(node, fabric) and not override
        reasons: list[str] = []
        if keep:
            status = STATUS_CONFIGURED
            reasons.append(
                "every cabled twin has an address on its own subnet and jumbo "
                "frames; left as it is"
            )
            if any(not a.peers for a in finished):
                reasons.append(
                    "no other node shares this node's subnets, so its addresses "
                    "follow a scheme of their own; apply with override to bring "
                    "every node onto one scheme"
                )
        elif not mode or any(not a.cidr for a in finished):
            status = STATUS_REFUSED
            reasons.extend(fabric.errors)
        else:
            status = STATUS_PROPOSED
            reasons.extend(fabric.errors)
            if not fabric.errors:
                reasons.append(
                    "addressed, but not with jumbo frames"
                    if all(a.current_cidr for a in finished)
                    else "no address on a cabled port"
                )
        planned.append(
            NodePlan(
                node.node_id,
                node.name,
                status,
                node.is_control_plane,
                tuple(finished),
                tuple(reasons),
            )
        )

    order = {n.node_id: i for i, n in enumerate(nodes)}
    planned.sort(key=lambda p: order[p.node_id])
    return FabricPlan(
        mode=mode,
        nodes=tuple(planned),
        problems=tuple(problems),
        advice=tuple(advice),
    )
