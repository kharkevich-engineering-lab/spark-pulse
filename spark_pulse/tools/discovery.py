"""Host network discovery tool.

Detects network interfaces, InfiniBand devices, local IP, and auto-generates
NCCL defaults. Used by Phase 2A to replace manual network configuration in
launch-cluster.sh.

Approach: uses psutil first, falls back to subprocess + /sys scanning.

**Peer discovery over mDNS.** ``docs/cluster-agent-plan.md`` section 3.1 and
section 6: we publish our own ``_spark-pulse._tcp`` record carrying the node
id, port and version rather than depending on ``_ssh._tcp`` and then guessing
whether a responder is a Spark or an office printer — but we browse for both,
because ``_ssh._tcp`` is what NVIDIA's own ``discover-sparks`` uses and DGX OS
ships ``/etc/avahi/services/ssh.service``. python-zeroconf was tested on the
Spark this session and coexists with a running avahi in both directions.

Two rules the same test produced, and both are load-bearing:

* **Announcing and browsing are restricted to real interfaces.** Browsing
  everything announced the service on the docker bridge and on a veth pair, so
  every container on the host would become mDNS noise. :func:`real_interfaces`
  is the filter, and it takes ``ibdev2netdev``'s view of which fabric links are
  up when that tool is present.
* **Discovery degrades to an empty list, never to an exception.** No zeroconf
  package, no multicast, a firewall on 5353 — all of them mean "no peers found
  yet", and adding a node by typing its address always works.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

#: Our own service type. Carries the node id, port and version in its TXT
#: record, so a responder identifies itself instead of being guessed at.
SPARK_PULSE_SERVICE = "_spark-pulse._tcp.local."

#: What NVIDIA's ``discover-sparks`` browses, and what DGX OS advertises.
SSH_SERVICE = "_ssh._tcp.local."

#: Interface name prefixes that are never a real network link: container
#: bridges, veth pairs, virtual switches and tunnels. Announcing on these is
#: the mistake the Spark test surfaced.
_VIRTUAL_PREFIXES = (
    "docker",
    "br-",
    "veth",
    "virbr",
    "vnet",
    "tap",
    "tun",
    "cni",
    "flannel",
    "kube",
    "cali",
    "lxc",
    "podman",
)

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


@dataclass(frozen=True)
class DiscoveredPeer:
    """One responder found on the LAN over mDNS.

    ``node_id`` and ``version`` are only ever populated for our own
    ``_spark-pulse._tcp`` record, which is exactly why we publish one: an
    ``_ssh._tcp`` answer tells us a host exists and nothing more.
    """

    address: str
    port: int
    service: str
    hostname: str = ""
    instance: str = ""
    node_id: str = ""
    version: str = ""

    @property
    def is_spark_pulse(self) -> bool:
        """Whether the responder identified itself as a Spark Pulse node."""
        return self.service == SPARK_PULSE_SERVICE


# ── Detection Functions ──────────────────────────────────────────────────────


def _try_import_psutil():
    """Try importing psutil, return None if unavailable."""
    try:
        import psutil  # noqa: F811

        return psutil
    except ImportError:
        return None


def _classify_interface(
    name: str,
) -> Literal["ethernet", "infiniband", "loopback", "docker", "other"]:
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
            result.warnings.append(
                "No active InfiniBand HCAs found — NCCL will use Ethernet"
            )

        # Check /dev/infiniband
        dev_ib = Path("/dev/infiniband")
        if not dev_ib.exists():
            result.warnings.append(
                "/dev/infiniband does not exist — IB devices may not be accessible"
            )

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

    # ``DiscoveryResult`` is frozen, so the NCCL defaults are computed against
    # a draft and the final result is built once. Assigning to the field after
    # construction raised ``FrozenInstanceError``, which made every real-mode
    # call to this function — the whole ``POST /api/discovery`` route — fail.
    draft = DiscoveryResult(
        local_ip=local_ip,
        ethernet_if=ethernet_if,
        infiniband_present=infiniband_present,
        infiniband_devices=ib_devices,
        interfaces=interfaces,
        nccl_defaults=None,
    )
    return replace(draft, nccl_defaults=build_nccl_defaults(draft))


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


# ── Real interfaces ──────────────────────────────────────────────────────────
#
# Everything below this line is scoped by :func:`real_interfaces`. Section 6:
# browsing with all interfaces announced the service on the docker bridge and
# on a veth pair as well as the real network, "or every container on the host
# becomes mDNS noise".


def ibdev2netdev_up() -> list[str]:
    """Fabric net devices ``ibdev2netdev`` reports as up.

    The tool ships on DGX OS — confirmed present on the Spark — and prints one
    line per port, ``mlx5_0 port 1 ==> ib0 (Up)``. Returns ``[]`` when it is
    absent, which is every machine that is not a DGX; the caller then falls
    back to ``/sys``-derived interface state.
    """
    try:
        result = subprocess.run(
            ["ibdev2netdev"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []

    names: list[str] = []
    for line in result.stdout.splitlines():
        if "==>" not in line or "(up)" not in line.lower():
            continue
        tail = line.split("==>", 1)[1].strip()
        name = tail.split("(", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return names


def is_real_interface(interface: NetworkInterface) -> bool:
    """Whether an interface is a real network link we may announce on.

    Loopback, container bridges, veth pairs and tunnels are excluded, and so is
    anything that is down: announcing on a link with no peers is noise, and
    browsing on one wastes the browse window.
    """
    if not interface.is_up:
        return False
    if interface.type in ("loopback", "docker"):
        return False
    return not interface.name.lower().startswith(_VIRTUAL_PREFIXES)


def real_interfaces() -> list[NetworkInterface]:
    """The interfaces mDNS may use: the management link plus the live fabric.

    An InfiniBand link is included only when ``ibdev2netdev`` calls it up, if
    that tool answered at all; otherwise the interface's own operstate decides.
    """
    fabric_up = ibdev2netdev_up()
    chosen = []
    for interface in detect_network_interfaces():
        if not is_real_interface(interface):
            continue
        if interface.type == "infiniband" and fabric_up:
            if interface.name not in fabric_up:
                continue
        chosen.append(interface)
    return chosen


def real_interface_names() -> list[str]:
    """Just the names, in discovery order."""
    return [interface.name for interface in real_interfaces()]


def announce_addresses() -> list[str]:
    """IPv4 addresses of the real interfaces, for zeroconf to bind to.

    python-zeroconf takes addresses rather than interface names, so this is the
    translation. An interface with no IPv4 address cannot carry a v4 mDNS
    announcement and is dropped.
    """
    return [
        interface.ip
        for interface in real_interfaces()
        if interface.ip and interface.ip != "127.0.0.1"
    ]


def detect_link_local_addresses() -> dict[str, str]:
    """Interface name to its IPv6 link-local (``fe80::``) address.

    An interface missing from this map has no link-local address, which
    silently disables every ``ff02::1`` peer sweep on that link — the trap
    section 3.1 names, produced by ``link-local: []`` in a netplan profile.
    """
    psutil = _try_import_psutil()
    if psutil is None:
        return _link_local_from_proc()

    found: dict[str, str] = {}
    try:
        addrs_by_name = psutil.net_if_addrs()
    except OSError:  # pragma: no cover — psutil failing is not our problem
        return found
    for name, addrs in addrs_by_name.items():
        for addr in addrs:
            if addr.family != socket.AF_INET6:
                continue
            address = str(addr.address).split("%", 1)[0].lower()
            if address.startswith("fe80"):
                found.setdefault(name, address)
    return found


def _link_local_from_proc() -> dict[str, str]:
    """Parse ``/proc/net/if_inet6`` when psutil is unavailable."""
    found: dict[str, str] = {}
    try:
        lines = Path("/proc/net/if_inet6").read_text().splitlines()
    except OSError:
        return found
    for line in lines:
        parts = line.split()
        if len(parts) < 6:
            continue
        raw, name = parts[0], parts[5]
        if not raw.lower().startswith("fe80"):
            continue
        groups = [raw[i : i + 4] for i in range(0, 32, 4)]
        found.setdefault(name, ":".join(groups).lower())
    return found


# ── mDNS ─────────────────────────────────────────────────────────────────────

#: Addresses that have answered mDNS, and the hostnames they answered under.
#: Section 8 asks for hostname churn to be reported rather than left as a
#: mystery, and churn is only visible across browses, so the observations are
#: kept for the life of the process.
_mdns_hostnames: dict[str, set[str]] = {}
_mdns_lock = threading.Lock()

#: The registration handle for our own record, so shutdown can withdraw it.
_announcement: tuple[object, object] | None = None


def _try_import_zeroconf():
    """Return the ``zeroconf`` module, or ``None`` when it is not installed."""
    try:
        import zeroconf  # noqa: F811

        return zeroconf
    except ImportError:  # pragma: no cover — zeroconf is a declared dependency
        return None


def mdns_available() -> bool:
    """Whether peer discovery can run at all.

    False means :func:`browse_peers` returns an empty list and manual entry is
    the only way to add a node — which is a fact worth telling an operator, not
    an error.
    """
    return _try_import_zeroconf() is not None


def mdns_hostname_history() -> dict[str, set[str]]:
    """Every address seen over mDNS, mapped to the hostnames it used."""
    with _mdns_lock:
        return {address: set(names) for address, names in _mdns_hostnames.items()}


def reset_mdns_history() -> None:
    """Forget the observed hostnames (tests, and an operator's fresh start)."""
    with _mdns_lock:
        _mdns_hostnames.clear()


def record_mdns_hostname(address: str, hostname: str) -> None:
    """Note that ``address`` answered mDNS under ``hostname``."""
    if not address or not hostname:
        return
    with _mdns_lock:
        _mdns_hostnames.setdefault(address, set()).add(hostname.rstrip("."))


def _decode_txt(properties: dict) -> dict[str, str]:
    """Zeroconf hands TXT keys and values back as bytes. Make them strings."""
    decoded: dict[str, str] = {}
    for key, value in (properties or {}).items():
        if isinstance(key, bytes):
            key = key.decode("utf-8", "replace")
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        decoded[str(key)] = "" if value is None else str(value)
    return decoded


def announce_self(node_id: str, port: int, version: str) -> bool:
    """Publish our ``_spark-pulse._tcp`` record on the real interfaces.

    The TXT record carries the node id, the port and the version, so a peer
    that finds us knows what we are instead of inferring it from an open SSH
    port. Idempotent: a second call withdraws the previous registration first.

    Returns whether the record was published. Never raises — a control plane
    that cannot announce itself still runs, and its peers can still be added by
    address.
    """
    global _announcement

    zc_module = _try_import_zeroconf()
    if zc_module is None:
        logger.info("zeroconf is not installed; not announcing over mDNS")
        return False

    addresses = announce_addresses()
    if not addresses:
        logger.info("No real interface carries an IPv4 address; not announcing")
        return False

    stop_announcement()
    hostname = socket.gethostname().split(".", 1)[0]
    try:
        zeroconf = zc_module.Zeroconf(
            interfaces=addresses, ip_version=zc_module.IPVersion.V4Only
        )
        info = zc_module.ServiceInfo(
            SPARK_PULSE_SERVICE,
            f"{hostname}-{node_id[:8]}.{SPARK_PULSE_SERVICE}",
            addresses=[socket.inet_aton(address) for address in addresses],
            port=int(port),
            properties={"node_id": node_id, "version": version, "port": str(port)},
            server=f"{hostname}.local.",
        )
        zeroconf.register_service(info)
    except Exception as exc:
        logger.warning("Could not announce over mDNS: %s", exc)
        return False

    _announcement = (zeroconf, info)
    logger.info("Announced %s on %s", SPARK_PULSE_SERVICE, ", ".join(addresses))
    return True


def stop_announcement() -> None:
    """Withdraw our record. Safe to call when nothing was published."""
    global _announcement

    if _announcement is None:
        return
    zeroconf, info = _announcement
    _announcement = None
    try:
        zeroconf.unregister_service(info)
        zeroconf.close()
    except Exception as exc:  # pragma: no cover — shutdown is best effort
        logger.debug("Could not withdraw the mDNS record: %s", exc)


def browse_peers(timeout: float = 3.0) -> list[DiscoveredPeer]:
    """Browse the LAN for Spark Pulse nodes and for SSH responders.

    Browsing is restricted to the real interfaces for the reason in this
    module's docstring. Both service types are collected: our own record
    identifies a node, and ``_ssh._tcp`` is what a Spark advertises before it
    has ever run Spark Pulse.

    Returns ``[]`` — never raises — when zeroconf is missing, when no real
    interface carries an address, or when mDNS itself fails. Discovery not
    working means "no peers found", and manual entry always works.
    """
    zc_module = _try_import_zeroconf()
    if zc_module is None:
        return []
    addresses = announce_addresses()
    if not addresses:
        return []

    peers: dict[tuple[str, str], DiscoveredPeer] = {}
    try:
        zeroconf = zc_module.Zeroconf(
            interfaces=addresses, ip_version=zc_module.IPVersion.V4Only
        )
    except Exception as exc:
        logger.warning("Could not open mDNS for browsing: %s", exc)
        return []

    def collect(service_type: str, name: str) -> None:
        try:
            info = zeroconf.get_service_info(service_type, name, timeout=1500)
        except Exception as exc:  # pragma: no cover — one bad responder
            logger.debug("Could not resolve %s: %s", name, exc)
            return
        if info is None:
            return
        properties = _decode_txt(getattr(info, "properties", {}) or {})
        hostname = (getattr(info, "server", "") or "").rstrip(".")
        for address in info.parsed_addresses():
            record_mdns_hostname(address, hostname)
            peers[(address, service_type)] = DiscoveredPeer(
                address=address,
                port=int(getattr(info, "port", 0) or 0),
                service=service_type,
                hostname=hostname,
                instance=name.split(".", 1)[0],
                node_id=properties.get("node_id", ""),
                version=properties.get("version", ""),
            )

    class _Listener:
        def add_service(self, _zc, service_type, name):
            collect(service_type, name)

        def update_service(self, _zc, service_type, name):
            collect(service_type, name)

        def remove_service(self, _zc, service_type, name):
            return None

    try:
        browser = zc_module.ServiceBrowser(
            zeroconf, [SPARK_PULSE_SERVICE, SSH_SERVICE], _Listener()
        )
        time.sleep(max(0.1, float(timeout)))
        browser.cancel()
    except Exception as exc:
        logger.warning("mDNS browse failed: %s", exc)
    finally:
        try:
            zeroconf.close()
        except Exception:  # pragma: no cover — closing is best effort
            pass

    return sorted(peers.values(), key=lambda p: (p.address, p.service))
