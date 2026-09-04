"""Mock discovery provider for simulation mode.

Provides scenario-driven network discovery simulation without requiring
real network interfaces or InfiniBand hardware.

Scenarios:
    - single_gpu: eth0 only, no IB (typical single-GPU workstation)
    - dgx: eth0 + 4x mlx5 HCAs (DGX Spark / DGX-1)
    - multi_node: eth0 + IB + multiple IPs (multi-node cluster head)
    - broken_ib: IB installed but down (hardware/driver issue)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from spark_pulse.tools import discovery as _real
from spark_pulse.tools.discovery import (  # noqa: F401 — re-exported shapes
    FABRIC_DIRECT as FABRIC_DIRECT,
    FABRIC_MESH as FABRIC_MESH,
    FABRIC_MODES as FABRIC_MODES,
    MESH_NCCL_ENV as MESH_NCCL_ENV,
    DiscoveredPeer,
    DiscoveryResult,
    FabricConfig,
    InfinibandDevice,
    NCCLConfig,
    NetworkInterface,
    RoCEPort as RoCEPort,
    ValidationResult,
    build_fabric_config as build_fabric_config,
    fabric_from_output as fabric_from_output,
    parse_ibdev2netdev as parse_ibdev2netdev,
)


@dataclass
class MockDiscoveryProvider:
    """Scenario-driven discovery mock provider."""

    scenario: Literal["single_gpu", "dgx", "multi_node", "broken_ib"] = "single_gpu"

    def get_interfaces(self) -> list[NetworkInterface]:
        """Return mock interfaces for the current scenario."""
        if self.scenario == "single_gpu":
            return [
                NetworkInterface(
                    name="eth0",
                    ip="192.168.1.100",
                    mtu=1500,
                    is_up=True,
                    type="ethernet",
                ),
                NetworkInterface(
                    name="lo", ip="127.0.0.1", mtu=65536, is_up=True, type="loopback"
                ),
                NetworkInterface(
                    name="docker0", ip="172.17.0.1", mtu=1500, is_up=True, type="docker"
                ),
            ]
        if self.scenario == "dgx":
            return [
                NetworkInterface(
                    name="eth0", ip="10.0.0.10", mtu=1500, is_up=True, type="ethernet"
                ),
                NetworkInterface(
                    name="ib0", ip=None, mtu=4096, is_up=True, type="infiniband"
                ),
                NetworkInterface(
                    name="ib1", ip=None, mtu=4096, is_up=True, type="infiniband"
                ),
                NetworkInterface(
                    name="lo", ip="127.0.0.1", mtu=65536, is_up=True, type="loopback"
                ),
                NetworkInterface(
                    name="docker0", ip="172.17.0.1", mtu=1500, is_up=True, type="docker"
                ),
            ]
        if self.scenario == "multi_node":
            return [
                NetworkInterface(
                    name="eth0", ip="10.0.0.10", mtu=1500, is_up=True, type="ethernet"
                ),
                NetworkInterface(
                    name="ib0", ip=None, mtu=4096, is_up=True, type="infiniband"
                ),
                NetworkInterface(
                    name="ib1", ip=None, mtu=4096, is_up=True, type="infiniband"
                ),
                NetworkInterface(
                    name="ib2", ip=None, mtu=4096, is_up=True, type="infiniband"
                ),
                NetworkInterface(
                    name="ib3", ip=None, mtu=4096, is_up=True, type="infiniband"
                ),
                NetworkInterface(
                    name="lo", ip="127.0.0.1", mtu=65536, is_up=True, type="loopback"
                ),
            ]
        # broken_ib
        return [
            NetworkInterface(
                name="eth0", ip="192.168.1.50", mtu=1500, is_up=True, type="ethernet"
            ),
            NetworkInterface(
                name="ib0", ip=None, mtu=4096, is_up=False, type="infiniband"
            ),
            NetworkInterface(
                name="lo", ip="127.0.0.1", mtu=65536, is_up=True, type="loopback"
            ),
        ]

    def get_infiniband_devices(self) -> list[InfinibandDevice]:
        """Return mock InfiniBand devices for the current scenario."""
        if self.scenario == "single_gpu":
            return []
        if self.scenario == "dgx":
            return [
                InfinibandDevice(
                    hca="mlx5_0", ports=[1, 2], net_devices=["ib0"], state="ACTIVE"
                ),
                InfinibandDevice(
                    hca="mlx5_1", ports=[1, 2], net_devices=["ib1"], state="ACTIVE"
                ),
                InfinibandDevice(
                    hca="mlx5_2", ports=[1, 2], net_devices=[], state="ACTIVE"
                ),
                InfinibandDevice(
                    hca="mlx5_3", ports=[1, 2], net_devices=[], state="ACTIVE"
                ),
            ]
        if self.scenario == "multi_node":
            return [
                InfinibandDevice(
                    hca="mlx5_0", ports=[1, 2], net_devices=["ib0"], state="ACTIVE"
                ),
                InfinibandDevice(
                    hca="mlx5_1", ports=[1, 2], net_devices=["ib1"], state="ACTIVE"
                ),
                InfinibandDevice(
                    hca="mlx5_2", ports=[1, 2], net_devices=["ib2"], state="ACTIVE"
                ),
                InfinibandDevice(
                    hca="mlx5_3", ports=[1, 2], net_devices=["ib3"], state="ACTIVE"
                ),
            ]
        # broken_ib
        return [
            InfinibandDevice(
                hca="mlx5_0", ports=[1, 2], net_devices=["ib0"], state="DOWN"
            ),
        ]

    def get_local_ip(self) -> str | None:
        """Return mock local IP for the current scenario."""
        if self.scenario == "single_gpu":
            return "192.168.1.100"
        if self.scenario == "dgx":
            return "10.0.0.10"
        if self.scenario == "multi_node":
            return "10.0.0.10"
        return "192.168.1.50"

    def get_nccl_defaults(self) -> NCCLConfig | None:
        """Return mock NCCL defaults for the current scenario."""
        if self.scenario == "single_gpu":
            return NCCLConfig(socket_ifname="eth0", ib_hca=None, ib_disable=True)
        if self.scenario == "dgx":
            return NCCLConfig(socket_ifname="eth0", ib_hca="mlx5_0", ib_disable=False)
        if self.scenario == "multi_node":
            return NCCLConfig(socket_ifname="eth0", ib_hca="mlx5_0", ib_disable=False)
        # broken_ib — IB present but disabled
        return NCCLConfig(socket_ifname="eth0", ib_hca=None, ib_disable=True)

    def get_validation(self) -> ValidationResult:
        """Return mock validation results for the current scenario."""
        if self.scenario == "single_gpu":
            return ValidationResult(
                healthy=True,
                warnings=[],
                errors=[],
            )
        if self.scenario == "dgx":
            return ValidationResult(
                healthy=True,
                warnings=[],
                errors=[],
            )
        if self.scenario == "multi_node":
            return ValidationResult(
                healthy=True,
                warnings=["Port 29500 is already in use"],
                errors=[],
            )
        # broken_ib
        return ValidationResult(
            healthy=False,
            warnings=[
                "InfiniBand HCA mlx5_0 is DOWN (expected ACTIVE)",
                "No active InfiniBand HCAs found — NCCL will use Ethernet",
            ],
            errors=[],
        )

    def run_discovery(self) -> DiscoveryResult:
        """Run full mock discovery and return results."""
        interfaces = self.get_interfaces()
        ib_devices = self.get_infiniband_devices()
        local_ip = self.get_local_ip()
        nccl_defaults = self.get_nccl_defaults()

        # Find ethernet interface
        ethernet_if = None
        for iface in interfaces:
            if iface.type == "ethernet" and iface.is_up and iface.ip:
                ethernet_if = iface.name
                break

        return DiscoveryResult(
            local_ip=local_ip,
            ethernet_if=ethernet_if,
            infiniband_present=len(ib_devices) > 0,
            infiniband_devices=ib_devices,
            interfaces=interfaces,
            nccl_defaults=nccl_defaults,
        )


# ── Module-level convenience ─────────────────────────────────────────────────

_default_provider: MockDiscoveryProvider | None = None


def _get_default_provider() -> MockDiscoveryProvider:
    """Get or create the default mock provider (single_gpu scenario)."""
    global _default_provider
    if _default_provider is None:
        _default_provider = MockDiscoveryProvider(scenario="single_gpu")
    return _default_provider


def set_mock_scenario(
    scenario: Literal["single_gpu", "dgx", "multi_node", "broken_ib"],
) -> None:
    """Set the mock scenario for all subsequent discovery calls."""
    global _default_provider
    _default_provider = MockDiscoveryProvider(scenario=scenario)


def reset_mock_scenario() -> None:
    """Reset to default single_gpu scenario."""
    global _default_provider
    _default_provider = None


def detect_network_interfaces() -> list[NetworkInterface]:
    """Mock: return interfaces for current scenario."""
    return _get_default_provider().get_interfaces()


def detect_local_ip() -> str | None:
    """Mock: return local IP for current scenario."""
    return _get_default_provider().get_local_ip()


def detect_infiniband_devices() -> list[InfinibandDevice]:
    """Mock: return IB devices for current scenario."""
    return _get_default_provider().get_infiniband_devices()


def build_nccl_defaults(discovery: DiscoveryResult) -> NCCLConfig | None:
    """Mock: return NCCL defaults for current scenario."""
    return _get_default_provider().get_nccl_defaults()


def validate_network() -> ValidationResult:
    """Mock: return validation results for current scenario."""
    return _get_default_provider().get_validation()


def run_discovery() -> DiscoveryResult:
    """Mock: run full discovery for current scenario."""
    return _get_default_provider().run_discovery()


# ── Convenience aliases (matching tools/discovery.py) ─────────────────────────

get_network_interfaces = detect_network_interfaces
get_local_ip = detect_local_ip
get_infiniband_devices = detect_infiniband_devices


def get_nccl_defaults() -> NCCLConfig | None:
    """Mock convenience: return NCCL defaults for current scenario."""
    return _get_default_provider().get_nccl_defaults()


def check_network_health() -> ValidationResult:
    """Mock convenience: return validation results for current scenario."""
    return _get_default_provider().get_validation()


# ── Peer discovery (mDNS) ────────────────────────────────────────────────────
#
# The real module speaks multicast DNS. Simulation cannot, and must not: a test
# run that announced a service on the developer's LAN would be a surprise. So
# the peers here are canned, the announcement is a no-op that reports success,
# and the hostname history is writable so the churn diagnostic has something to
# find. The *shape* is the real module's, down to the service constants.

SPARK_PULSE_SERVICE = _real.SPARK_PULSE_SERVICE
SSH_SERVICE = _real.SSH_SERVICE

#: Two peers, deliberately unlike each other. ``spark-02`` runs Spark Pulse and
#: says so in its TXT record; ``spark-03`` is a bare DGX that only advertises
#: SSH, which is what a Spark looks like before it has ever been enrolled.
_MOCK_PEERS = [
    DiscoveredPeer(
        address="10.0.0.11",
        port=8100,
        service=SPARK_PULSE_SERVICE,
        hostname="spark-02.local",
        instance="spark-02-9f3c1a2b",
        node_id="9f3c1a2b4d5e6f708192a3b4c5d6e7f8",
        version="1.2.3",
    ),
    DiscoveredPeer(
        address="10.0.0.12",
        port=22,
        service=SSH_SERVICE,
        hostname="spark-03.local",
        instance="spark-03",
    ),
]

_mock_peers: list[DiscoveredPeer] = list(_MOCK_PEERS)
_mock_mdns_available = True
_mock_hostnames: dict[str, set[str]] = {}


def set_mock_peers(peers: list[DiscoveredPeer] | None) -> None:
    """Replace the canned peer list. ``None`` restores the default two."""
    global _mock_peers
    _mock_peers = list(_MOCK_PEERS if peers is None else peers)


def set_mdns_available(available: bool) -> None:
    """Simulate mDNS being unavailable, so the degraded path can be exercised."""
    global _mock_mdns_available
    _mock_mdns_available = bool(available)


def mdns_available() -> bool:
    """Mock: whether peer discovery would work."""
    return _mock_mdns_available


def browse_peers(timeout: float = 3.0) -> list[DiscoveredPeer]:
    """Mock: the canned peers, or none at all when mDNS is unavailable.

    Browsing records each responder's hostname the way the real module does, so
    the hostname-churn diagnostic sees the same input in both modes.
    """
    if not _mock_mdns_available:
        return []
    for peer in _mock_peers:
        record_mdns_hostname(peer.address, peer.hostname)
    return sorted(_mock_peers, key=lambda p: (p.address, p.service))


def announce_self(node_id: str, port: int, version: str) -> bool:
    """Mock: never touch the network. Reports whether it would have announced."""
    return _mock_mdns_available


def stop_announcement() -> None:
    """Mock: nothing was announced, so nothing is withdrawn."""


def record_mdns_hostname(address: str, hostname: str) -> None:
    """Mock: note that ``address`` answered under ``hostname``."""
    if not address or not hostname:
        return
    _mock_hostnames.setdefault(address, set()).add(hostname.rstrip("."))


def mdns_hostname_history() -> dict[str, set[str]]:
    """Mock: every address seen, mapped to the hostnames it used."""
    return {address: set(names) for address, names in _mock_hostnames.items()}


def reset_mdns_history() -> None:
    """Mock: forget the observed hostnames."""
    _mock_hostnames.clear()


# ── Real interfaces ──────────────────────────────────────────────────────────


def ibdev2netdev_up() -> list[str]:
    """Mock: the fabric links the scenario has up."""
    return [
        interface.name
        for interface in detect_network_interfaces()
        if interface.type == "infiniband" and interface.is_up
    ]


def is_real_interface(interface: NetworkInterface) -> bool:
    """Mock: delegates to the real predicate — it is pure name classification."""
    return _real.is_real_interface(interface)


def real_interfaces() -> list[NetworkInterface]:
    """Mock: the scenario's interfaces, minus loopback and container bridges."""
    return [i for i in detect_network_interfaces() if _real.is_real_interface(i)]


def real_interface_names() -> list[str]:
    """Mock: just the names."""
    return [interface.name for interface in real_interfaces()]


def announce_addresses() -> list[str]:
    """Mock: IPv4 addresses of the scenario's real interfaces."""
    return [i.ip for i in real_interfaces() if i.ip and i.ip != "127.0.0.1"]


def detect_link_local_addresses() -> dict[str, str]:
    """Mock: link-local addresses per interface.

    The fabric links deliberately have none, which is exactly what
    ``link-local: []`` in ``spark-vllm-docker``'s netplan profile produces — so
    the diagnostic that catches it has something to catch in simulation.
    """
    return {
        interface.name: "fe80::1"
        for interface in real_interfaces()
        if interface.type == "ethernet"
    }


def reset_mock_discovery() -> None:
    """Reset every mock-only piece of discovery state."""
    reset_mock_scenario()
    reset_mdns_history()
    set_mock_peers(None)
    set_mdns_available(True)


# ── The CX7 fabric ───────────────────────────────────────────────────────────
#
# Simulation answers with the ``ibdev2netdev`` output ``spark-vllm-docker``'s
# ``docs/NETWORKING.md`` prints at lines 22-27 — one cable in the outermost
# QSFP port, so two of the four RoCE devices up — and the addresses its netplan
# profile assigns at lines 95-112. The parsing and the rule application are the
# real module's; only the bytes are invented.

SIM_IBDEV2NETDEV = (
    "rocep1s0f0 port 1 ==> enp1s0f0np0 (Down)\n"
    "rocep1s0f1 port 1 ==> enp1s0f1np1 (Up)\n"
    "roceP2p1s0f0 port 1 ==> enP2p1s0f0np0 (Down)\n"
    "roceP2p1s0f1 port 1 ==> enP2p1s0f1np1 (Up)"
)

SIM_FABRIC_ADDRESSES = (
    "1: lo    inet 127.0.0.1/8 scope host lo\n"
    "2: enp1s0f1np1    inet 192.168.177.11/24 scope global enp1s0f1np1\n"
    "3: enP2p1s0f1np1    inet 192.168.178.11/24 scope global enP2p1s0f1np1"
)

#: Scenarios with no ConnectX at all. ``detect_fabric`` then reports no ports,
#: which is a warning rather than an error — a workstation is allowed to have
#: no fabric.
_FABRICLESS_SCENARIOS = ("single_gpu",)


def fabric_output() -> str:
    """What :data:`FABRIC_COMMAND` would print in the current scenario."""
    if _get_default_provider().scenario in _FABRICLESS_SCENARIOS:
        return "\n== addr\n" + SIM_FABRIC_ADDRESSES
    return SIM_IBDEV2NETDEV + "\n== addr\n" + SIM_FABRIC_ADDRESSES


def detect_fabric() -> FabricConfig:
    """Mock: the scenario's fabric, read by the real rules."""
    return _real.fabric_from_output(fabric_output())
