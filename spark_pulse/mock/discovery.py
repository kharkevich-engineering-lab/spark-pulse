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

from spark_pulse.tools.discovery import (
    DiscoveryResult,
    InfinibandDevice,
    NCCLConfig,
    NetworkInterface,
    ValidationResult,
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
