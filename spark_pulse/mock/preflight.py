"""Simulated pre-flight — the real checks over a simulated host.

There is no second implementation of the checks here, for the same reason
``mock.node_service`` has no second implementation of the container service: a
parallel copy cannot catch a bug in the code it stands in for. Every verdict,
every observation and every remedy in simulation is produced by
:mod:`spark_pulse.tools.preflight` itself. Only the bytes coming back from the
node are invented.

What is invented is chosen to be *this hardware*, not a convenient fiction:

* ``nvidia-smi`` reports ``[N/A]`` for GPU memory, exactly as a DGX Spark does,
  so the unified-memory path — read ``MemAvailable`` instead, never fail the
  node — is the one simulation exercises rather than a path that only runs on
  real silicon.
* ``docker info`` lists no ``nvidia`` runtime, again exactly as a DGX Spark
  does — the toolkit is installed but registered only as an OCI hook, which is
  the path ``--gpus all`` takes. Simulating a registered runtime hid a check
  that blocked every deploy on real hardware.
* Peers hold no engine images (the simulated SSH transport seeds none), so the
  "a pull is needed, here is roughly how much" branch is what a two-node
  pre-flight actually shows.
* The control node's own ports include the ones this process listens on.

:data:`UNREACHABLE` is the seam for the other half of the reachability check: an
address in it answers the way an unplugged Spark does, with a transport failure
rather than a non-zero exit.
"""

from __future__ import annotations

from typing import Any, Callable

from spark_pulse.tools.preflight import (  # noqa: F401 — re-exported shapes
    ASSUMED_IMAGE_BYTES as ASSUMED_IMAGE_BYTES,
    CHECK_DISK as CHECK_DISK,
    CHECK_DOCKER as CHECK_DOCKER,
    CHECK_FABRIC as CHECK_FABRIC,
    CHECK_GPU as CHECK_GPU,
    CHECK_IMAGE as CHECK_IMAGE,
    CHECK_INTERFACES as CHECK_INTERFACES,
    CHECK_MODEL as CHECK_MODEL,
    CHECK_ORDER as CHECK_ORDER,
    CHECK_PORTS as CHECK_PORTS,
    CHECK_REACHABILITY as CHECK_REACHABILITY,
    CHECK_TOOLKIT as CHECK_TOOLKIT,
    DISK_HEADROOM as DISK_HEADROOM,
    PROBE_TIMEOUT as PROBE_TIMEOUT,
    STATUS_FAIL as STATUS_FAIL,
    STATUS_PASS as STATUS_PASS,
    STATUS_WARN as STATUS_WARN,
    STATUSES as STATUSES,
    VERDICT_BLOCKED as VERDICT_BLOCKED,
    VERDICT_READY as VERDICT_READY,
    VERDICT_SLOW as VERDICT_SLOW,
    VERDICTS as VERDICTS,
    Check as Check,
    HostProbe as HostProbe,
    NodeTarget as NodeTarget,
    ProbeResult as ProbeResult,
    checks_for_node as checks_for_node,
    disk_command as disk_command,
    parse_df as parse_df,
    parse_gpu_query as parse_gpu_query,
    parse_listening_ports as parse_listening_ports,
    parse_meminfo as parse_meminfo,
    parse_runtimes as parse_runtimes,
    parse_toolkit as parse_toolkit,
    selector_names as selector_names,
    targets_for as targets_for,
    verdict_for as verdict_for,
)
from spark_pulse.tools import preflight as _real

#: Addresses that answer the way a node that is off answers: no connection at
#: all, rather than a command that failed. Tests add to it; nothing else does.
UNREACHABLE: set[str] = set()

#: The simulated host. A DGX Spark: one GB10, no GPU-memory reporting, a
#: unified 121 GB pool, two fabric ports and a management link.
SIM_DOCKER_VERSION = "27.5.1"
SIM_DOCKER_ROOT = "/var/lib/docker"
#: What ``docker info`` reports on a stock DGX Spark: no ``nvidia`` entry.
SIM_RUNTIMES = "io.containerd.runc.v2 runc "
#: …and what the toolkit probe finds there instead: the hook binary, no CDI.
SIM_TOOLKIT = "hook\nctk"
SIM_GPU_ROW = "0, NVIDIA GB10, [N/A], [N/A]"
SIM_MEM_TOTAL_KB = 126_950_000
SIM_MEM_AVAILABLE_KB = 101_400_000
SIM_LISTENING_PORTS = (22, 5000, 8100)
SIM_NETDEVS = ("lo", "eth0", "docker0", "enp1s0f0np0", "enp1s0f1np1")
#: ``/sys/class/infiniband`` on a Spark lists every RoCE device, twins
#: included, whether or not its link is up — which is what makes it the right
#: place to check an ``NCCL_IB_HCA`` name against.
SIM_IBDEVS = (
    "ib0",
    "ib1",
    "rocep1s0f0",
    "rocep1s0f1",
    "roceP2p1s0f0",
    "roceP2p1s0f1",
)

#: ``ibdev2netdev`` on a Spark with one cable in the outermost QSFP port —
#: the exact four lines ``spark-vllm-docker`` ``docs/NETWORKING.md`` prints at
#: lines 22-27. Two ports up, so this simulates the non-mesh shape.
SIM_IBDEV2NETDEV = (
    "rocep1s0f0 port 1 ==> enp1s0f0np0 (Down)\n"
    "rocep1s0f1 port 1 ==> enp1s0f1np1 (Up)\n"
    "roceP2p1s0f0 port 1 ==> enP2p1s0f0np0 (Down)\n"
    "roceP2p1s0f1 port 1 ==> enP2p1s0f1np1 (Up)"
)

#: The addresses NETWORKING.md's netplan profile gives those twins (lines
#: 95-112). The two twins are deliberately on different subnets, which is the
#: rule the guide states in bold at line 133.
SIM_IP_ADDRESSES = (
    "1: lo    inet 127.0.0.1/8 scope host lo\n"
    "2: eth0    inet 192.168.1.100/24 brd 192.168.1.255 scope global eth0\n"
    "3: enp1s0f1np1    inet 192.168.177.11/24 scope global enp1s0f1np1\n"
    "4: enP2p1s0f1np1    inet 192.168.178.11/24 scope global enP2p1s0f1np1"
)
SIM_DISK_TOTAL_KB = 3_800_000_000
SIM_DISK_FREE_KB = 1_900_000_000


def _df_line(mount: str) -> str:
    return (
        f"/dev/nvme0n1p2 {SIM_DISK_TOTAL_KB} "
        f"{SIM_DISK_TOTAL_KB - SIM_DISK_FREE_KB} {SIM_DISK_FREE_KB} 51% {mount}"
    )


class SimulatedHostProbe:
    """A host probe that answers the pre-flight's commands out of memory.

    Every command it is asked to run is recorded in :attr:`commands`, so a test
    can assert that a check aimed at a peer actually left the machine.
    """

    def __init__(self, address: str, unreachable: bool = False):
        self.address = address
        self.unreachable = unreachable
        self.commands: list[str] = []

    def run(self, command: str, timeout: int = PROBE_TIMEOUT) -> ProbeResult:
        self.commands.append(command)
        if self.unreachable:
            return ProbeResult(
                reachable=False,
                error=f"ssh: connect to host {self.address} port 22: "
                "No route to host",
            )
        return self._answer(command)

    def _answer(self, command: str) -> ProbeResult:
        if command.startswith("echo spark-pulse-preflight"):
            return _ok("spark-pulse-preflight")
        if command.startswith("docker info"):
            return _ok(f"{SIM_DOCKER_VERSION}|{SIM_DOCKER_ROOT}|{SIM_RUNTIMES}")
        if command.startswith("command -v nvidia-container-runtime-hook"):
            return _ok(SIM_TOOLKIT)
        if command.startswith("nvidia-smi"):
            return _ok(SIM_GPU_ROW)
        if command.startswith("cat /proc/meminfo"):
            return _ok(
                f"MemTotal:       {SIM_MEM_TOTAL_KB} kB\n"
                f"MemFree:        {SIM_MEM_AVAILABLE_KB // 2} kB\n"
                f"MemAvailable:   {SIM_MEM_AVAILABLE_KB} kB\n"
            )
        if command.startswith("ss -H"):
            body = "\n".join(
                f"LISTEN 0 4096 0.0.0.0:{port} 0.0.0.0:*"
                for port in SIM_LISTENING_PORTS
            )
            return _ok(body)
        if command.startswith("ls -1 /sys/class/net"):
            return _ok("\n".join(SIM_NETDEVS) + "\n== ib\n" + "\n".join(SIM_IBDEVS))
        if command.startswith("ibdev2netdev"):
            return _ok(SIM_IBDEV2NETDEV + "\n== addr\n" + SIM_IP_ADDRESSES)
        if command.startswith("for p in"):
            lines = []
            for path in _paths_in(command):
                lines.append(f"== {path}")
                lines.append(_df_line("/"))
            return _ok("\n".join(lines))
        return ProbeResult(
            reachable=True, returncode=127, stderr=f"sh: not simulated: {command}"
        )


def _ok(stdout: str) -> ProbeResult:
    return ProbeResult(reachable=True, returncode=0, stdout=stdout)


def _paths_in(command: str) -> list[str]:
    """The quoted paths out of the disk command's ``for p in …`` list."""
    import shlex

    body = command.split("for p in", 1)[1].split(";", 1)[0]
    return shlex.split(body)


def probe_for(target: NodeTarget, ssh_client: Any = None) -> HostProbe:
    """The simulated probe bound to ``target``."""
    _ = ssh_client
    return SimulatedHostProbe(
        target.address or "localhost",
        unreachable=target.address in UNREACHABLE,
    )


def reset() -> None:
    """Forget any simulated unreachability."""
    UNREACHABLE.clear()


def run(
    recipe_id: str = "",
    *,
    probe_factory: Callable[..., HostProbe] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """The real pre-flight, over the simulated host probe."""
    return _real.run(
        recipe_id,
        probe_factory=probe_factory or probe_for,
        **kwargs,
    )


__all__ = [
    "ASSUMED_IMAGE_BYTES",
    "CHECK_DISK",
    "CHECK_DOCKER",
    "CHECK_FABRIC",
    "CHECK_GPU",
    "CHECK_IMAGE",
    "CHECK_INTERFACES",
    "CHECK_MODEL",
    "CHECK_ORDER",
    "CHECK_PORTS",
    "CHECK_REACHABILITY",
    "CHECK_TOOLKIT",
    "DISK_HEADROOM",
    "PROBE_TIMEOUT",
    "SIM_IBDEV2NETDEV",
    "SIM_IBDEVS",
    "SIM_IP_ADDRESSES",
    "SIM_LISTENING_PORTS",
    "SIM_NETDEVS",
    "SIM_TOOLKIT",
    "STATUSES",
    "STATUS_FAIL",
    "STATUS_PASS",
    "STATUS_WARN",
    "UNREACHABLE",
    "VERDICTS",
    "VERDICT_BLOCKED",
    "VERDICT_READY",
    "VERDICT_SLOW",
    "Check",
    "HostProbe",
    "NodeTarget",
    "ProbeResult",
    "SimulatedHostProbe",
    "checks_for_node",
    "disk_command",
    "parse_df",
    "parse_gpu_query",
    "parse_listening_ports",
    "parse_meminfo",
    "parse_runtimes",
    "parse_toolkit",
    "selector_names",
    "probe_for",
    "reset",
    "run",
    "targets_for",
    "verdict_for",
]
