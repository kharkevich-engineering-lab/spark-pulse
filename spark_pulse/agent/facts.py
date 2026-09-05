"""What a node reports about itself.

Facts are carried in the ``Hello`` and in every heartbeat. Two of them are
load-bearing beyond description:

* ``machine_id`` is **diagnostic only**. It is collected so the control plane
  can warn that two nodes report the same one — DGX Sparks ship duplicates —
  and it is never, anywhere, an identity (§3.1).
* ``hardware_fingerprint`` is compared on every heartbeat against what
  enrollment recorded. It changes when a machine is reimaged or when an
  identity is copied to different hardware, and either marks the node denied
  for a human decision rather than being silently trusted (§3.2).

Every probe here is best-effort and total: a missing file, a Linux-only path
on a developer's Mac, or a Docker daemon that is not running each yield an
empty field rather than an exception. An agent that cannot describe its kernel
must still be able to run a container.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import socket
from pathlib import Path
from typing import Any

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.version import __version__

logger = logging.getLogger(__name__)

__all__ = ["collect_facts", "facts_dict", "read_machine_id", "read_boot_id"]

_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def read_machine_id() -> str:
    """``/etc/machine-id``, or empty.

    Diagnostic only — see the module docstring. The node registry has its own
    reader for the same file with the same warning attached; this one exists
    so the agent does not depend on the registry, which lives on the control
    node.
    """
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(candidate).read_text().strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def read_boot_id() -> str:
    """The kernel's boot id, or empty. Changes on every reboot."""
    try:
        return Path(_BOOT_ID_PATH).read_text().strip()
    except OSError:
        return ""


def _kernel() -> str:
    try:
        info = platform.uname()
        return f"{info.system} {info.release}"
    except Exception:  # pragma: no cover — platform does not fail in practice
        return ""


def _os_release() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.partition("=")[2].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


def _memory_bytes() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


def _docker_version(docker_service: Any | None) -> str:
    if docker_service is None:
        return ""
    try:
        version = docker_service.client.version()
    except Exception:
        return ""
    if isinstance(version, dict):
        return str(version.get("Version") or "")
    return ""


def _interfaces() -> tuple[list[pb.NetworkInterface], list[str]]:
    """Interfaces and RoCE device names, via the existing discovery code.

    Reuses ``tools.discovery`` rather than parsing ``ip`` a second time: a
    second implementation of the same mapping is exactly what phase E deleted
    (``tools/network.py``), and it is not being reintroduced here.
    """
    try:
        from spark_pulse.tools import discovery
    except Exception:  # pragma: no cover — import is unconditional in practice
        return [], []
    interfaces: list[pb.NetworkInterface] = []
    try:
        for found in discovery.detect_network_interfaces():
            interfaces.append(
                pb.NetworkInterface(
                    name=found.name,
                    ip=found.ip or "",
                    mtu=int(found.mtu or 0),
                    is_up=bool(found.is_up),
                    type=found.type,
                )
            )
    except Exception as exc:
        logger.debug("interface probe failed: %s", exc)
    hcas: list[str] = []
    try:
        hcas = [p.name for p in Path("/sys/class/infiniband").iterdir()]
    except OSError:
        pass
    return interfaces, sorted(hcas)


#: Places a board serial shows up, most specific first. The device-tree entry
#: is where an ARM64 DGX Spark carries it; the DMI ones cover x86 development
#: boxes. All are read best-effort, and ``product_uuid`` is root-only, so a
#: node running the agent unprivileged simply falls through to the composite.
_SERIAL_PATHS = (
    "/proc/device-tree/serial-number",
    "/sys/class/dmi/id/product_uuid",
    "/sys/class/dmi/id/board_serial",
    "/sys/class/dmi/id/product_serial",
)


def _board_serial() -> str:
    for candidate in _SERIAL_PATHS:
        try:
            value = Path(candidate).read_bytes().decode(errors="ignore")
        except OSError:
            continue
        value = value.strip().strip("\x00")
        if value and value.lower() not in ("none", "unknown", "to be filled by o.e.m."):
            return value
    return ""


def _fingerprint(
    interfaces: list[pb.NetworkInterface],
    machine_id: str,
    cpu_count: int,
    memory_bytes: int,
) -> str:
    """A stable-across-reboots, unstable-across-reimage hardware fingerprint.

    A board serial when the hardware exposes one, and otherwise a composite of
    the things that describe the machine rather than its configuration: the
    interface *names* the kernel enumerates, the CPU count, the memory size
    and the machine-id.

    Deliberately *not* built from the hostname or from an IP address. Both
    change under DHCP and under a rename, and a fingerprint that moves when a
    node is renamed would deny a node for being renamed — the exact failure
    §3.1 says every name-keyed system in the survey documented.
    """
    serial = _board_serial()
    if serial:
        return hashlib.sha256(f"serial:{serial}".encode()).hexdigest()
    names = sorted(i.name for i in interfaces if i.name and i.type != "docker")
    material = "|".join([*names, str(cpu_count), str(memory_bytes), machine_id])
    if not material.strip("|"):
        return ""
    return hashlib.sha256(material.encode()).hexdigest()


def collect_facts(docker_service: Any | None = None) -> pb.NodeFacts:
    """Describe this machine. Never raises."""
    machine_id = read_machine_id()
    cpu_count = os.cpu_count() or 0
    interfaces, hcas = _interfaces()
    memory_bytes = _memory_bytes()
    try:
        from spark_pulse import tools

        gpu_count = len(tools.system.get_gpu_stats() or [])
    except Exception:
        # A Spark has one GPU and the count is advisory; a probe that needs
        # nvidia-smi must not be able to stop an agent from reporting in.
        gpu_count = 0
    return pb.NodeFacts(
        hostname=socket.gethostname(),
        boot_id=read_boot_id(),
        machine_id=machine_id,
        os_release=_os_release(),
        kernel=_kernel(),
        agent_version=__version__,
        docker_version=_docker_version(docker_service),
        cpu_count=cpu_count,
        memory_bytes=memory_bytes,
        gpu_count=gpu_count,
        interfaces=interfaces,
        infiniband_interfaces=hcas,
        hardware_fingerprint=_fingerprint(
            interfaces, machine_id, cpu_count, memory_bytes
        ),
    )


def facts_dict(facts: pb.NodeFacts) -> dict[str, str]:
    """The three identity-relevant facts, as the ledger wants them."""
    return {
        "machine_id": facts.machine_id,
        "boot_id": facts.boot_id,
        "hardware_fingerprint": facts.hardware_fingerprint,
    }
