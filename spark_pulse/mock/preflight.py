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
SIM_RUNTIMES = '{"io.containerd.runc.v2":{"path":"runc"},"nvidia":{"path":"nvidia-container-runtime"},"runc":{"path":"runc"}}'  # noqa: E501
SIM_GPU_ROW = "0, NVIDIA GB10, [N/A], [N/A]"
SIM_MEM_TOTAL_KB = 126_950_000
SIM_MEM_AVAILABLE_KB = 101_400_000
SIM_LISTENING_PORTS = (22, 5000, 8100)
SIM_NETDEVS = ("lo", "eth0", "docker0", "enp1s0f0np0", "enp1s0f1np1")
SIM_IBDEVS = ("ib0", "ib1", "rocep1s0f0", "rocep1s0f1")
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
    "SIM_IBDEVS",
    "SIM_LISTENING_PORTS",
    "SIM_NETDEVS",
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
    "probe_for",
    "reset",
    "run",
    "targets_for",
    "verdict_for",
]
