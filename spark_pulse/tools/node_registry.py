"""The node registry — the persisted set of machines this control plane knows.

Step one of ``docs/cluster-agent-plan.md`` section 7: *"Node registry including
the control node itself, populated from the existing discovery code."* Before
this module the cluster page had two free-text IP boxes whose contents vanished
on refresh, and nothing about a node survived a restart.

**Identity is a server-minted random id.** Not the hostname, and explicitly not
``/etc/machine-id``. Section 3.1 records the two reasons: DGX Sparks ship with
duplicate machine-ids, and every system surveyed that keys identity on a name
has a documented re-enrollment failure while every system that mints a random
id does not. k0s abandoned machine-id for identity in v1.30 for this exact
class of reason. So :func:`mint_node_id` is the only source of a node id, a
node keeps its id across a rename or a re-address, and the machine-id is read
only by :func:`read_machine_id` and used only by :func:`diagnose`, which warns
when two nodes report the same one.

State lives in ``~/.config/spark-pulse/nodes.json`` and goes through
:mod:`spark_pulse.tools.atomic_json`, so a crash mid-write cannot lose the
registry and an unreadable file is a hard error rather than an empty cluster.

:func:`register_self` is what puts the control node in the registry on first
run. It is idempotent across restarts and it **fills blanks only**: a value an
operator has typed is never replaced by a discovered one.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from spark_pulse.tools.atomic_json import read_state_file, write_json_atomic

logger = logging.getLogger(__name__)

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
    "self_node",
    "update_node",
]

#: Where the registry lives. Same directory as every other piece of durable
#: control-plane state.
_REGISTRY_PATH = Path.home() / ".config" / "spark-pulse" / "nodes.json"

#: The three states section 8 insists are shown as three states. ``unknown`` is
#: the one that matters: a node we cannot reach while its rank is still serving
#: is unverified, not failed.
NODE_STATES = ("healthy", "unknown", "dead")

#: Files systemd and dbus keep the machine-id in. Read for diagnostics only.
_MACHINE_ID_FILES = (
    Path("/etc/machine-id"),
    Path("/var/lib/dbus/machine-id"),
)


def registry_path() -> Path:
    """The file the registry is persisted to."""
    return _REGISTRY_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mint_node_id() -> str:
    """Mint a fresh node id.

    The *only* way a node id is produced. It is random, so it collides with
    nothing, survives a rename, and cannot be forged from a hostname that two
    machines happen to share.
    """
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class NodeRecord:
    """One machine the control plane knows about.

    Attributes:
        id: The minted identity. Never derived from a name or a machine-id.
        name: Display name. Free for an operator to change; identity does not
            move when it does.
        address: IP or hostname used to reach the node.
        is_control_plane: Whether this record is the machine we run on. Exactly
            one record should carry it.
        ssh_user: SSH login for a peer. Empty means "let ssh_config decide".
        ssh_key_path: Optional path to the private key for this node. The key
            itself never leaves the control plane; only a path is stored.
        ethernet_interface: Management interface name, for NCCL/GLOO pinning.
        infiniband_interfaces: Fabric interface names, in the order discovery
            reported them.
        state: One of :data:`NODE_STATES`.
        last_seen: ISO-8601 UTC timestamp of the last time we had contact, or
            ``None`` if we never have.
        machine_id: Diagnostic only. Present so :func:`diagnose` can warn about
            the duplicates this hardware ships with. Nothing keys on it.
    """

    id: str
    name: str = ""
    address: str = ""
    is_control_plane: bool = False
    ssh_user: str = ""
    ssh_key_path: str = ""
    ethernet_interface: str = ""
    infiniband_interfaces: tuple[str, ...] = ()
    state: str = "unknown"
    last_seen: str | None = None
    machine_id: str = ""

    @property
    def label(self) -> str:
        """Human-readable name for logs and error messages."""
        return self.name or self.address or self.id

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["infiniband_interfaces"] = list(self.infiniband_interfaces)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeRecord:
        """Build a record from persisted JSON, tolerating unknown keys.

        A record read back without an ``id`` is given one rather than dropped:
        losing a node because a file was hand-edited is worse than minting.
        """
        interfaces = data.get("infiniband_interfaces") or ()
        state = str(data.get("state") or "unknown")
        return cls(
            id=str(data.get("id") or mint_node_id()),
            name=str(data.get("name") or ""),
            address=str(data.get("address") or ""),
            is_control_plane=bool(data.get("is_control_plane", False)),
            ssh_user=str(data.get("ssh_user") or ""),
            ssh_key_path=str(data.get("ssh_key_path") or ""),
            ethernet_interface=str(data.get("ethernet_interface") or ""),
            infiniband_interfaces=tuple(str(name) for name in interfaces),
            state=state if state in NODE_STATES else "unknown",
            last_seen=data.get("last_seen") or None,
            machine_id=str(data.get("machine_id") or ""),
        )


@dataclass(frozen=True, slots=True)
class Finding:
    """One diagnostic result: what we saw, and what to do about it.

    Section 8 asks for *diagnostics rather than mysteries*. A finding is never
    an error — every one of these describes a condition the cluster can run
    with, and every one of them costs an afternoon when it is not named.
    """

    code: str
    severity: str  # "info" | "warning"
    summary: str
    remedy: str
    node_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["node_ids"] = list(self.node_ids)
        return data


# ── Persistence ──────────────────────────────────────────────────────────────


def list_nodes() -> list[NodeRecord]:
    """Every node in the registry, control plane first, then by name.

    Raises:
        StateFileError: The file exists but could not be read or parsed. A
            registry we cannot read is not an empty cluster.
    """
    data = read_state_file(registry_path(), expect=dict)
    if not data:
        return []
    raw = data.get("nodes")
    if not isinstance(raw, list):
        return []
    nodes = [NodeRecord.from_dict(item) for item in raw if isinstance(item, dict)]
    return _sorted(nodes)


def _sorted(nodes: Iterable[NodeRecord]) -> list[NodeRecord]:
    return sorted(nodes, key=lambda n: (not n.is_control_plane, n.label.lower()))


def _save(nodes: Iterable[NodeRecord]) -> None:
    write_json_atomic(
        registry_path(),
        {"version": 1, "nodes": [node.to_dict() for node in _sorted(nodes)]},
    )


def get_node(node_id: str) -> NodeRecord | None:
    """The node with ``node_id``, or ``None``."""
    for node in list_nodes():
        if node.id == node_id:
            return node
    return None


def self_node() -> NodeRecord | None:
    """The control-plane record, or ``None`` before :func:`register_self`."""
    for node in list_nodes():
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
    state: str = "unknown",
    machine_id: str = "",
) -> NodeRecord:
    """Add a node and return it, with a freshly minted id.

    The caller does not choose the id. Address is required for a peer — a node
    we cannot name an address for is not a node we can reach — but not for the
    control plane, which may not have discovered its own address yet.
    """
    address = address.strip()
    name = name.strip()
    if not address and not is_control_plane:
        raise ValueError("a node needs an address")
    if state not in NODE_STATES:
        raise ValueError(f"state must be one of {', '.join(NODE_STATES)}")

    nodes = list_nodes()
    if address and any(n.address == address for n in nodes):
        raise ValueError(f"a node with address {address} is already registered")
    if is_control_plane and any(n.is_control_plane for n in nodes):
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
        state=state,
        last_seen=_now() if is_control_plane else None,
        machine_id=machine_id,
    )
    nodes.append(node)
    _save(nodes)
    return node


#: Fields :func:`update_node` will change. ``id`` and ``is_control_plane`` are
#: absent on purpose: identity does not move, and which machine we are running
#: on is not an editable opinion.
_UPDATABLE = frozenset(
    {
        "name",
        "address",
        "ssh_user",
        "ssh_key_path",
        "ethernet_interface",
        "infiniband_interfaces",
        "state",
        "last_seen",
        "machine_id",
    }
)


def update_node(node_id: str, **changes: Any) -> NodeRecord:
    """Apply ``changes`` to one node and return the updated record.

    Raises:
        KeyError: No such node.
        ValueError: A field that may not be changed, or an invalid state.
    """
    unknown = set(changes) - _UPDATABLE
    if unknown:
        raise ValueError(f"cannot change: {', '.join(sorted(unknown))}")
    if "state" in changes and changes["state"] not in NODE_STATES:
        raise ValueError(f"state must be one of {', '.join(NODE_STATES)}")
    if "infiniband_interfaces" in changes:
        changes["infiniband_interfaces"] = tuple(changes["infiniband_interfaces"])

    nodes = list_nodes()
    for index, node in enumerate(nodes):
        if node.id != node_id:
            continue
        updated = replace(node, **changes)
        if updated.address and any(
            other.address == updated.address and other.id != node_id for other in nodes
        ):
            raise ValueError(
                f"a node with address {updated.address} is already registered"
            )
        nodes[index] = updated
        _save(nodes)
        return updated
    raise KeyError(node_id)


def remove_node(node_id: str) -> NodeRecord:
    """Forget a node. Returns the record that was removed.

    This is *forget*, the third of section 8's three removal actions: it drops
    what we know about a node that is already gone. Wiping a node's identity or
    uninstalling its agent are separate actions and arrive with the agent.

    Raises:
        KeyError: No such node.
        ValueError: The node is the control plane, which cannot forget itself.
    """
    nodes = list_nodes()
    for index, node in enumerate(nodes):
        if node.id != node_id:
            continue
        if node.is_control_plane:
            raise ValueError("the control plane cannot be removed from the registry")
        nodes.pop(index)
        _save(nodes)
        return node
    raise KeyError(node_id)


# ── The control node registers itself ────────────────────────────────────────


def read_machine_id() -> str:
    """The host's machine-id, for diagnostics only.

    Never used as identity — see this module's docstring. Returns ``""`` when
    the file is absent, which is every non-systemd host and every macOS
    developer machine.
    """
    for path in _MACHINE_ID_FILES:
        try:
            value = path.read_text().strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def _discovered_self() -> dict[str, Any]:
    """What the existing discovery code knows about this machine."""
    from spark_pulse.tools import discovery

    try:
        interfaces = discovery.detect_network_interfaces()
    except Exception as exc:  # pragma: no cover — discovery is best effort
        logger.debug("Interface detection failed: %s", exc)
        interfaces = []

    ethernet = ""
    for interface in interfaces:
        if interface.type == "ethernet" and interface.is_up and interface.ip:
            ethernet = interface.name
            break

    fabric = tuple(
        interface.name
        for interface in interfaces
        if interface.type == "infiniband" and interface.is_up
    )

    try:
        address = discovery.detect_local_ip() or ""
    except Exception as exc:  # pragma: no cover — discovery is best effort
        logger.debug("Local IP detection failed: %s", exc)
        address = ""

    return {
        "address": address,
        "ethernet_interface": ethernet,
        "infiniband_interfaces": fabric,
    }


def register_self(*, name: str = "") -> NodeRecord:
    """Ensure the control node is in the registry, and return it.

    Idempotent across restarts, and **fills blanks only**. On the first run the
    record is created from discovery. On every later run the discovered values
    are written into fields that are still empty and nothing else is touched,
    so an address or an interface name an operator corrected by hand survives
    the next restart. ``last_seen`` and ``machine_id`` are refreshed each time
    because they are observations, not settings.
    """
    import socket

    discovered = _discovered_self()
    default_name = name.strip() or socket.gethostname() or "control"

    existing = self_node()
    if existing is None:
        return add_node(
            name=default_name,
            is_control_plane=True,
            state="healthy",
            machine_id=read_machine_id(),
            **discovered,
        )

    changes: dict[str, Any] = {}
    if not existing.name:
        changes["name"] = default_name
    if not existing.address and discovered["address"]:
        changes["address"] = discovered["address"]
    if not existing.ethernet_interface and discovered["ethernet_interface"]:
        changes["ethernet_interface"] = discovered["ethernet_interface"]
    if not existing.infiniband_interfaces and discovered["infiniband_interfaces"]:
        changes["infiniband_interfaces"] = discovered["infiniband_interfaces"]
    changes["state"] = "healthy"
    changes["last_seen"] = _now()
    changes["machine_id"] = read_machine_id()
    return update_node(existing.id, **changes)


# ── Diagnostics ──────────────────────────────────────────────────────────────


@dataclass
class _Findings:
    items: list[Finding] = field(default_factory=list)

    def add(
        self,
        code: str,
        severity: str,
        summary: str,
        remedy: str,
        node_ids: Iterable[str] = (),
    ) -> None:
        self.items.append(
            Finding(
                code=code,
                severity=severity,
                summary=summary,
                remedy=remedy,
                node_ids=tuple(node_ids),
            )
        )


def diagnose(nodes: list[NodeRecord] | None = None) -> list[Finding]:
    """Check the things that otherwise cost an afternoon.

    Section 8's diagnostics panel. Each check reports a *finding* carrying its
    remedy, never an exception: every condition here is one the cluster can run
    with, and the cost of each is confusion rather than failure.

    * ``duplicate_machine_id`` — two nodes report the same ``/etc/machine-id``.
      A known defect on this hardware. Our identity is unaffected, but DHCP
      leases and mDNS responders keyed on it will fight.
    * ``mdns_hostname_churn`` — one address has answered under more than one
      mDNS hostname. That is what a duplicate machine-id looks like from the
      outside, and it is why a peer seems to move.
    * ``interface_no_link_local`` — an interface is up with no IPv6 link-local
      address, which silently disables every ``ff02::1`` peer sweep on that
      link. ``spark-vllm-docker``'s networking guide sets ``link-local: []``
      on the fabric interfaces and NVIDIA's playbook does not, so both
      configurations will be met.
    * ``mdns_unavailable`` — informational. Discovery degraded to an empty
      list; manual entry still works and nothing is broken.
    """
    from spark_pulse.tools import discovery

    if nodes is None:
        nodes = list_nodes()
    found = _Findings()

    _check_duplicate_machine_ids(found, nodes)
    _check_mdns_hostname_churn(found, discovery)
    _check_link_local(found, discovery)
    _check_mdns_available(found, discovery)

    return found.items


def _check_duplicate_machine_ids(found: _Findings, nodes: list[NodeRecord]) -> None:
    by_machine_id: dict[str, list[NodeRecord]] = {}
    for node in nodes:
        if node.machine_id:
            by_machine_id.setdefault(node.machine_id, []).append(node)
    for machine_id, sharing in by_machine_id.items():
        if len(sharing) < 2:
            continue
        names = ", ".join(node.label for node in sharing)
        found.add(
            "duplicate_machine_id",
            "warning",
            f"{len(sharing)} nodes report the same machine-id "
            f"{machine_id[:8]}…: {names}.",
            "Nothing in Spark Pulse keys on the machine-id, so enrollment is "
            "unaffected — but DHCP and mDNS do, which is why those peers seem "
            "to swap addresses. Regenerate it on all but one: "
            "sudo rm -f /etc/machine-id /var/lib/dbus/machine-id && "
            "sudo systemd-machine-id-setup && sudo reboot.",
            [node.id for node in sharing],
        )


def _check_mdns_hostname_churn(found: _Findings, discovery: Any) -> None:
    try:
        history = discovery.mdns_hostname_history()
    except Exception as exc:  # pragma: no cover — history is best effort
        logger.debug("mDNS history unavailable: %s", exc)
        return
    for address, hostnames in sorted(history.items()):
        if len(hostnames) < 2:
            continue
        seen = ", ".join(sorted(hostnames))
        found.add(
            "mdns_hostname_churn",
            "warning",
            f"{address} has answered mDNS under more than one hostname: {seen}.",
            "Two responders are claiming one address, or one host keeps "
            "renaming itself. Duplicate machine-ids are the usual cause on "
            "this hardware; check that finding first, then confirm each host's "
            "hostname with hostnamectl.",
        )


def _check_link_local(found: _Findings, discovery: Any) -> None:
    try:
        interfaces = discovery.detect_network_interfaces()
        link_local = discovery.detect_link_local_addresses()
    except Exception as exc:  # pragma: no cover — discovery is best effort
        logger.debug("Link-local detection failed: %s", exc)
        return
    missing = [
        interface.name
        for interface in interfaces
        if interface.is_up
        and interface.type in ("ethernet", "infiniband")
        and not link_local.get(interface.name)
    ]
    if not missing:
        return
    found.add(
        "interface_no_link_local",
        "warning",
        f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} up with "
        "no IPv6 link-local address.",
        "A peer sweep to ff02::1 goes nowhere on such a link, so nodes on it "
        "will never be discovered and must be added by address. This is what "
        "link-local: [] in a netplan profile does; remove it and run "
        "sudo netplan apply.",
    )


def _check_mdns_available(found: _Findings, discovery: Any) -> None:
    try:
        available = discovery.mdns_available()
    except Exception as exc:  # pragma: no cover — probing is best effort
        logger.debug("mDNS availability probe failed: %s", exc)
        return
    if available:
        return
    found.add(
        "mdns_unavailable",
        "info",
        "mDNS is not available, so peer discovery returns an empty list.",
        "Nothing is broken and adding a node by address always works. To get "
        "discovery back, install the zeroconf package and make sure UDP 5353 "
        "is not blocked on the management link.",
    )
