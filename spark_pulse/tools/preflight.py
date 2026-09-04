"""Pre-flight — what is missing, on which node, and what to do about it.

Section 8 of ``docs/cluster-agent-plan.md`` asks for *diagnostics rather than
mysteries*, and the deploy path is where the mysteries are most expensive: an
image that is not there costs twenty minutes, a model that is half-copied costs
an hour and then fails, and a pinned interface that does not exist kills the
collective outright with a message nobody outside NCCL can read. All of those
are knowable in seconds, before anything starts. This module knows them.

**Every check reports four things**: the node it ran on, what was observed,
what to do about it, and a status of ``pass``, ``warn`` or ``fail``. A check
that cannot name a remedy is not worth running, so ``remedy`` is filled on
every non-passing result.

**Warn is not a weak fail.** A node that needs a 26 GB pull will deploy
perfectly; it will just take a while. A node we cannot reach will not deploy at
all. Collapsing those into one "worst status" is what makes a pre-flight
useless — an operator learns to ignore it, because it is always red. So
:func:`verdict_for` produces three verdicts:

* ``blocked`` — at least one check failed. The deploy cannot proceed.
* ``slow`` — nothing failed, but something must move bytes first. It will work;
  budget the time.
* ``ready`` — nothing failed and nothing must transfer. Advisories may still be
  attached (unreported GPU memory is one, on this hardware always).

**Two transports, deliberately.** Container facts — is the image here, at which
digest — go through the node service, which phase C bound to a node at
construction so no call can silently ask the wrong machine. Host facts — a GPU,
a port, an interface, free disk — need a shell, so they go through a
:class:`HostProbe`: locally a subprocess, on a peer an SSH command. The probe
keeps phase A's structural distinction between *unreachable* and *the command
failed*: ``ssh`` reserves exit 255 for its own failures, so
:class:`~spark_pulse.tools.ssh.SSHClient` raises for a transport failure and
returns a result for a remote non-zero exit. A pre-flight that reported "docker
is missing" when the truth was "the node is off" would be worse than none.

**One hardware fact is baked in.** ``nvidia-smi`` reports no GPU memory at all
on a DGX Spark — total, used and free all come back ``[N/A]``, because the GPU
shares the 121 GB unified pool. A capacity check that believes the zeros
reports a node with no memory, which is both wrong and alarming. So the GPU
check reads ``MemAvailable`` from ``/proc/meminfo`` instead, and a node is
*never* failed for memory ``nvidia-smi`` declined to report; the figure is
called unavailable, in words.

Nothing here starts a deployment or is wired into the deploy path: it is a
report. The coordinator wires it in once the parallel start-path work lands.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Protocol

from spark_pulse import tools
from spark_pulse.tools.ssh import SSHClient, SSHError

logger = logging.getLogger(__name__)

# ── Vocabulary ───────────────────────────────────────────────────────────────

#: A check's outcome. ``warn`` is a first-class answer, not a soft failure.
STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUSES = (STATUS_PASS, STATUS_WARN, STATUS_FAIL)

#: The overall answer. See the module docstring for why there are three.
VERDICT_READY = "ready"
VERDICT_SLOW = "slow"
VERDICT_BLOCKED = "blocked"
VERDICTS = (VERDICT_READY, VERDICT_SLOW, VERDICT_BLOCKED)

#: Check identifiers, in the order they are reported.
CHECK_REACHABILITY = "reachability"
CHECK_DOCKER = "docker"
CHECK_TOOLKIT = "nvidia_toolkit"
CHECK_GPU = "gpu"
CHECK_IMAGE = "image"
CHECK_MODEL = "model"
CHECK_PORTS = "ports"
CHECK_INTERFACES = "interfaces"
CHECK_DISK = "disk"

CHECK_ORDER = (
    CHECK_REACHABILITY,
    CHECK_DOCKER,
    CHECK_TOOLKIT,
    CHECK_GPU,
    CHECK_IMAGE,
    CHECK_MODEL,
    CHECK_PORTS,
    CHECK_INTERFACES,
    CHECK_DISK,
)

#: Headroom demanded beyond the bytes that must actually land, as a multiplier.
#: A pull unpacks as well as downloads, so "exactly enough" is not enough.
DISK_HEADROOM = 1.25

#: What a probe command is given before it is considered hung.
PROBE_TIMEOUT = 20

#: Fallback size for an image whose size nothing can report, used only to say
#: roughly how long a pull will take. A Spark engine image is about this.
ASSUMED_IMAGE_BYTES = 26_843_545_600

_GIB = 1024**3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bytes(value: int | float | None) -> str:
    """Human-readable byte count, in the units an operator budgets time in."""
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover — unreachable, GB terminates


# ── The report shapes ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NodeTarget:
    """One machine the pre-flight runs against.

    Built from the node registry when the address is known there, so a check
    can name the node the way the operator does rather than by IP, and so the
    interfaces check knows which names the plan will pin on it.
    """

    id: str
    label: str
    address: str
    is_control_plane: bool = False
    ssh_user: str = ""
    ethernet_interface: str = ""
    infiniband_interfaces: tuple[str, ...] = ()
    ranks: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["infiniband_interfaces"] = list(self.infiniband_interfaces)
        data["ranks"] = list(self.ranks)
        return data


@dataclass(frozen=True, slots=True)
class Check:
    """One check on one node: what it is, what we saw, what to do.

    ``costs_time`` is what separates a warning that costs time from one that
    costs nothing, and it is the only input to the ``slow`` verdict.
    ``delay_bytes`` says how much, when that is knowable — a model this control
    plane has never seen has a real transfer ahead of it and no size to put on
    it, and saying "0 bytes" there would be a lie the verdict then believes.
    """

    id: str
    title: str
    node: str
    node_id: str
    status: str
    observed: str
    remedy: str = ""
    delay_bytes: int = 0
    costs_time: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == STATUS_FAIL

    @property
    def delays(self) -> bool:
        return self.status == STATUS_WARN and (self.costs_time or self.delay_bytes > 0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _check(
    check_id: str,
    title: str,
    target: NodeTarget,
    status: str,
    observed: str,
    remedy: str = "",
    delay_bytes: int = 0,
    costs_time: bool = False,
    **detail: Any,
) -> Check:
    """Build a check, naming the node it ran on in every case."""
    return Check(
        id=check_id,
        title=title,
        node=target.label,
        node_id=target.id,
        status=status,
        observed=observed,
        remedy=remedy,
        delay_bytes=int(delay_bytes),
        costs_time=bool(costs_time or delay_bytes > 0),
        detail=detail,
    )


# ── The host probe ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One shell command's outcome on one node.

    ``reachable`` is the distinction phase A made structural: ``False`` means
    we never got to run anything, and the return code is meaningless. Anything
    else is the remote command's own verdict.
    """

    reachable: bool
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.reachable and self.returncode == 0

    @property
    def message(self) -> str:
        """The most useful line of output, for an observation string."""
        text = (self.stderr or self.stdout or self.error or "").strip()
        return text.splitlines()[0][:200] if text else ""


class HostProbe(Protocol):
    """Runs a shell command on exactly one node."""

    def run(self, command: str, timeout: int = PROBE_TIMEOUT) -> ProbeResult:
        """Run ``command`` and report what happened."""
        ...


class LocalHostProbe:
    """Shell commands on the machine this process runs on.

    Local is reachable by construction, so the only way to come back
    unreachable is no usable shell at all.
    """

    def run(self, command: str, timeout: int = PROBE_TIMEOUT) -> ProbeResult:
        try:
            done = subprocess.run(
                ["/bin/sh", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:  # pragma: no cover — no /bin/sh
            return ProbeResult(reachable=False, error=str(exc))
        except subprocess.TimeoutExpired:
            return ProbeResult(
                reachable=True,
                returncode=124,
                error=f"timed out after {timeout}s",
            )
        return ProbeResult(
            reachable=True,
            returncode=done.returncode,
            stdout=done.stdout or "",
            stderr=done.stderr or "",
        )


class SSHHostProbe:
    """Shell commands on a peer, over SSH.

    :class:`~spark_pulse.tools.ssh.SSHError` means the node could not be
    reached or authenticated; an :class:`~spark_pulse.tools.ssh.SSHResult`
    means the command ran and this is what it said. The two are never mixed.
    """

    def __init__(self, address: str, ssh_client: SSHClient):
        self.address = address
        self.ssh = ssh_client

    def run(self, command: str, timeout: int = PROBE_TIMEOUT) -> ProbeResult:
        try:
            result = self.ssh.exec(self.address, command, timeout=timeout)
        except SSHError as exc:
            return ProbeResult(reachable=False, error=exc.message or str(exc))
        except OSError as exc:
            return ProbeResult(reachable=False, error=str(exc))
        return ProbeResult(
            reachable=True,
            returncode=int(getattr(result, "returncode", 1)),
            stdout=getattr(result, "stdout", "") or "",
            stderr=getattr(result, "stderr", "") or "",
        )


def probe_for(target: NodeTarget, ssh_client: SSHClient | None = None) -> HostProbe:
    """The host probe bound to ``target``.

    Overridden wholesale by ``spark_pulse.mock.preflight``, so simulation swaps
    the transport at one place rather than branching inside every check.
    """
    if target.is_control_plane:
        return LocalHostProbe()
    from spark_pulse.tools.ssh import OpenSSHClient

    return SSHHostProbe(
        target.address,
        ssh_client or OpenSSHClient(user=target.ssh_user or None),
    )


# ── Parsing the probe output ─────────────────────────────────────────────────

_LISTENING_PORT_RE = re.compile(r"[:.](\d{1,5})\s")


def parse_listening_ports(text: str) -> set[int]:
    """Every TCP port in ``ss -ltn`` or ``netstat -an`` output.

    Both spell the local address differently (``0.0.0.0:8000`` and
    ``*.8000``), and both pad it with whitespace, which is what the pattern
    keys on. A port we cannot parse is simply not reported as busy — this
    check exists to name a conflict, not to invent one.
    """
    ports: set[int] = set()
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith(("state", "proto", "active")):
            continue
        fields = stripped.split()
        # The local address is the fourth column in both tools' output, but a
        # short ``ss`` line drops columns, so scan the whole line and take the
        # first port-looking token after a colon or dot.
        for token in fields:
            match = _LISTENING_PORT_RE.search(token + " ")
            if match:
                port = int(match.group(1))
                if 0 < port < 65536:
                    ports.add(port)
                break
    return ports


def parse_meminfo(text: str) -> dict[str, int]:
    """``/proc/meminfo`` as bytes, keyed by its own field names."""
    values: dict[str, int] = {}
    for line in (text or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not key or not parts or not parts[0].isdigit():
            continue
        scale = 1024 if len(parts) > 1 and parts[1].lower() == "kb" else 1
        values[key.strip()] = int(parts[0]) * scale
    return values


def parse_gpu_query(text: str) -> list[dict[str, Any]]:
    """``nvidia-smi --query-gpu`` CSV rows, memory left ``None`` when absent.

    ``[N/A]`` is the honest answer on this hardware and it is preserved as
    ``None`` rather than coerced to zero, because zero would read as "no
    memory" and that is a different, alarming claim.
    """
    gpus: list[dict[str, Any]] = []
    for line in (text or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[0].lstrip("-").isdigit():
            continue

        def _num(raw: str) -> int | None:
            value = raw.strip("[] ")
            if not value or value in {"N/A", "NA", "Not Supported", "Unknown Error"}:
                return None
            try:
                return int(float(value))
            except ValueError:
                return None

        gpus.append(
            {
                "index": int(parts[0]),
                "name": parts[1],
                "memory_total_mib": _num(parts[2]) if len(parts) > 2 else None,
                "memory_free_mib": _num(parts[3]) if len(parts) > 3 else None,
            }
        )
    return gpus


def parse_df(text: str) -> dict[str, dict[str, Any]]:
    """The ``== <path>`` / ``df -Pk`` pairs :data:`_DISK_COMMAND` prints."""
    found: dict[str, dict[str, Any]] = {}
    path = ""
    for line in (text or "").splitlines():
        if line.startswith("== "):
            path = line[3:].strip()
            continue
        if not path:
            continue
        fields = line.split()
        if len(fields) < 6 or not fields[1].isdigit():
            continue
        found[path] = {
            "filesystem": fields[0],
            "total_bytes": int(fields[1]) * 1024,
            "available_bytes": int(fields[3]) * 1024,
            "mount": fields[5],
        }
        path = ""
    return found


def parse_runtimes(text: str) -> list[str]:
    """Runtime names out of ``docker info``'s ``{{json .Runtimes}}``."""
    return sorted(set(re.findall(r'"([A-Za-z0-9_.-]+)"\s*:\s*\{', text or "")))


# ── Probe commands ───────────────────────────────────────────────────────────

#: One round trip answers reachability *and* everything about docker: the
#: daemon version, its registered runtimes and where it keeps its data.
DOCKER_COMMAND = (
    "docker info --format "
    "'{{.ServerVersion}}|{{.DockerRootDir}}|{{json .Runtimes}}' 2>&1"
)

GPU_COMMAND = (
    "nvidia-smi --query-gpu=index,name,memory.total,memory.free "
    "--format=csv,noheader,nounits 2>&1"
)

MEMINFO_COMMAND = "cat /proc/meminfo 2>/dev/null"

PORTS_COMMAND = "ss -H -ltn 2>/dev/null || netstat -an 2>/dev/null | grep -i listen"

INTERFACES_COMMAND = (
    "ls -1 /sys/class/net 2>/dev/null; echo '== ib'; "
    "ls -1 /sys/class/infiniband 2>/dev/null"
)

REACH_COMMAND = "echo spark-pulse-preflight"

#: ``df`` refuses a path that does not exist yet, and a docker root or hub
#: cache that has never been created is a perfectly ordinary state — so walk up
#: to the nearest directory that does exist and measure that filesystem.
_DISK_COMMAND = (
    'for p in %s; do d="$p"; '
    'while [ ! -d "$d" ] && [ "$d" != "/" ]; do d=`dirname "$d"`; done; '
    'echo "== $p"; df -Pk "$d" 2>/dev/null | tail -n 1; done'
)


def disk_command(paths: Iterable[str]) -> str:
    """The command that measures free space for ``paths``."""
    return _DISK_COMMAND % " ".join(shlex.quote(str(p)) for p in paths)


# ── Context ──────────────────────────────────────────────────────────────────


@dataclass
class _NodeFacts:
    """Everything one node's probe told us, gathered once per run."""

    reachable: bool = True
    unreachable_reason: str = ""
    docker: ProbeResult | None = None
    docker_version: str = ""
    docker_root: str = "/var/lib/docker"
    runtimes: list[str] = field(default_factory=list)
    gpu: ProbeResult | None = None
    gpus: list[dict[str, Any]] = field(default_factory=list)
    meminfo: dict[str, int] = field(default_factory=dict)
    ports: ProbeResult | None = None
    listening: set[int] = field(default_factory=set)
    interfaces: ProbeResult | None = None
    netdevs: set[str] = field(default_factory=set)
    ibdevs: set[str] = field(default_factory=set)
    disk: dict[str, dict[str, Any]] = field(default_factory=dict)
    disk_probe: ProbeResult | None = None


def _gather(target: NodeTarget, probe: HostProbe, hub_dir: str) -> _NodeFacts:
    """Run every host command for one node, once."""
    facts = _NodeFacts()

    reach = probe.run(REACH_COMMAND, timeout=PROBE_TIMEOUT)
    if not reach.reachable:
        facts.reachable = False
        facts.unreachable_reason = reach.error or reach.message
        return facts
    if not reach.ok:
        facts.reachable = True
        facts.unreachable_reason = reach.message or f"exit {reach.returncode}"
        facts.docker = reach
        return facts

    facts.docker = probe.run(DOCKER_COMMAND)
    if facts.docker.ok:
        version, _, rest = facts.docker.stdout.strip().partition("|")
        root, _, runtimes = rest.partition("|")
        facts.docker_version = version.strip()
        facts.docker_root = root.strip() or facts.docker_root
        facts.runtimes = parse_runtimes(runtimes)

    facts.gpu = probe.run(GPU_COMMAND)
    if facts.gpu.ok:
        facts.gpus = parse_gpu_query(facts.gpu.stdout)
    meminfo = probe.run(MEMINFO_COMMAND)
    if meminfo.ok:
        facts.meminfo = parse_meminfo(meminfo.stdout)

    facts.ports = probe.run(PORTS_COMMAND)
    if facts.ports.ok:
        facts.listening = parse_listening_ports(facts.ports.stdout)

    facts.interfaces = probe.run(INTERFACES_COMMAND)
    if facts.interfaces.ok:
        head, _, tail = facts.interfaces.stdout.partition("== ib")
        facts.netdevs = {line.strip() for line in head.splitlines() if line.strip()}
        facts.ibdevs = {line.strip() for line in tail.splitlines() if line.strip()}

    facts.disk_probe = probe.run(disk_command([facts.docker_root, hub_dir]))
    if facts.disk_probe.ok:
        facts.disk = parse_df(facts.disk_probe.stdout)

    return facts


@dataclass
class _Context:
    """Everything the checks need, resolved once for the whole run."""

    plan: dict[str, Any]
    targets: list[NodeTarget]
    facts: dict[str, _NodeFacts]
    image_rows: dict[str, dict[str, Any]]
    model_rows: dict[str, dict[str, Any]]
    hub_dir: str
    image_bytes: int
    model_bytes: int
    wanted_digest: str = ""

    @property
    def node_count(self) -> int:
        return max(1, int(self.plan.get("node_count") or 1))


# ── The checks ───────────────────────────────────────────────────────────────


def _check_reachability(target: NodeTarget, ctx: _Context) -> Check:
    """Can we reach the node at all, and is that different from a bad command?

    The two failures get different remedies because they are different
    problems: a node that is off needs power and a route, a node that answers
    with a broken shell needs a login fixed.
    """
    facts = ctx.facts[target.id]
    title = "Reachable"
    if not facts.reachable:
        return _check(
            CHECK_REACHABILITY,
            title,
            target,
            STATUS_FAIL,
            f"could not open a connection to {target.label} "
            f"({target.address or 'no address'}): "
            f"{facts.unreachable_reason or 'no route or no credentials'}",
            f"Check that {target.label} is powered on and routable, then that "
            f"passwordless SSH works: "
            f"ssh {shlex.quote(target.ssh_user or 'USER')}@"
            f"{target.address or 'ADDRESS'} true. A host key that changed "
            "fails here too, and refusing it is the correct behaviour.",
            reachable=False,
        )
    if facts.unreachable_reason:
        return _check(
            CHECK_REACHABILITY,
            title,
            target,
            STATUS_FAIL,
            f"{target.label} answered, but the probe command failed: "
            f"{facts.unreachable_reason}",
            "The connection is fine, so this is the account's shell rather "
            f"than the network. Log in to {target.label} and check that a "
            "non-interactive shell runs commands and prints nothing extra.",
            reachable=True,
        )
    return _check(
        CHECK_REACHABILITY,
        title,
        target,
        STATUS_PASS,
        (
            "this machine"
            if target.is_control_plane
            else f"reached {target.address} over SSH"
        ),
        reachable=True,
    )


def _check_docker(target: NodeTarget, ctx: _Context) -> Check:
    """Docker installed, and the daemon answering."""
    facts = ctx.facts[target.id]
    title = "Docker"
    result = facts.docker
    if result is None or not result.reachable:
        return _check(
            CHECK_DOCKER,
            title,
            target,
            STATUS_FAIL,
            f"could not ask {target.label} about Docker: node unreachable",
            "Fix reachability first; this check has nothing to say until then.",
        )
    if result.ok and facts.docker_version:
        return _check(
            CHECK_DOCKER,
            title,
            target,
            STATUS_PASS,
            f"Docker {facts.docker_version} responding",
            version=facts.docker_version,
            root=facts.docker_root,
        )
    text = (result.stdout or result.stderr or "").lower()
    if result.returncode == 127 or "not found" in text or "command not found" in text:
        return _check(
            CHECK_DOCKER,
            title,
            target,
            STATUS_FAIL,
            f"docker is not installed on {target.label}",
            "Install Docker Engine on the node "
            "(https://docs.docker.com/engine/install/ubuntu/) and add the "
            f"login on {target.label} to the docker group, or every command "
            "here needs sudo.",
        )
    return _check(
        CHECK_DOCKER,
        title,
        target,
        STATUS_FAIL,
        f"docker on {target.label} did not answer: {result.message or 'no output'}",
        "The client is installed but the daemon did not reply. On the node: "
        "sudo systemctl start docker, then confirm the login is in the docker "
        "group with id -nG.",
    )


def _check_toolkit(target: NodeTarget, ctx: _Context) -> Check:
    """The NVIDIA container toolkit, registered as a Docker runtime.

    Every container this stack starts asks for ``--gpus all``. Without the
    toolkit registered, that request fails at ``docker run`` with a message
    about an unknown runtime, minutes into a deploy.
    """
    facts = ctx.facts[target.id]
    title = "NVIDIA container toolkit"
    if facts.docker is None or not facts.docker.ok:
        return _check(
            CHECK_TOOLKIT,
            title,
            target,
            STATUS_FAIL,
            f"could not read Docker's runtimes on {target.label}",
            "Fix the Docker check first; the toolkit is registered with the "
            "daemon, so an unreachable daemon cannot be asked.",
        )
    if any("nvidia" in name for name in facts.runtimes):
        return _check(
            CHECK_TOOLKIT,
            title,
            target,
            STATUS_PASS,
            f"registered as a Docker runtime ({', '.join(facts.runtimes)})",
            runtimes=facts.runtimes,
        )
    return _check(
        CHECK_TOOLKIT,
        title,
        target,
        STATUS_FAIL,
        f"no nvidia runtime is registered with Docker on {target.label} "
        f"(found: {', '.join(facts.runtimes) or 'none'})",
        "Every container this stack starts asks for --gpus all, which fails "
        "without it. On the node: install nvidia-container-toolkit, then "
        "sudo nvidia-ctk runtime configure --runtime=docker && "
        "sudo systemctl restart docker.",
        runtimes=facts.runtimes,
    )


def _check_gpu(target: NodeTarget, ctx: _Context) -> Check:
    """A GPU visible, and an honest answer about memory.

    On a DGX Spark ``nvidia-smi`` reports no GPU memory at all, because the GPU
    shares the host's unified pool. Believing the ``[N/A]`` as zero would fail
    a perfectly healthy node, so free memory comes from ``/proc/meminfo`` and a
    node is never failed for a figure ``nvidia-smi`` declined to give.
    """
    facts = ctx.facts[target.id]
    title = "GPU"
    result = facts.gpu
    if result is None or not result.reachable:
        return _check(
            CHECK_GPU,
            title,
            target,
            STATUS_FAIL,
            f"could not ask {target.label} about its GPUs: node unreachable",
            "Fix reachability first.",
        )
    if not result.ok or not facts.gpus:
        return _check(
            CHECK_GPU,
            title,
            target,
            STATUS_FAIL,
            f"nvidia-smi reported no GPU on {target.label}: "
            f"{result.message or 'no output'}",
            "The deployment asks for every GPU on the node, so it cannot "
            f"start here. On {target.label} run nvidia-smi: if the driver is "
            "missing install it, and if it is loaded but lists nothing, "
            "reboot after the driver install.",
            gpus=[],
        )

    names = ", ".join(f"{g['index']}: {g['name']}" for g in facts.gpus)
    reported = [g for g in facts.gpus if g.get("memory_free_mib") is not None]
    available = facts.meminfo.get("MemAvailable")

    if reported:
        free_mib = sum(int(g["memory_free_mib"] or 0) for g in reported)
        return _check(
            CHECK_GPU,
            title,
            target,
            STATUS_PASS,
            f"{len(facts.gpus)} GPU ({names}); {free_mib / 1024:.1f} GB free "
            "as reported by nvidia-smi",
            gpus=facts.gpus,
            free_bytes=free_mib * 1024 * 1024,
            memory_source="nvidia-smi",
        )
    if available:
        return _check(
            CHECK_GPU,
            title,
            target,
            STATUS_PASS,
            f"{len(facts.gpus)} GPU ({names}); nvidia-smi reports no GPU "
            "memory on this hardware because the pool is unified, so "
            f"{available / _GIB:.1f} GB is the free figure from /proc/meminfo",
            gpus=facts.gpus,
            free_bytes=int(available),
            memory_source="meminfo",
        )
    return _check(
        CHECK_GPU,
        title,
        target,
        STATUS_WARN,
        f"{len(facts.gpus)} GPU ({names}); free memory is unavailable — "
        "nvidia-smi does not report it on this hardware and /proc/meminfo "
        f"could not be read on {target.label}",
        "Nothing is broken and the deploy can proceed. The figure stays "
        "unknown, so size gpu_memory_utilization from a recipe verified on "
        "this hardware rather than from a value copied from an x86 recipe "
        "(NVIDIA's own Spark recipes use 0.4 solo, 0.8-0.9 at two nodes).",
        gpus=facts.gpus,
        free_bytes=None,
        memory_source="unavailable",
    )


def _check_image(target: NodeTarget, ctx: _Context) -> Check:
    """The image the plan resolved, compared by digest with what the node has.

    A tag match is not enough: same tag, different digest is exactly the drift
    case, and it is why the deploy pins a digest at all. An absent image is a
    **warning** — the deploy will work, it just has to move tens of gigabytes
    first, which is a wait to budget for rather than a reason to stop.
    """
    row = ctx.image_rows.get(target.id) or {}
    ref = str(ctx.plan.get("image_ref") or "")
    title = "Engine image"

    if row.get("error"):
        return _check(
            CHECK_IMAGE,
            title,
            target,
            STATUS_WARN,
            f"could not ask {target.label} whether it holds {ref}: " f"{row['error']}",
            "Assume a pull. Fix the Docker check and re-run the pre-flight to "
            "find out for certain.",
            delay_bytes=ctx.image_bytes,
            ref=ref,
            pull_required=True,
        )
    if row.get("matches"):
        seen = row.get("digest") or row.get("image_id") or "the planned digest"
        return _check(
            CHECK_IMAGE,
            title,
            target,
            STATUS_PASS,
            f"present at {seen}",
            ref=ref,
            pull_required=False,
            digest=row.get("digest", ""),
        )
    if row.get("present"):
        return _check(
            CHECK_IMAGE,
            title,
            target,
            STATUS_WARN,
            f"{target.label} holds {ref} at "
            f"{row.get('digest') or row.get('image_id') or 'an unknown digest'}"
            f", but the plan pins {ctx.wanted_digest or 'a different digest'}; "
            f"about {_bytes(ctx.image_bytes)} will transfer",
            f"Digest drift, not a broken node. Refresh it: seed the control "
            f"node's registry and have {target.label} pull from it with "
            "POST /api/images/sync, which keeps the registry credential on "
            "this machine.",
            delay_bytes=ctx.image_bytes,
            ref=ref,
            pull_required=True,
            digest=row.get("digest", ""),
        )
    return _check(
        CHECK_IMAGE,
        title,
        target,
        STATUS_WARN,
        f"{ref} is not on {target.label}; about {_bytes(ctx.image_bytes)} "
        "will transfer before the container starts",
        f"Not a failure — a wait. Pre-seed it so the deploy does not pay for "
        f"it: POST /api/images/sync copies the image into this machine's "
        f"registry once and has {target.label} pull from there over the fast "
        "link, with no registry credential leaving the control node.",
        delay_bytes=ctx.image_bytes,
        ref=ref,
        pull_required=True,
    )


def _check_model(target: NodeTarget, ctx: _Context) -> Check:
    """The weights, verified against the hub's own manifest on the node itself.

    Three states, because "the directory exists" was never an answer: a
    transfer that copied no symlinks, one that copied symlinks but no blobs and
    one that truncated every file all leave a directory behind.
    """
    row = ctx.model_rows.get(target.id) or {}
    model = str(ctx.plan.get("model") or "")
    title = "Model"

    if not model:
        return _check(
            CHECK_MODEL,
            title,
            target,
            STATUS_PASS,
            "the recipe names no model of its own; the command carries it",
            model="",
        )
    if row.get("error"):
        return _check(
            CHECK_MODEL,
            title,
            target,
            STATUS_FAIL,
            f"could not verify {model} on {target.label}: {row['error']}",
            "The verifier runs on the node with the node's own python. Check "
            f"that python3 exists on {target.label} and that "
            f"{ctx.hub_dir} is readable by the SSH login.",
            model=model,
        )

    state = str(row.get("state") or "absent")
    expected = int(row.get("bytes_expected") or 0)
    present = int(row.get("bytes_present") or 0)
    if state == "verified":
        return _check(
            CHECK_MODEL,
            title,
            target,
            STATUS_PASS,
            f"{model} verified against the manifest "
            f"({row.get('files_present') or 0} files, {_bytes(present)})",
            model=model,
            state=state,
        )
    if state == "partial":
        missing = list(row.get("missing") or [])
        named = ", ".join(missing[:3]) + ("…" if len(missing) > 3 else "")
        outstanding = max(expected - present, 0)
        return _check(
            CHECK_MODEL,
            title,
            target,
            STATUS_WARN,
            f"{model} is partial on {target.label}: {row.get('reason') or ''} "
            f"({row.get('missing_count') or len(missing)} missing"
            f"{', including ' + named if named else ''}); "
            f"{_bytes(outstanding)} outstanding",
            f"A partial copy fails at load time, not at start, so finish it "
            f"first: POST /api/models/{model}/replicate re-sends only what is "
            "missing and re-verifies on the node against the manifest.",
            delay_bytes=outstanding,
            costs_time=True,
            model=model,
            state=state,
            missing=missing[:20],
        )
    size = (
        _bytes(expected)
        if expected
        else "the weights, whose size nothing here can report yet"
    )
    return _check(
        CHECK_MODEL,
        title,
        target,
        STATUS_WARN,
        f"{model} is not on {target.label}; {size} must be copied first",
        f"Not a failure — a wait, and an hour-scale one. Replicate before "
        f"deploying: POST /api/models/{model}/replicate copies from this node "
        "over the fast link and verifies on arrival, so the hub token never "
        "leaves the control plane. When the control node has no copy either, "
        "download it here first with POST /api/models/download.",
        delay_bytes=expected,
        costs_time=True,
        model=model,
        state=state,
    )


def _check_ports(target: NodeTarget, ctx: _Context) -> list[Check]:
    """The API port and the rendezvous port, on every node that runs a rank.

    Checked on peers too, not only the control plane: a rank binds its API port
    on its own machine, and the port that is free here is a statement about
    here.

    The rendezvous port is nuanced on purpose. Above one node the launch binds
    it and a conflict is fatal; at one node vLLM derives a file-based store and
    never binds it at all, so a busy port there is worth saying and not worth
    failing over.
    """
    facts = ctx.facts[target.id]
    checks: list[Check] = []
    api_port = int(ctx.plan.get("port") or 0)
    rendezvous = ctx.plan.get("rendezvous_port")

    if facts.ports is None or not facts.ports.ok:
        return [
            _check(
                CHECK_PORTS,
                "Ports",
                target,
                STATUS_WARN,
                f"could not list listening ports on {target.label}: "
                f"{(facts.ports.message if facts.ports else '') or 'no output'}",
                "Neither ss nor netstat answered, so a conflict would only "
                "surface at start. Install iproute2 on the node to get this "
                "check back.",
                api_port=api_port,
            )
        ]

    for port, label, fatal in (
        (api_port, "API port", True),
        (int(rendezvous or 0), "rendezvous port", ctx.node_count > 1),
    ):
        if not port:
            continue
        title = f"{label} {port}"
        if port not in facts.listening:
            checks.append(
                _check(
                    CHECK_PORTS,
                    title,
                    target,
                    STATUS_PASS,
                    f"{port} is free on {target.label}",
                    port=port,
                    role=label,
                )
            )
            continue
        if fatal:
            checks.append(
                _check(
                    CHECK_PORTS,
                    title,
                    target,
                    STATUS_FAIL,
                    f"{port} is already bound on {target.label}",
                    f"The launch binds this port on {target.label} and will "
                    "fail. Find the holder there with "
                    f"sudo ss -ltnp 'sport = :{port}' and stop it, or pick "
                    "another API port with the port parameter.",
                    port=port,
                    role=label,
                )
            )
        else:
            checks.append(
                _check(
                    CHECK_PORTS,
                    title,
                    target,
                    STATUS_WARN,
                    f"{port} is bound on {target.label}, but at one node the "
                    "engine derives a file-based store and never binds its "
                    "rendezvous port",
                    "Nothing to do for this deployment. It would be fatal "
                    "above one node, so free it before adding a second node.",
                    port=port,
                    role=label,
                )
            )
    return checks


def _check_interfaces(target: NodeTarget, ctx: _Context) -> list[Check]:
    """That the interface names the plan will pin exist on the node.

    ``NCCL_SOCKET_IFNAME`` and ``GLOO_SOCKET_IFNAME`` are find-or-fail: a
    collective told to use ``enp1s0f0np0`` aborts if that name is missing
    rather than choosing another. They are resolved eagerly, before any
    rank-count logic, so a wrong name is a launch that dies at the first
    collective with a message from inside NCCL.

    Only above one node, which is exactly the gate the engines apply.
    """
    if ctx.node_count < 2:
        return []
    facts = ctx.facts[target.id]
    wanted_net, wanted_ib = _pinned_interfaces(target, ctx)
    if not wanted_net and not wanted_ib:
        return [
            _check(
                CHECK_INTERFACES,
                "Interfaces",
                target,
                STATUS_WARN,
                f"the plan pins no interface on {target.label}, so NCCL will "
                "choose one itself",
                "Autoselection picks the management link often enough to be a "
                "performance bug rather than a failure. Record the node's "
                "interfaces in the registry (PATCH /api/nodes/{id}) so the "
                "plan can pin the fabric.",
            )
        ]
    if facts.interfaces is None or not facts.interfaces.ok:
        return [
            _check(
                CHECK_INTERFACES,
                "Interfaces",
                target,
                STATUS_WARN,
                f"could not list interfaces on {target.label}",
                "The names cannot be confirmed, and a wrong one aborts the "
                "collective at launch. Check /sys/class/net is readable by "
                "the SSH login.",
            )
        ]

    checks: list[Check] = []
    for name, present, where in [
        (n, n in facts.netdevs, "/sys/class/net") for n in sorted(wanted_net)
    ] + [(n, n in facts.ibdevs, "/sys/class/infiniband") for n in sorted(wanted_ib)]:
        if present:
            checks.append(
                _check(
                    CHECK_INTERFACES,
                    f"Interface {name}",
                    target,
                    STATUS_PASS,
                    f"{name} exists on {target.label}",
                    interface=name,
                )
            )
            continue
        checks.append(
            _check(
                CHECK_INTERFACES,
                f"Interface {name}",
                target,
                STATUS_FAIL,
                f"the plan pins {name} but {target.label} has no such device "
                f"in {where} (it has "
                f"{', '.join(sorted(facts.netdevs | facts.ibdevs)) or 'none'})",
                f"Interface pinning is find-or-fail: the collective aborts on "
                f"{target.label} rather than picking another link. Correct the "
                "name on the node's registry record (PATCH /api/nodes/{id}) — "
                "ip -brief link on the node lists the real ones, and NVIDIA's "
                "two-port rule puts the two devices of one port on different "
                "subnets.",
                interface=name,
            )
        )
    return checks


def _pinned_interfaces(target: NodeTarget, ctx: _Context) -> tuple[set[str], set[str]]:
    """The netdev and InfiniBand names this node's rank will be pinned to.

    The rendered rank is the authority when it carries them; the registry
    record is the fallback, because that is what the eager resolution reads
    when a plan is built for real nodes.
    """
    netdevs: set[str] = set()
    ibdevs: set[str] = set()
    for rank in ctx.plan.get("ranks") or []:
        if int(rank.get("node_rank", -1)) not in target.ranks:
            continue
        env = rank.get("env") or {}
        for key in ("NCCL_SOCKET_IFNAME", "GLOO_SOCKET_IFNAME"):
            value = str(env.get(key) or "").strip()
            if value and value != "lo":
                netdevs.update(part for part in value.split(",") if part)
        hca = str(env.get("NCCL_IB_HCA") or "").strip()
        if hca:
            ibdevs.update(part for part in hca.split(",") if part)
    if not netdevs and target.ethernet_interface:
        netdevs.add(target.ethernet_interface)
    if not ibdevs:
        ibdevs.update(target.infiniband_interfaces)
    return netdevs, ibdevs


def _check_disk(target: NodeTarget, ctx: _Context) -> list[Check]:
    """Room for the image and for the weights, on the filesystems that hold them.

    Grouped by mount point, because a node with one big filesystem needs room
    for both at once and a node with two needs each separately — and reporting
    the wrong one of those is how a deploy dies at 94%.
    """
    facts = ctx.facts[target.id]
    image_needed = _needed_image_bytes(target, ctx)
    model_needed = _needed_model_bytes(target, ctx)
    unsized = _model_unsized(target, ctx)

    if unsized:
        return [
            _check(
                CHECK_DISK,
                "Disk",
                target,
                STATUS_WARN,
                f"{ctx.plan.get('model')} has to be copied to {target.label} "
                "and nothing here knows how big it is, so headroom cannot be "
                "judged",
                "The size is known once this control plane holds a verified "
                "copy. Download it here first, then re-run the pre-flight — "
                "or check free space on the node by hand, because a transfer "
                "that fills the disk leaves a partial copy to clean up.",
                mount="",
            )
        ]

    if not facts.disk:
        if not image_needed and not model_needed:
            return [
                _check(
                    CHECK_DISK,
                    "Disk",
                    target,
                    STATUS_PASS,
                    f"nothing has to be written on {target.label}",
                )
            ]
        return [
            _check(
                CHECK_DISK,
                "Disk",
                target,
                STATUS_WARN,
                f"could not measure free space on {target.label}, and "
                f"{_bytes(image_needed + model_needed)} has to land there",
                "df did not answer. Check free space by hand before "
                "deploying: a transfer that fills the disk leaves a partial "
                "copy behind that has to be cleaned up.",
            )
        ]

    docker_root = facts.docker_root
    wanted: dict[str, dict[str, Any]] = {}
    for path, need, what in (
        (docker_root, image_needed, "the engine image"),
        (ctx.hub_dir, model_needed, "the weights"),
    ):
        entry = facts.disk.get(path)
        if entry is None or not need:
            continue
        slot = wanted.setdefault(
            entry["mount"],
            {"need": 0, "for": [], "available": entry["available_bytes"]},
        )
        slot["need"] += need
        slot["for"].append(what)

    if not wanted:
        return [
            _check(
                CHECK_DISK,
                "Disk",
                target,
                STATUS_PASS,
                f"nothing has to be written on {target.label}",
            )
        ]

    checks: list[Check] = []
    for mount, slot in sorted(wanted.items()):
        need = int(slot["need"] * DISK_HEADROOM)
        available = int(slot["available"])
        what = " and ".join(slot["for"])
        title = f"Disk on {mount}"
        if available >= need:
            checks.append(
                _check(
                    CHECK_DISK,
                    title,
                    target,
                    STATUS_PASS,
                    f"{_bytes(available)} free on {mount}, {_bytes(need)} "
                    f"needed for {what}",
                    mount=mount,
                    available_bytes=available,
                    needed_bytes=need,
                )
            )
            continue
        checks.append(
            _check(
                CHECK_DISK,
                title,
                target,
                STATUS_FAIL,
                f"{mount} on {target.label} has {_bytes(available)} free but "
                f"{what} needs about {_bytes(need)} "
                f"({int(DISK_HEADROOM * 100 - 100)}% over the payload, because "
                "an image unpacks as well as downloads)",
                f"Free space on {target.label} before deploying: "
                "docker image prune -a removes engine images nothing runs, "
                "and DELETE /api/models/{id} removes weights this control "
                "plane knows are unused.",
                mount=mount,
                available_bytes=available,
                needed_bytes=need,
            )
        )
    return checks


def _needed_image_bytes(target: NodeTarget, ctx: _Context) -> int:
    row = ctx.image_rows.get(target.id) or {}
    return 0 if row.get("matches") else ctx.image_bytes


def _needed_model_bytes(target: NodeTarget, ctx: _Context) -> int:
    row = ctx.model_rows.get(target.id) or {}
    if str(row.get("state") or "") == "verified":
        return 0
    expected = int(row.get("bytes_expected") or 0)
    present = int(row.get("bytes_present") or 0)
    return max(expected - present, 0)


def _model_unsized(target: NodeTarget, ctx: _Context) -> bool:
    """Whether weights have to land here and nothing can say how many bytes.

    Reported rather than guessed. A headroom check against an invented figure
    passes for a reason that has nothing to do with the disk.
    """
    if not ctx.plan.get("model"):
        return False
    row = ctx.model_rows.get(target.id) or {}
    if row.get("error") or str(row.get("state") or "") == "verified":
        return False
    return not int(row.get("bytes_expected") or 0)


#: The check functions, in report order. Each returns one check or several.
_CHECKS: tuple[tuple[str, Callable[[NodeTarget, _Context], Any]], ...] = (
    (CHECK_REACHABILITY, _check_reachability),
    (CHECK_DOCKER, _check_docker),
    (CHECK_TOOLKIT, _check_toolkit),
    (CHECK_GPU, _check_gpu),
    (CHECK_IMAGE, _check_image),
    (CHECK_MODEL, _check_model),
    (CHECK_PORTS, _check_ports),
    (CHECK_INTERFACES, _check_interfaces),
    (CHECK_DISK, _check_disk),
)


def checks_for_node(target: NodeTarget, ctx: _Context) -> list[Check]:
    """Every check for one node, in report order.

    An unreachable node short-circuits: every later check would report the same
    "could not ask" and bury the one fact that matters.
    """
    reach = _check_reachability(target, ctx)
    if reach.failed:
        return [reach]
    results: list[Check] = [reach]
    for _check_id, fn in _CHECKS[1:]:
        produced = fn(target, ctx)
        if isinstance(produced, Check):
            results.append(produced)
        else:
            results.extend(produced)
    return results


# ── The verdict ──────────────────────────────────────────────────────────────


def verdict_for(checks: list[Check]) -> tuple[str, str]:
    """The overall answer, and why — in that order of importance.

    Not the worst status. A pre-flight whose verdict is the maximum of its
    checks reads red whenever anything at all needs doing, and an operator
    stops looking at it. So: a failure blocks, bytes that must move make it
    slow, and everything else is ready with advisories attached.
    """
    failed = [c for c in checks if c.status == STATUS_FAIL]
    if failed:
        nodes = sorted({c.node for c in failed})
        return (
            VERDICT_BLOCKED,
            f"cannot proceed: {len(failed)} check"
            f"{'' if len(failed) == 1 else 's'} failed on "
            f"{', '.join(nodes)}",
        )

    delaying = [c for c in checks if c.delays]
    if delaying:
        total = sum(c.delay_bytes for c in delaying)
        nodes = sorted({c.node for c in delaying})
        moved = f"about {_bytes(total)}" if total else "data of unreported size"
        return (
            VERDICT_SLOW,
            f"will run, but {moved} has to transfer first across "
            f"{', '.join(nodes)}",
        )

    advisories = [c for c in checks if c.status == STATUS_WARN]
    if advisories:
        return (
            VERDICT_READY,
            f"ready; {len(advisories)} advisor"
            f"{'y' if len(advisories) == 1 else 'ies'} worth reading",
        )
    return VERDICT_READY, "ready: every check passed"


# ── Node resolution ──────────────────────────────────────────────────────────


def targets_for(plan: dict[str, Any]) -> list[NodeTarget]:
    """The machines a plan will actually run on.

    A solo plan is a cluster of size one, so it resolves to the control node
    rather than to nothing: that is the machine the container starts on and the
    machine whose ports, disk and image matter.
    """
    addresses = [str(a) for a in (plan.get("nodes") or []) if str(a).strip()]
    records = _registry_records()

    if not addresses:
        control = _control_record(records)
        return [_target_from(control, is_control=True, ranks=(0,))]

    targets: list[NodeTarget] = []
    for rank, address in enumerate(addresses):
        record = records.get(address)
        is_control = bool(record and record.is_control_plane) or _is_local(address)
        targets.append(
            _target_from(record, address=address, is_control=is_control, ranks=(rank,))
        )
    return targets


def _registry_records() -> dict[str, Any]:
    try:
        return {node.address: node for node in tools.node_registry.list_nodes()}
    except Exception as exc:  # pragma: no cover — an unreadable registry
        logger.debug("could not read the node registry: %s", exc)
        return {}


def _control_record(records: dict[str, Any]) -> Any | None:
    for node in records.values():
        if node.is_control_plane:
            return node
    try:
        return tools.node_registry.self_node()
    except Exception:  # pragma: no cover — best effort
        return None


def _is_local(address: str) -> bool:
    try:
        return bool(tools.node_service.is_local_address(address))
    except Exception:  # pragma: no cover — best effort
        return False


def _target_from(
    record: Any | None,
    address: str = "",
    is_control: bool = False,
    ranks: tuple[int, ...] = (),
) -> NodeTarget:
    if record is None:
        resolved = address or "localhost"
        return NodeTarget(
            id=f"address:{resolved}",
            label=("this machine" if is_control else resolved),
            address=resolved,
            is_control_plane=is_control,
            ranks=ranks,
        )
    return NodeTarget(
        id=record.id,
        label=record.label,
        address=address or record.address,
        is_control_plane=is_control or record.is_control_plane,
        ssh_user=record.ssh_user,
        ethernet_interface=record.ethernet_interface,
        infiniband_interfaces=tuple(record.infiniband_interfaces),
        ranks=ranks,
    )


# ── Gathering the container and model facts ──────────────────────────────────


def _image_rows(
    targets: list[NodeTarget],
    plan: dict[str, Any],
    services: Callable[[Any], Any] | None,
) -> tuple[dict[str, dict[str, Any]], str, int]:
    """What each node holds for the planned image, compared by digest."""
    ref = str(plan.get("image_ref") or "")
    rows: dict[str, dict[str, Any]] = {}
    if not ref:
        return rows, "", 0

    resolve = services or tools.node_service.NodeServices()
    wanted_digest = str(tools.registry.location_for(ref).digest or "")
    size = int(plan.get("image_size_bytes") or 0)
    control_id = ""

    def _one(target: NodeTarget) -> dict[str, Any]:
        node = tools.node_service.Node(
            id=target.id,
            address="" if target.is_control_plane else target.address,
            is_self=target.is_control_plane,
            ssh_user=target.ssh_user,
        )
        try:
            info = resolve(node).image_info(ref)
        except Exception as exc:  # noqa: BLE001 — every transport failure alike
            return {"present": False, "error": str(exc)[:300]}
        if info is None:
            return {"present": False, "error": None}
        digests = [
            str(entry).partition("@")[2] for entry in (info.get("repo_digests") or [])
        ]
        return {
            "present": True,
            "error": None,
            "image_id": str(info.get("id") or ""),
            "digest": digests[0] if digests else "",
            "digests": digests,
            "size_bytes": int(info.get("size_bytes") or 0),
        }

    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        gathered = list(pool.map(_one, targets))

    control_holds_it = False
    for target, row in zip(targets, gathered):
        rows[target.id] = row
        if target.is_control_plane:
            control_id = str(row.get("image_id") or "")
            if not wanted_digest and row.get("digest"):
                wanted_digest = str(row["digest"])
            control_holds_it = bool(
                row.get("present")
                and (not wanted_digest or wanted_digest in (row.get("digests") or []))
            )
            size = size or int(row.get("size_bytes") or 0)

    # A node "matches" when it carries the pinned digest. A matching *tag* is
    # never enough — same tag, different digest is the drift case itself, and
    # refreshing it is the point. The one fallback is a peer whose daemon
    # reports no repo digest at all: then an image ID identical to the control
    # node's is the same content, and only if the control node holds the
    # pinned digest in the first place.
    for target in targets:
        row = rows[target.id]
        if not row.get("present"):
            row["matches"] = False
            continue
        digests = row.get("digests") or []
        if wanted_digest:
            row["matches"] = wanted_digest in digests
        else:
            row["matches"] = target.is_control_plane or not control_id
        if row["matches"] or target.is_control_plane or not control_holds_it:
            continue
        row["matches"] = bool(
            not digests and control_id and row.get("image_id") == control_id
        )

    return rows, wanted_digest, size or ASSUMED_IMAGE_BYTES


def _model_rows(
    targets: list[NodeTarget],
    plan: dict[str, Any],
    presence: Callable[..., dict[str, Any]] | None,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Per-node model state, from the hub-cache verifier phase B shipped."""
    model = str(plan.get("model") or "")
    rows: dict[str, dict[str, Any]] = {}
    if not model:
        return rows, 0

    peers = [t for t in targets if not t.is_control_plane]
    ask = presence or tools.models.presence
    try:
        report = ask(model, [t.address for t in peers])
    except Exception as exc:  # noqa: BLE001 — report it rather than raise
        logger.debug("model presence failed for %s: %s", model, exc)
        return {t.id: {"error": str(exc)[:300]} for t in targets}, 0

    local = report.get("local_report") or {}
    local_state = str(report.get("local_state") or local.get("state") or "absent")
    by_address = {str(row.get("node")): row for row in report.get("nodes") or []}
    expected = int(local.get("bytes_expected") or 0)

    for target in targets:
        if target.is_control_plane:
            rows[target.id] = {
                "state": local_state,
                "reason": local.get("reason", ""),
                "bytes_expected": expected,
                "bytes_present": int(local.get("bytes_present") or 0),
                "files_present": int(local.get("files_present") or 0),
                "missing": list(local.get("missing") or []),
                "missing_count": int(local.get("missing_count") or 0),
                "error": None,
            }
            continue
        row = dict(by_address.get(target.address) or {})
        row.setdefault("state", "absent")
        # A node that reports no expectation of its own inherits the control
        # node's: the manifest is the same document on every machine.
        if not row.get("bytes_expected"):
            row["bytes_expected"] = expected
        rows[target.id] = row

    largest = max(
        (int(row.get("bytes_expected") or 0) for row in rows.values()),
        default=0,
    )
    return rows, largest


# ── The entry point ──────────────────────────────────────────────────────────


def run(
    recipe_id: str = "",
    *,
    engine: str | None = None,
    variant: str | None = None,
    model: str | None = None,
    params: dict[str, Any] | None = None,
    extra_args: list[str] | None = None,
    nodes: list[str] | None = None,
    allow_missing_model: bool = True,
    plan: dict[str, Any] | None = None,
    targets: list[NodeTarget] | None = None,
    probe_factory: Callable[..., HostProbe] | None = None,
    ssh_client: SSHClient | None = None,
    services: Callable[[Any], Any] | None = None,
    model_presence: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run every pre-flight check for a deployment and report the verdict.

    Takes the same inputs as the deploy plan, and resolves the plan itself when
    one is not handed in, so an operator asks the same question of the
    pre-flight that they asked of the preview.

    Args:
        recipe_id: The recipe to plan. Ignored when ``plan`` is supplied.
        engine, variant, model, params, extra_args, nodes, allow_missing_model:
            The plan's own knobs, passed through unchanged.
        plan: An already-resolved plan, to avoid planning twice.
        targets: Override the node set (tests).
        probe_factory: Builds the host probe for a node (simulation, tests).
        ssh_client: SSH transport handed to the default probe factory.
        services: Node-service resolver (simulation, tests).
        model_presence: The model verifier seam (simulation, tests).

    Returns:
        The report: verdict, summary, every check, and the checks split into
        what blocks, what delays and what is merely worth reading.
    """
    if plan is None:
        plan = tools.deploy_dispatch.plan_deployment(
            recipe_id=recipe_id,
            engine=engine,
            variant=variant,
            model=model,
            params=params or {},
            extra_args=extra_args or [],
            nodes=nodes or None,
            allow_missing_model=allow_missing_model,
        )

    node_targets = targets if targets is not None else targets_for(plan)
    hub_dir = _hub_dir()
    build_probe = probe_factory or (
        lambda target: probe_for(target, ssh_client=ssh_client)
    )

    with ThreadPoolExecutor(max_workers=max(1, len(node_targets))) as pool:
        gathered = list(
            pool.map(
                lambda target: _gather(target, build_probe(target), hub_dir),
                node_targets,
            )
        )
    facts = {t.id: f for t, f in zip(node_targets, gathered)}

    reachable = [t for t in node_targets if facts[t.id].reachable]
    image_rows, wanted_digest, image_bytes = _image_rows(reachable, plan, services)
    model_rows, model_bytes = _model_rows(reachable, plan, model_presence)

    ctx = _Context(
        plan=plan,
        targets=node_targets,
        facts=facts,
        image_rows=image_rows,
        model_rows=model_rows,
        hub_dir=hub_dir,
        image_bytes=image_bytes,
        model_bytes=model_bytes,
        wanted_digest=wanted_digest,
    )

    checks: list[Check] = []
    for target in node_targets:
        checks.extend(checks_for_node(target, ctx))

    verdict, summary = verdict_for(checks)
    blocking = [c for c in checks if c.status == STATUS_FAIL]
    delaying = [c for c in checks if c.delays]
    advisories = [c for c in checks if c.status == STATUS_WARN and not c.delays]

    return {
        "verdict": verdict,
        "summary": summary,
        "can_proceed": verdict != VERDICT_BLOCKED,
        "delays": bool(delaying),
        "estimated_transfer_bytes": sum(c.delay_bytes for c in delaying),
        "counts": {
            STATUS_PASS: sum(1 for c in checks if c.status == STATUS_PASS),
            STATUS_WARN: sum(1 for c in checks if c.status == STATUS_WARN),
            STATUS_FAIL: len(blocking),
        },
        "nodes": [t.to_dict() for t in node_targets],
        "checks": [c.to_dict() for c in checks],
        "blocking": [c.to_dict() for c in blocking],
        "delaying": [c.to_dict() for c in delaying],
        "advisories": [c.to_dict() for c in advisories],
        "plan": {
            "recipe_id": plan.get("recipe_id", ""),
            "engine": plan.get("engine", ""),
            "variant": plan.get("variant", ""),
            "image_ref": plan.get("image_ref", ""),
            "model": plan.get("model", ""),
            "port": plan.get("port"),
            "rendezvous_port": plan.get("rendezvous_port"),
            "node_count": plan.get("node_count", 1),
        },
        "checked_at": _now(),
    }


def _hub_dir() -> str:
    try:
        return str(tools.models.hub_dir())
    except Exception:  # pragma: no cover — best effort
        return "~/.cache/huggingface/hub"


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
    "DOCKER_COMMAND",
    "GPU_COMMAND",
    "INTERFACES_COMMAND",
    "MEMINFO_COMMAND",
    "PORTS_COMMAND",
    "PROBE_TIMEOUT",
    "REACH_COMMAND",
    "STATUSES",
    "STATUS_FAIL",
    "STATUS_PASS",
    "STATUS_WARN",
    "VERDICTS",
    "VERDICT_BLOCKED",
    "VERDICT_READY",
    "VERDICT_SLOW",
    "Check",
    "HostProbe",
    "LocalHostProbe",
    "NodeTarget",
    "ProbeResult",
    "SSHHostProbe",
    "checks_for_node",
    "disk_command",
    "parse_df",
    "parse_gpu_query",
    "parse_listening_ports",
    "parse_meminfo",
    "parse_runtimes",
    "probe_for",
    "run",
    "targets_for",
    "verdict_for",
]
