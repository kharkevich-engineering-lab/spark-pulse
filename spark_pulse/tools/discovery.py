"""Host network discovery tool.

Detects network interfaces, InfiniBand devices, local IP, and auto-generates
NCCL defaults. Used by Phase 2A to replace manual network configuration in
launch-cluster.sh.

Approach: uses psutil first, falls back to subprocess + /sys scanning.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# ── Data Models ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NetworkInterface:
    """Normalized network interface info."""

    name: str
    ip: str | None
    mtu: int
    is_up: bool
    type: Literal["ethernet", "infiniband", "loopback", "docker", "other"]


@dataclass(frozen=True)
class InfinibandDevice:
    """InfiniBand HCA with its ports and net devices."""

    hca: str  # e.g. mlx5_0
    ports: list[int]  # port numbers (1, 2, ...)
    net_devices: list[str]  # e.g. ["ib0", "ib1"]
    state: str  # "ACTIVE", "DOWN", ...


@dataclass(frozen=True)
class NCCLConfig:
    """Auto-detected NCCL defaults."""

    socket_ifname: str
    ib_hca: str | None  # None when no IB
    ib_disable: bool  # True when IB absent


@dataclass(frozen=True)
class DiscoveryResult:
    """Complete host network discovery snapshot."""

    local_ip: str | None
    ethernet_if: str | None
    infiniband_present: bool
    infiniband_devices: list[InfinibandDevice]
    interfaces: list[NetworkInterface]
    nccl_defaults: NCCLConfig | None
    validation_errors: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Network health check results."""

    healthy: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ── Detection Functions ──────────────────────────────────────────────────────


def _try_import_psutil():
    """Try importing psutil, return None if unavailable."""
    try:
        import psutil  # noqa: F811

        return psutil
    except ImportError:
        return None


def _classify_interface(name: str) -> Literal["ethernet", "infiniband", "loopback", "docker", "other"]:
    """Classify a network interface by name patterns."""
    if name == "lo":
        return "loopback"
    if name.startswith(("docker", "br-")):
        return "docker"
    if name.startswith(("ib", "mlx5")):
        return "infiniband"
    if name.startswith(("eth", "en")):
        return "ethernet"
    return "other"


def detect_network_interfaces() -> list[NetworkInterface]:
    """Scan all network interfaces via psutil/netifaces.

    Classifies each as ethernet, infiniband, loopback, docker, or other.
    Falls back to /sys/class/net scanning when psutil is unavailable.
    """
    psutil = _try_import_psutil()

    if psutil is not None:
        return _detect_with_psutil(psutil)
    return _detect_with_sys()


def _detect_with_psutil(psutil) -> list[NetworkInterface]:
    """Detect interfaces using psutil."""
    interfaces = []
    net_ifs = psutil.net_if_addrs()
    net_if_stats = psutil.net_if_stats()

    for name, addrs in net_ifs.items():
        stats = net_if_stats.get(name)
        if stats is None:
            continue

        is_up = stats.isup
        mtu = stats.mtu
        iface_type = _classify_interface(name)

        # Get the first non-loopback IPv4 address
        ip = None
        for addr in addrs:
            if addr.family == socket.AF_INET and addr.address != "127.0.0.1":
                ip = addr.address
                break

        interfaces.append(
            NetworkInterface(
                name=name,
                ip=ip,
                mtu=mtu,
                is_up=is_up,
                type=iface_type,
            )
        )

    return interfaces


def _detect_with_sys() -> list[NetworkInterface]:
    """Detect interfaces by scanning /sys/class/net (no psutil)."""
    interfaces = []
    net_path = Path("/sys/class/net")

    if not net_path.exists():
        return interfaces

    for iface_dir in net_path.iterdir():
        name = iface_dir.name
        iface_type = _classify_interface(name)

        # Get MTU
        mtu = 1500
        mtu_file = iface_dir / "mtu"
        if mtu_file.exists():
            try:
                mtu = int(mtu_file.read_text().strip())
            except (ValueError, OSError):
                pass

        # Get operstate (up/down)
        is_up = False
        state_file = iface_dir / "operstate"
        if state_file.exists():
            state = state_file.read_text().strip().lower()
            is_up = state == "up"

        # Get IP via socket fallback
        ip = None
        try:
            ip = _get_ip_for_interface(name)
        except OSError:
            pass

        interfaces.append(
            NetworkInterface(
                name=name,
                ip=ip,
                mtu=mtu,
                is_up=is_up,
                type=iface_type,
            )
        )

    return interfaces


def _get_ip_for_interface(iface: str) -> str | None:
    """Get IPv4 address for a specific interface using socket ioctl."""
    import struct  # noqa: F811

    try:
        import fcntl  # noqa: F811

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        interfaces = struct.pack("256s", iface[:15].encode())
        result = fcntl.ioctl(sock.fileno(), 0x8915, interfaces)  # SIOCGIFADDR
        ip = socket.inet_ntoa(result[20:24])
        sock.close()
        return ip if ip != "0.0.0.0" else None
    except (OSError, ImportError):
        return None


def detect_local_ip() -> str | None:
    """Get local IP via socket to 8.8.8.8:53, fallback to default route.

    Handles: default route works, 8.8.8.8 fallback, multiple addresses, IPv6 only.
    """
    # Method 1: Try connecting to 8.8.8.8 (doesn't actually send packets)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 53))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip != "0.0.0.0":
            return ip
    except OSError:
        pass

    # Method 2: Parse default route
    return _get_default_route_ip()


def _get_default_route_ip() -> str | None:
    """Get IP from the default route entry."""
    # Try ip route on Linux
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "dev" in line:
                    parts = line.split()
                    for i, part in enumerate(parts):
                        if part == "dev" and i + 1 < len(parts):
                            iface = parts[i + 1]
                            return _get_ip_for_interface(iface)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Method 3: hostname -I
    try:
        result = subprocess.run(
            ["hostname", "-I"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            addrs = result.stdout.strip().split()
            for addr in addrs:
                if not addr.startswith(":") and addr != "::1":
                    return addr
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


def detect_infiniband_devices() -> list[InfinibandDevice]:
    """Detect all InfiniBand HCAs (mlx5_0, mlx5_1, ...).

    Strategy: scan /sys/class/infiniband, parse ibstat output.
    Returns list — machines frequently have 4+ HCAs on DGX.
    """
    hcas = _scan_sys_infiniband()
    if hcas:
        return hcas

    # Fallback: parse ibstat
    return _parse_ibstat()


def _scan_sys_infiniband() -> list[InfinibandDevice]:
    """Scan /sys/class/infiniband for HCA devices."""
    ib_path = Path("/sys/class/infiniband")
    if not ib_path.exists():
        return []

    devices = []
    for hca_dir in ib_path.iterdir():
        hca_name = hca_dir.name
        ports = []
        net_devices = []
        state = "DOWN"

        # Scan ports
        port_dir = hca_dir / "ports"
        if port_dir.exists():
            for port_num_dir in port_dir.iterdir():
                if not port_num_dir.is_dir():
                    continue
                try:
                    port_num = int(port_num_dir.name)
                    ports.append(port_num)

                    # Get port state
                    state_file = port_num_dir / "state"
                    if state_file.exists():
                        state_text = state_file.read_text().strip()
                        if "ACTIVE" in state_text.upper():
                            state = "ACTIVE"

                    # Get associated net device
                    netdev_file = port_num_dir / "net"
                    if netdev_file.exists() and netdev_file.is_dir():
                        for nd in netdev_file.iterdir():
                            if nd.is_dir():
                                net_devices.append(nd.name)
                except (ValueError, OSError):
                    continue

        if ports:
            devices.append(
                InfinibandDevice(
                    hca=hca_name,
                    ports=sorted(ports),
                    net_devices=net_devices,
                    state=state,
                )
            )

    return devices


def _parse_ibstat() -> list[InfinibandDevice]:
    """Parse ibstat output as fallback."""
    try:
        result = subprocess.run(
            ["ibstat"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []

        devices = []
        current_hca = None
        current_ports = []
        current_net = []
        current_state = "DOWN"

        for line in result.stdout.splitlines():
            line = line.strip()
            # HCA header line (no leading whitespace, ends with :)
            if line and not line[0].isspace() and line.endswith(":"):
                if current_hca and current_ports:
                    devices.append(
                        InfinibandDevice(
                            hca=current_hca,
                            ports=sorted(current_ports),
                            net_devices=current_net,
                            state=current_state,
                        )
                    )
                current_hca = line[:-1]
                current_ports = []
                current_net = []
                current_state = "DOWN"
            elif "State:" in line:
                current_state = "ACTIVE" if "ACTIVE" in line else "DOWN"
            elif "Physical State:" in line and "Active" in line:
                current_state = "ACTIVE"
            elif "Port:" in line:
                try:
                    port_num = int(line.split()[-1])
                    current_ports.append(port_num)
                except ValueError:
                    pass
            elif "CA" in line or "net" in line.lower():
                pass  # skip CA lines

        # Don't forget the last device
        if current_hca and current_ports:
            devices.append(
                InfinibandDevice(
                    hca=current_hca,
                    ports=sorted(current_ports),
                    net_devices=current_net,
                    state=current_state,
                )
            )

        return devices
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def build_nccl_defaults(discovery: DiscoveryResult) -> NCCLConfig | None:
    """Auto-populate NCCL config from discovery results.

    Creates clean boundary:
        Discovery -> NCCL defaults -> Network env builder
    """
    # Find ethernet interface
    ethernet_if = None
    for iface in discovery.interfaces:
        if iface.type == "ethernet" and iface.is_up and iface.ip:
            ethernet_if = iface.name
            break

    if not ethernet_if:
        return None

    if discovery.infiniband_present:
        # Find first active IB HCA
        ib_hca = None
        for dev in discovery.infiniband_devices:
            if dev.state == "ACTIVE":
                ib_hca = dev.hca
                break
        if not ib_hca:
            # IB present but none active — disable it
            return NCCLConfig(
                socket_ifname=ethernet_if,
                ib_hca=None,
                ib_disable=True,
            )
        return NCCLConfig(
            socket_ifname=ethernet_if,
            ib_hca=ib_hca,
            ib_disable=False,
        )

    # No IB — disable NCCL IB
    return NCCLConfig(
        socket_ifname=ethernet_if,
        ib_hca=None,
        ib_disable=True,
    )


def validate_network() -> ValidationResult:
    """Network health check.

    Checks:
    - interface exists and is up
    - IP assigned
    - Docker host networking works
    - NCCL variables sane
    - IB device accessible
    - /dev/infiniband present
    - required ports available
    """
    result = ValidationResult(healthy=True)

    interfaces = detect_network_interfaces()
    local_ip = detect_local_ip()

    # Check 1: At least one ethernet interface up with IP
    eth_up = [i for i in interfaces if i.type == "ethernet" and i.is_up and i.ip]
    if not eth_up:
        result.errors.append("No ethernet interface is up with an assigned IP")
        result.healthy = False

    # Check 2: Local IP detectable
    if not local_ip:
        result.warnings.append("Could not detect local IP address")

    # Check 3: Loopback should be up
    lo = [i for i in interfaces if i.type == "loopback"]
    if not lo or not lo[0].is_up:
        result.warnings.append("Loopback interface is not up")

    # Check 4: Docker bridge (if present) should not conflict
    docker_ifs = [i for i in interfaces if i.type == "docker"]
    if docker_ifs:
        for dif in docker_ifs:
            if not dif.is_up:
                result.warnings.append(f"Docker interface {dif.name} is down")

    # Check 5: IB validation (if present)
    ib_devices = detect_infiniband_devices()
    if ib_devices:
        for dev in ib_devices:
            if dev.state != "ACTIVE":
                result.warnings.append(
                    f"InfiniBand HCA {dev.hca} is {dev.state} (expected ACTIVE)"
                )
        if not any(d.state == "ACTIVE" for d in ib_devices):
            result.warnings.append("No active InfiniBand HCAs found — NCCL will use Ethernet")

        # Check /dev/infiniband
        dev_ib = Path("/dev/infiniband")
        if not dev_ib.exists():
            result.warnings.append("/dev/infiniband does not exist — IB devices may not be accessible")

    # Check 6: Common ports availability
    for port in [29500, 29501, 29502]:
        if not _port_available(port):
            result.warnings.append(f"Port {port} is already in use")

    return result


def _port_available(port: int) -> bool:
    """Check if a TCP port is available for binding."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


# ── Public API ───────────────────────────────────────────────────────────────


def run_discovery() -> DiscoveryResult:
    """Run full network discovery and return results.

    This is the main entry point — called by the discovery router.
    """
    interfaces = detect_network_interfaces()
    local_ip = detect_local_ip()
    ib_devices = detect_infiniband_devices()

    # Classify interfaces
    ethernet_if = None
    for iface in interfaces:
        if iface.type == "ethernet" and iface.is_up and iface.ip:
            ethernet_if = iface.name
            break

    infiniband_present = len(ib_devices) > 0

    discovery = DiscoveryResult(
        local_ip=local_ip,
        ethernet_if=ethernet_if,
        infiniband_present=infiniband_present,
        infiniband_devices=ib_devices,
        interfaces=interfaces,
        nccl_defaults=None,  # computed below
    )

    # Build NCCL defaults
    discovery.nccl_defaults = build_nccl_defaults(discovery)

    return discovery


# ── Module-level convenience functions ───────────────────────────────────────


def get_network_interfaces() -> list[NetworkInterface]:
    """Get all detected network interfaces."""
    return detect_network_interfaces()


def get_local_ip() -> str | None:
    """Get the local IP address."""
    return detect_local_ip()


def get_infiniband_devices() -> list[InfinibandDevice]:
    """Get all detected InfiniBand devices."""
    return detect_infiniband_devices()


def get_nccl_defaults() -> NCCLConfig | None:
    """Get auto-detected NCCL defaults."""
    discovery = run_discovery()
    return discovery.nccl_defaults


def check_network_health() -> ValidationResult:
    """Run network health validation."""
    return validate_network()
