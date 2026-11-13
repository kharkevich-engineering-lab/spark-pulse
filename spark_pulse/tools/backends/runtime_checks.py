"""What a node's container runtime needs to run an engine, per runtime (#168).

The doctor and the pre-flight used to hard-code Docker: ``docker info`` through
the CLI, the daemon's registered runtimes, the docker group. On a rootless
Podman desktop that CLI is the podman-docker shim, whose ``docker info`` cannot
render Docker's template, so the pre-flight blocked every deploy there with
"``sudo systemctl start docker``". The checks now live here, one set per
runtime, chosen from what the node's own agent reports (``container_runtime``,
#166), and both callers reach them through the run backend's ``diagnose``.

Each set is pure. It *declares* the shell commands it needs (:meth:`commands`)
and turns their output plus the agent's facts into findings
(:meth:`evaluate`); running them is the caller's business, because the
pre-flight and the doctor each already have a transport to the node's agent
and neither should grow a second one. ``tests/test_run_backends.py`` holds the
callers to that: nothing outside ``tools/backends`` names a check set.

Statuses are the doctor's vocabulary (``ok`` · ``warn`` · ``broken`` ·
``unknown``); the pre-flight maps them onto its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

#: Finding ids. The daemon's keeps the doctor's historical name on Docker.
DOCKER_DAEMON = "docker-daemon"
PODMAN_DAEMON = "podman-daemon"
NO_RUNTIME = "container-runtime"
GPU_ACCESS = "gpu-access"
PODMAN_SOCKET = "podman-socket"
SUBUID = "subuid"

#: Kinds, so a caller can place a finding without knowing every id.
KIND_DAEMON = "daemon"
KIND_GPU = "gpu"
KIND_HOST = "host"

DOCKER_ROOT = "/var/lib/docker"

# ── Commands ─────────────────────────────────────────────────────────────────

#: One round trip answers everything about docker: the daemon version, its
#: registered runtimes and where it keeps its data.
DOCKER_COMMAND = (
    "docker info --format "
    "'{{.ServerVersion}}|{{.DockerRootDir}}|"
    "{{range $name, $_ := .Runtimes}}{{$name}} {{end}}' 2>&1"
)

#: What lets a container reach the GPU. Three separate mechanisms can each
#: provide it, so the probe reports evidence rather than a verdict; see
#: :func:`parse_toolkit`. Ends in ``true`` because the final ``ls`` fails on a
#: machine with no CDI directory, which is the ordinary case.
TOOLKIT_COMMAND = (
    "command -v nvidia-container-runtime-hook >/dev/null 2>&1 && echo hook; "
    "command -v nvidia-ctk >/dev/null 2>&1 && echo ctk; "
    "ls -1 /etc/cdi/*.yaml /etc/cdi/*.json /var/run/cdi/*.yaml /var/run/cdi/*.json "
    "2>/dev/null | sed 's/^/cdi /'; true"
)

#: Where rootless Podman keeps images, which is what the disk check measures.
PODMAN_STORE_COMMAND = "podman info --format '{{.Store.GraphRoot}}' 2>&1"

#: Whether the user's socket survives a reboot: the agent found it live, but a
#: socket that was only *started* is gone after the next boot.
PODMAN_SOCKET_COMMAND = "systemctl --user is-enabled podman.socket 2>&1; true"

#: Rootless Podman maps container uids onto the user's subordinate ranges, and
#: refuses to create a container without them.
SUBUID_COMMAND = (
    "u=$(id -un); for f in /etc/subuid /etc/subgid; do "
    'if grep -q "^$u:" "$f" 2>/dev/null; then echo "$f yes"; '
    'else echo "$f no"; fi; done'
)

#: The sockets a daemon could be on, for a node where none answered the agent.
SOCKETS_COMMAND = (
    "for s in /var/run/docker.sock "
    '"${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"; do '
    '[ -S "$s" ] && echo "$s"; done; true'
)


# ── Parsing ──────────────────────────────────────────────────────────────────


def parse_toolkit(text: str) -> dict[str, Any]:
    """Evidence from :data:`TOOLKIT_COMMAND` — which GPU paths exist."""
    hook = ctk = False
    cdi: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line == "hook":
            hook = True
        elif line == "ctk":
            ctk = True
        elif line.startswith("cdi "):
            cdi.append(line[4:].strip())
    return {"hook": hook, "ctk": ctk, "cdi": cdi}


def parse_runtimes(text: str) -> list[str]:
    """Runtime names out of :data:`DOCKER_COMMAND`'s third field.

    The probe asks for names only, because ``{{json .Runtimes}}`` returns
    several kilobytes of runtime-spec feature data per runtime, and a regex
    over that picks up the nested ``"status": {`` key and reports a runtime
    called ``status``. JSON is still accepted so an older probe's output, or a
    stored report, still parses.
    """
    text = (text or "").strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            pass
        else:
            if isinstance(parsed, dict):
                return sorted(parsed)
    return sorted({name for name in text.split() if name})


# ── What goes in and what comes out ──────────────────────────────────────────


class ProbeOutput(Protocol):
    """One command's outcome: the pre-flight's ``ProbeResult`` has this shape."""

    reachable: bool
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool: ...

    @property
    def message(self) -> str: ...


#: Runs one shell command on the node and returns what happened, or ``None``
#: when it could not be asked.
Runner = Callable[[str], "ProbeOutput | None"]


@dataclass(frozen=True)
class RuntimeFacts:
    """What the node's agent said about its runtime, or that it said nothing.

    ``reported`` is false when there is no connection to ask, which is not the
    same as a connected agent whose daemon answered nothing.
    """

    reported: bool = False
    name: str = ""
    version: str = ""
    rootless: bool = False
    cdi_devices: tuple[str, ...] = ()
    docker_version: str = ""

    @classmethod
    def from_node_facts(cls, facts: Any) -> "RuntimeFacts":
        return cls(
            reported=True,
            name=str(getattr(facts, "container_runtime", "") or ""),
            version=str(getattr(facts, "runtime_version", "") or ""),
            rootless=bool(getattr(facts, "rootless", False)),
            cdi_devices=tuple(getattr(facts, "cdi_devices", ()) or ()),
            docker_version=str(getattr(facts, "docker_version", "") or ""),
        )


@dataclass(frozen=True)
class RuntimeFinding:
    """One thing about the runtime, in the doctor's vocabulary."""

    check: str
    kind: str
    title: str
    status: str  # ok | warn | broken | unknown
    detail: str
    remedy: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeReport:
    """Every finding for one node, and where its runtime keeps images."""

    runtime: str  # docker | podman | unknown
    findings: list[RuntimeFinding]
    storage_root: str = ""

    def of_kind(self, kind: str) -> list[RuntimeFinding]:
        return [f for f in self.findings if f.kind == kind]


class RuntimeChecks(Protocol):
    name: str

    def commands(self) -> dict[str, str]:
        """The shell commands this runtime's checks read, by key."""
        ...

    def evaluate(
        self,
        label: str,
        facts: RuntimeFacts,
        outputs: dict[str, ProbeOutput | None],
    ) -> RuntimeReport:
        """Findings from the agent's facts and the commands' output. Pure."""
        ...


def run_checks(
    checks: RuntimeChecks, label: str, facts: RuntimeFacts, run: Runner | None
) -> RuntimeReport:
    """Run a set's commands with ``run`` and evaluate them."""
    outputs: dict[str, ProbeOutput | None] = {}
    for key, command in checks.commands().items():
        outputs[key] = run(command) if run is not None else None
    return checks.evaluate(label, facts, outputs)


def checks_for(facts: RuntimeFacts) -> RuntimeChecks:
    """The check set for a node, from what its agent reports.

    * ``podman`` and ``docker`` are what they say.
    * Nothing reported (no connection, or simulation) is Docker as before,
      read from the probe alone: the pre-flight on a node it cannot see the
      agent of must behave exactly as it always has.
    * An agent older than the field that still reports a daemon version is
      Docker — that is the only runtime such an agent ran against.
    * A connected agent with no runtime and no version: nothing answered it,
      and that is a finding of its own.
    """
    if facts.name == "podman":
        return PodmanChecks()
    if facts.name == "docker" or not facts.reported or facts.docker_version:
        return DockerChecks()
    return UnknownRuntimeChecks()


def _usable(output: ProbeOutput | None) -> bool:
    return output is not None and output.ok


# ── Docker ───────────────────────────────────────────────────────────────────


class DockerChecks:
    """Docker: the daemon, and how a container reaches the GPU."""

    name = "docker"

    def commands(self) -> dict[str, str]:
        return {"docker": DOCKER_COMMAND, "toolkit": TOOLKIT_COMMAND}

    def evaluate(
        self,
        label: str,
        facts: RuntimeFacts,
        outputs: dict[str, ProbeOutput | None],
    ) -> RuntimeReport:
        result = outputs.get("docker")
        version = root = ""
        runtimes: list[str] = []
        if _usable(result):
            assert result is not None
            version, _, rest = result.stdout.strip().partition("|")
            root_text, _, runtime_text = rest.partition("|")
            version = version.strip()
            root = root_text.strip()
            runtimes = parse_runtimes(runtime_text)
        storage_root = root or DOCKER_ROOT
        daemon = self._daemon(label, facts, result, version, storage_root)
        toolkit = outputs.get("toolkit")
        evidence = (
            parse_toolkit(toolkit.stdout)
            if _usable(toolkit) and toolkit is not None
            else {"hook": False, "ctk": False, "cdi": []}
        )
        if result is None or not result.reachable:
            gpu = RuntimeFinding(
                GPU_ACCESS,
                KIND_GPU,
                "NVIDIA container toolkit",
                "unknown",
                f"could not ask {label} how a container reaches the GPU",
            )
        else:
            gpu = self._gpu(label, _usable(result), runtimes, evidence)
        return RuntimeReport("docker", [daemon, gpu], storage_root)

    def _daemon(
        self,
        label: str,
        facts: RuntimeFacts,
        result: ProbeOutput | None,
        version: str,
        root: str,
    ) -> RuntimeFinding:
        def finding(status: str, detail: str, remedy: str = "", **data: Any):
            return RuntimeFinding(
                DOCKER_DAEMON, KIND_DAEMON, "Docker", status, detail, remedy, data
            )

        if version:
            return finding(
                "ok", f"Docker {version} responding", version=version, root=root
            )
        if facts.reported and facts.docker_version:
            # The agent's own connection is the one engines are created on, so
            # its answer stands even where the docker CLI is missing or says
            # something else.
            return finding(
                "ok",
                f"Docker {facts.docker_version} responding",
                version=facts.docker_version,
                root=root,
            )
        if result is None or not result.reachable:
            if facts.reported:
                return finding(
                    "broken",
                    "the agent is connected and its Docker daemon answered "
                    "nothing — the daemon is down or unreachable to it. This "
                    "is not 'no such container': every container query on "
                    "this node is unknown, not empty",
                    "a daemon that will not start is a job on that machine; "
                    "restarting it from here would kill whatever it is still "
                    "running",
                )
            return finding(
                "broken",
                f"could not ask {label} about Docker: node unreachable",
                "Fix reachability first; this check has nothing to say until then.",
            )
        text = (result.stdout or result.stderr or "").lower()
        if (
            result.returncode == 127
            or "not found" in text
            or "command not found" in text
        ):
            return finding(
                "broken",
                f"docker is not installed on {label}",
                "Install Docker Engine on the node "
                "(https://docs.docker.com/engine/install/ubuntu/) and add the "
                f"login on {label} to the docker group, or every command "
                "here needs sudo.",
            )
        return finding(
            "broken",
            f"docker on {label} did not answer: {result.message or 'no output'}",
            "The client is installed but the daemon did not reply. On the node: "
            "sudo systemctl start docker, then confirm the login is in the docker "
            "group with id -nG.",
        )

    def _gpu(
        self,
        label: str,
        asked: bool,
        runtimes: list[str],
        evidence: dict[str, Any],
    ) -> RuntimeFinding:
        """Can a container reach the GPU?

        Every container this stack starts asks for ``--gpus all``, and three
        different mechanisms can satisfy that. Only one of them puts a runtime
        named ``nvidia`` in ``docker info``:

        * ``--runtime=nvidia`` needs the registered runtime. We never pass it.
        * ``--gpus all`` — what we actually pass — makes the daemon resolve a
          *device request*, which it hands to ``nvidia-container-runtime-hook``
          found on ``PATH``. No runtime registration is involved.
        * CDI device specs under ``/etc/cdi`` or ``/var/run/cdi``.

        A stock DGX Spark has the toolkit installed and no ``nvidia`` runtime
        registered, and ``--gpus all`` works there — so demanding the runtime
        name blocked every deploy on a correctly configured machine.
        """
        title = "NVIDIA container toolkit"

        def finding(status: str, detail: str, remedy: str = ""):
            return RuntimeFinding(
                GPU_ACCESS,
                KIND_GPU,
                title,
                status,
                detail,
                remedy,
                {
                    "runtimes": runtimes,
                    "hook": bool(evidence["hook"]),
                    "cdi_specs": list(evidence["cdi"]),
                },
            )

        if not asked:
            return RuntimeFinding(
                GPU_ACCESS,
                KIND_GPU,
                title,
                "broken",
                f"could not read Docker's runtimes on {label}",
                "Fix the Docker check first; the toolkit is registered with the "
                "daemon, so an unreachable daemon cannot be asked.",
            )
        runtime = next((n for n in runtimes if "nvidia" in n), "")
        if evidence["hook"]:
            # The path --gpus all actually takes, so report it as the reason
            # even when a runtime is registered too.
            return finding(
                "ok",
                "nvidia-container-runtime-hook is installed, which is what "
                "--gpus all uses"
                + (f"; runtime {runtime} is registered as well" if runtime else ""),
            )
        if runtime:
            return finding(
                "ok", f"registered as a Docker runtime ({', '.join(runtimes)})"
            )
        cdi = list(evidence["cdi"])
        if cdi:
            return finding(
                "ok",
                f"{len(cdi)} CDI device spec(s) present ({', '.join(cdi[:3])})",
            )
        return finding(
            "broken",
            f"no way for a container to reach the GPU on {label}: no "
            "nvidia-container-runtime-hook on PATH, no CDI specs, and no nvidia "
            f"runtime registered (runtimes: {', '.join(runtimes) or 'none'})",
            "Every container this stack starts asks for --gpus all, which fails "
            "without it. On the node: install nvidia-container-toolkit, then "
            "sudo nvidia-ctk runtime configure --runtime=docker && "
            "sudo systemctl restart docker.",
        )


# ── Podman ───────────────────────────────────────────────────────────────────


class PodmanChecks:
    """Podman: the daemon, a CDI GPU, and what rootless operation needs.

    The agent creates an engine through Podman's own API with the GPU as a
    CDI device (#167), so the GPU question is "does a CDI spec name one",
    answered from the agent's facts — not Docker's toolkit hook, which Podman
    never consults.
    """

    name = "podman"

    def commands(self) -> dict[str, str]:
        return {
            "store": PODMAN_STORE_COMMAND,
            "socket": PODMAN_SOCKET_COMMAND,
            "subuid": SUBUID_COMMAND,
        }

    def evaluate(
        self,
        label: str,
        facts: RuntimeFacts,
        outputs: dict[str, ProbeOutput | None],
    ) -> RuntimeReport:
        mode = "rootless" if facts.rootless else "rootful"
        store = outputs.get("store")
        storage_root = (
            store.stdout.strip().splitlines()[0]
            if _usable(store) and store is not None and store.stdout.strip()
            else ""
        )
        daemon = RuntimeFinding(
            PODMAN_DAEMON,
            KIND_DAEMON,
            "Podman",
            "ok",
            f"Podman {facts.version or '?'} responding ({mode})",
            data={
                "version": facts.version,
                "rootless": facts.rootless,
                "root": storage_root,
            },
        )
        findings = [daemon, self._gpu(label, facts)]
        if facts.rootless:
            findings.append(self._socket(label, outputs.get("socket")))
            findings.append(self._subuid(label, outputs.get("subuid")))
        return RuntimeReport("podman", findings, storage_root)

    def _gpu(self, label: str, facts: RuntimeFacts) -> RuntimeFinding:
        gpus = [d for d in facts.cdi_devices if d.startswith("nvidia.com/gpu=")]
        if gpus:
            chosen = "nvidia.com/gpu=all" if "nvidia.com/gpu=all" in gpus else gpus[0]
            return RuntimeFinding(
                GPU_ACCESS,
                KIND_GPU,
                "GPU access",
                "ok",
                f"CDI names the GPU; engines get {chosen}",
                data={"cdi_devices": list(facts.cdi_devices)},
            )
        return RuntimeFinding(
            GPU_ACCESS,
            KIND_GPU,
            "GPU access",
            "broken",
            f"no CDI spec on {label} names a GPU, and Podman reaches a GPU only "
            "through CDI: an engine would start without one",
            "On the node: sudo nvidia-ctk cdi generate "
            "--output=/etc/cdi/nvidia.yaml (nvidia-container-toolkit provides "
            "nvidia-ctk).",
            {"cdi_devices": list(facts.cdi_devices)},
        )

    def _socket(self, label: str, output: ProbeOutput | None) -> RuntimeFinding:
        title = "Podman socket"
        if output is None or not output.reachable:
            return RuntimeFinding(
                PODMAN_SOCKET, KIND_HOST, title, "unknown", f"could not ask {label}"
            )
        state = (output.stdout or "").strip().splitlines()
        state_word = state[0].strip() if state else ""
        if state_word == "enabled":
            return RuntimeFinding(
                PODMAN_SOCKET,
                KIND_HOST,
                title,
                "ok",
                "podman.socket is enabled for the agent's user",
            )
        return RuntimeFinding(
            PODMAN_SOCKET,
            KIND_HOST,
            title,
            "warn",
            f"podman.socket is {state_word or 'not enabled'} for the agent's user "
            f"on {label}: it answers now, and after a reboot the agent finds no "
            "daemon",
            "On the node, as the agent's user: systemctl --user enable --now "
            "podman.socket",
        )

    def _subuid(self, label: str, output: ProbeOutput | None) -> RuntimeFinding:
        title = "Subordinate ids"
        if output is None or not output.reachable or not output.stdout.strip():
            return RuntimeFinding(
                SUBUID, KIND_HOST, title, "unknown", f"could not ask {label}"
            )
        missing = [
            line.split()[0]
            for line in output.stdout.splitlines()
            if line.strip().endswith(" no")
        ]
        if not missing:
            return RuntimeFinding(
                SUBUID,
                KIND_HOST,
                title,
                "ok",
                "the agent's user has subuid and subgid ranges",
            )
        return RuntimeFinding(
            SUBUID,
            KIND_HOST,
            title,
            "broken",
            f"the agent's user on {label} has no range in {', '.join(missing)}, "
            "and rootless Podman cannot create a container without one",
            "On the node: sudo usermod --add-subuids 100000-165535 "
            "--add-subgids 100000-165535 <user>, then podman system migrate.",
        )


# ── Nothing answered ─────────────────────────────────────────────────────────


class UnknownRuntimeChecks:
    """A connected agent whose daemon answered nothing.

    Not "Docker is down": on a rootless Podman desktop the default socket is
    rootful Podman's, which the operator cannot read, and the useful finding
    is which socket *does* exist.
    """

    name = "unknown"

    def commands(self) -> dict[str, str]:
        return {"sockets": SOCKETS_COMMAND}

    def evaluate(
        self,
        label: str,
        facts: RuntimeFacts,
        outputs: dict[str, ProbeOutput | None],
    ) -> RuntimeReport:
        output = outputs.get("sockets")
        sockets = (
            [s.strip() for s in output.stdout.splitlines() if s.strip()]
            if _usable(output) and output is not None
            else []
        )
        podman = [s for s in sockets if s.endswith("/podman/podman.sock")]
        if podman:
            detail = (
                f"the agent on {label} reached no daemon, and a Podman socket "
                f"exists at {podman[0]}: nothing answered on it"
            )
            remedy = (
                "On the node, as the agent's user: systemctl --user enable --now "
                "podman.socket, then restart the agent"
            )
        elif sockets:
            detail = (
                f"the agent on {label} reached no daemon on "
                f"{', '.join(sockets)}: it is down, or not readable by the "
                "agent's user"
            )
            remedy = (
                "Start the daemon on that machine, or give the agent's user "
                "access to it; restarting it from here would kill whatever it "
                "is still running"
            )
        else:
            detail = (
                f"the agent on {label} reached no container daemon, and no "
                "Docker or Podman socket exists there"
            )
            remedy = (
                "Install Docker Engine, or Podman with podman.socket enabled "
                "for the agent's user"
            )
        daemon = RuntimeFinding(
            NO_RUNTIME,
            KIND_DAEMON,
            "Container runtime",
            "broken",
            detail,
            remedy,
            {"sockets": sockets},
        )
        gpu = RuntimeFinding(
            GPU_ACCESS,
            KIND_GPU,
            "GPU access",
            "broken",
            f"could not tell how a container reaches the GPU on {label}",
            "Fix the container runtime first.",
        )
        return RuntimeReport("unknown", [daemon, gpu], "")
