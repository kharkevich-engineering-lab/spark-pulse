"""Why is this node not working, and can it be fixed from here.

The doctor and the installer share their probes deliberately: everything the
installer measures to *choose* an install, the doctor measures to *explain*
one, and both call :func:`spark_pulse.agent.bootstrap_probe.probe_node`. There
is one probe layer and two callers, so a capability that is measured correctly
for an install is measured correctly for a diagnosis by construction.

**Two channels, and which one is used is itself a finding.** When the agent is
connected, the hub already knows its liveness, its version, its certificate
window and what its Docker daemon answered — no SSH, no login, nothing to
authenticate. When it is *not* connected, which is precisely when the doctor is
called, that channel is gone and SSH is the recovery path. This is the honest
reason the SSH transport survives past bootstrap: it is not an installer, it is
the way in when the thing it installed is down.

**Three outcomes, not two.** Every finding says which of these it is, because
collapsing the third into the second sends someone round in circles:

``fixable-here``
    The doctor can do it: restart a unit, re-enable lingering, fix a mode.
``needs-a-decision``
    Fixable, but not by a program. Re-enrolment destroys a node's identity, so
    it is never something the doctor does because something seemed stuck.
``needs-a-human-on-that-machine``
    A dead disk, a Docker daemon that will not start, a clock that is wrong.
    Real, common, and not reachable from here.

**Safe when nothing is wrong.** :func:`diagnose` runs read-only commands only.
:func:`treat` is the one that changes anything, it changes only what a finding
named, and it re-verifies afterwards. Both are idempotent, and running the
doctor against a healthy node makes no mutation at all — which is asserted in
the tests rather than promised here.

**The control node is not a special case.** It runs an ordinary agent enrolled
in the ordinary way, so it is diagnosed by this function with its own node id
like any peer, and the agent-channel checks work on it with no SSH at all.
"""

from __future__ import annotations

import logging
import shlex
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from spark_pulse.agent.bootstrap import (
    InstallPaths,
    NodeAccess,
    detect_installation,
    open_node_session,
    paths_for,
)
from spark_pulse.agent.bootstrap_probe import (
    NodeCapabilities,
    PrivilegedRunner,
    SudoDeclined,
    probe_node,
)
from spark_pulse.agent.bootstrap_transport import (
    BootstrapError,
    Connector,
    NodeSession,
    Prompt,
)
from spark_pulse.agent.hub import Liveness
from spark_pulse.agent.server import ControlPlaneServer
from spark_pulse.version import __version__

logger = logging.getLogger(__name__)

__all__ = [
    "DoctorReport",
    "Finding",
    "Repair",
    "REACHABILITY_PROBE",
    "diagnose",
    "treat",
]

#: What the doctor calls a finding it can act on, and what it calls one it
#: cannot. The third value is a real category, not a euphemism for the second.
FIXABLE = "fixable-here"
NEEDS_DECISION = "needs-a-decision"
NEEDS_HUMAN = "needs-a-human-on-that-machine"
NOTHING_TO_DO = "nothing-to-do"

#: A certificate inside this window is reported before it becomes an outage.
CERT_WARNING_SECONDS = 7 * 24 * 3600

#: Beyond this, a certificate a node was issued may be rejected by the node
#: itself. Certificates are backdated five minutes precisely so a small skew is
#: harmless; this is the point at which it stops being small.
CLOCK_SKEW_SECONDS = 120.0

#: Below this on the identity filesystem, an image pull will fail.
LOW_DISK_BYTES = 5 * 1024 * 1024 * 1024

#: Asked of the node's own python, so the answer is the node's view of the
#: control plane rather than ours of the node.
REACHABILITY_PROBE = (
    "python3 -c 'import socket,sys; "
    "socket.create_connection((sys.argv[1], int(sys.argv[2])), 3).close()'"
)


@dataclass(frozen=True)
class Finding:
    """One thing the doctor looked at."""

    check: str
    status: str  # ok | warn | broken | unknown
    detail: str
    #: Which channel established this. ``agent``, ``ssh``, or ``control-plane``.
    channel: str = "control-plane"
    verdict: str = NOTHING_TO_DO
    remedy: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class Repair:
    """Something the doctor did, or declined to do, and why."""

    check: str
    action: str
    applied: bool
    detail: str


@dataclass
class DoctorReport:
    """What is wrong with one node, and what was done about it."""

    node_id: str
    host: str = ""
    channels: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    repairs: list[Repair] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)

    def add(self, finding: Finding) -> Finding:
        self.findings.append(finding)
        return finding

    def get(self, check: str) -> Finding | None:
        for finding in self.findings:
            if finding.check == check:
                return finding
        return None

    @property
    def healthy(self) -> bool:
        return all(f.status in ("ok", "unknown") for f in self.findings)

    @property
    def problems(self) -> list[Finding]:
        return [f for f in self.findings if f.status in ("warn", "broken")]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["healthy"] = self.healthy
        return data


# ── Diagnosis ───────────────────────────────────────────────────────────────


async def diagnose(
    server: ControlPlaneServer,
    node_id: str,
    *,
    access: NodeAccess | None = None,
    connector: Connector | None = None,
) -> DoctorReport:
    """Look at a node and say what is wrong. Changes nothing.

    ``access`` is optional: without it only the checks the control plane and
    the agent channel can answer are run, and the host-level ones are recorded
    as ``unknown`` rather than guessed. That is the shape of the control node's
    own case too — it needs no SSH at all.
    """
    report = DoctorReport(node_id=node_id, host=access.host if access else "")
    entry = server.ledger.get(node_id)
    connected = server.hub.is_connected(node_id)

    if entry is None:
        report.add(
            Finding(
                "membership",
                "broken",
                f"{node_id} is not in this control plane's enrollment ledger",
                verdict=NEEDS_DECISION,
                remedy="enrol the node, or restore the ledger; nothing here can "
                "invent a membership",
            )
        )
    else:
        report.add(
            Finding(
                "membership",
                "ok",
                f"enrolled as {entry.name or node_id} in state {entry.state}",
            )
        )

    _check_connection(server, node_id, connected, report)
    if connected:
        report.channels.append("agent")
        _check_agent_facts(server, node_id, report)
    _check_certificate(entry, report)

    if access is None:
        for check in (
            "unit",
            "linger",
            "docker-socket",
            "identity",
            "reachability",
            "disk",
            "clock",
        ):
            report.add(
                Finding(
                    check,
                    "unknown",
                    "no SSH access was offered, so this was not looked at",
                    channel="ssh",
                )
            )
        return report

    report.channels.append("ssh")
    session = await open_node_session(server, access, connector=connector)
    try:
        caps = await probe_node(session, username=access.username)
        report.capabilities = caps.to_dict()
        paths = await detect_installation(session, caps) or paths_for("user", caps)
        await _check_host(server, session, caps, paths, report)
    finally:
        await session.close()
    return report


def _check_connection(
    server: ControlPlaneServer, node_id: str, connected: bool, report: DoctorReport
) -> None:
    if not connected:
        report.add(
            Finding(
                "agent-connection",
                "broken",
                "the agent is not connected to the control plane",
                channel="control-plane",
                verdict=FIXABLE,
                remedy="start the agent's unit on the node",
            )
        )
        return
    liveness = server.hub.liveness(node_id)
    if liveness is Liveness.HEALTHY:
        report.add(
            Finding("agent-connection", "ok", "connected and heartbeating", "agent")
        )
    else:
        report.add(
            Finding(
                "agent-connection",
                "warn" if liveness is Liveness.UNKNOWN else "broken",
                f"the stream is open but the agent is {liveness.value}: no recent "
                "heartbeat",
                channel="agent",
                verdict=FIXABLE,
                remedy="restart the agent's unit",
            )
        )


def _check_agent_facts(
    server: ControlPlaneServer, node_id: str, report: DoctorReport
) -> None:
    """Everything the hub already knows. No SSH, no login, no cost."""
    connection = server.hub.get(node_id)
    if connection is None:  # pragma: no cover - raced with a disconnect
        return
    snapshot = connection.snapshot()

    if snapshot.agent_version and snapshot.agent_version != __version__:
        report.add(
            Finding(
                "agent-version",
                "warn",
                f"the node runs agent {snapshot.agent_version}; this control "
                f"plane is {__version__}",
                channel="agent",
                verdict=FIXABLE,
                remedy="reinstall the agent to ship the current bundle; the node "
                "keeps its identity and rejoins as the same node",
            )
        )
    else:
        report.add(
            Finding(
                "agent-version",
                "ok",
                f"agent {snapshot.agent_version or __version__}",
                "agent",
            )
        )

    docker_version = snapshot.facts.docker_version
    if docker_version:
        report.add(Finding("docker-daemon", "ok", f"docker {docker_version}", "agent"))
    else:
        report.add(
            Finding(
                "docker-daemon",
                "broken",
                "the agent is connected and its Docker daemon answered nothing — "
                "the daemon is down or unreachable to it. This is not 'no such "
                "container': every container query on this node is unknown, not "
                "empty",
                channel="agent",
                verdict=NEEDS_HUMAN,
                remedy="a daemon that will not start is a job on that machine; "
                "restarting it from here would kill whatever it is still running",
            )
        )


def _check_certificate(entry: Any, report: DoctorReport) -> None:
    if entry is None or not entry.cert_not_after:
        report.add(
            Finding(
                "certificate",
                "unknown",
                "no certificate window is recorded for this node",
            )
        )
        return
    remaining = entry.cert_not_after - time.time()
    if remaining <= 0:
        report.add(
            Finding(
                "certificate",
                "broken",
                f"the node's certificate expired {int(-remaining) // 3600}h ago; "
                "renewal runs over the authenticated channel, so an agent that "
                "was off through its whole renewal window cannot renew itself",
                verdict=NEEDS_DECISION,
                remedy="re-enrol the node (remove_node_and_identity, then "
                "install) — that is destructive and is an operator's call, "
                "never the doctor's",
            )
        )
    elif remaining < CERT_WARNING_SECONDS:
        report.add(
            Finding(
                "certificate",
                "warn",
                f"the certificate expires in {int(remaining) // 3600}h and "
                "renewal has not happened",
                verdict=FIXABLE,
                remedy="restart the agent so its renewal loop runs",
            )
        )
    else:
        report.add(
            Finding(
                "certificate",
                "ok",
                f"valid for another {int(remaining) // 86400}d",
            )
        )


async def _check_host(
    server: ControlPlaneServer,
    session: NodeSession,
    caps: NodeCapabilities,
    paths: InstallPaths,
    report: DoctorReport,
) -> None:
    """The checks that need to be on the machine. All read-only."""
    unit = await session.run(
        f"{paths.systemctl} is-active {paths.unit_name}", timeout=20
    )
    enabled = await session.run(
        f"{paths.systemctl} is-enabled {paths.unit_name}", timeout=20
    )
    active = unit.stdout.strip() == "active"
    if active and enabled.ok:
        report.add(
            Finding("unit", "ok", f"{paths.unit_name} is active and enabled", "ssh")
        )
    else:
        report.add(
            Finding(
                "unit",
                "broken",
                f"{paths.unit_name} is {unit.stdout.strip() or 'not installed'} and "
                f"{enabled.stdout.strip() or 'not enabled'}",
                channel="ssh",
                verdict=FIXABLE,
                remedy=f"{paths.systemctl} enable --now {paths.unit_name}",
            )
        )

    if paths.scope == "user":
        if caps.linger:
            report.add(
                Finding("linger", "ok", f"lingering is on for {caps.user}", "ssh")
            )
        else:
            report.add(
                Finding(
                    "linger",
                    "broken" if caps.linger is False else "unknown",
                    f"lingering is off for {caps.user}, so the agent stops when "
                    "they log out and does not start at boot — the fault that "
                    "reads as 'it works until I close my laptop'",
                    channel="ssh",
                    verdict=FIXABLE,
                    remedy=f"loginctl enable-linger {caps.user}",
                )
            )
    else:
        report.add(
            Finding("linger", "ok", "a system unit does not depend on lingering", "ssh")
        )

    if caps.docker_socket:
        report.add(
            Finding(
                "docker-socket",
                "ok",
                f"reachable as {caps.user} (docker {caps.docker_version})",
                "ssh",
            )
        )
    elif "cannot connect to the docker daemon" in caps.docker_error.lower():
        report.add(
            Finding(
                "docker-socket",
                "broken",
                f"the Docker daemon is not answering on this node: {caps.docker_error[:160]}",
                channel="ssh",
                verdict=NEEDS_HUMAN,
                remedy="the daemon itself is down; restarting it from here would "
                "kill whatever it is still running",
            )
        )
    else:
        report.add(
            Finding(
                "docker-socket",
                "broken",
                f"{caps.user} cannot reach the Docker socket: {caps.docker_error[:160]}"
                + (
                    ""
                    if caps.in_docker_group
                    else f"; {caps.user} is not in the docker group"
                ),
                channel="ssh",
                verdict=FIXABLE,
                remedy=f"usermod -aG docker {caps.user}, then restart this user's "
                "systemd manager — a group added now is not in the current login",
            )
        )

    await _check_identity(server, session, paths, report)

    target = await _control_target(session, paths)
    host, _, port = target.rpartition(":")
    reach = await session.run(
        f"{REACHABILITY_PROBE} {shlex.quote(host or '127.0.0.1')} "
        f"{shlex.quote(port or str(server.session_port))}",
        timeout=20,
    )
    if reach.ok:
        report.add(
            Finding("reachability", "ok", "the node can reach the session port", "ssh")
        )
    else:
        report.add(
            Finding(
                "reachability",
                "broken",
                f"the node cannot open a connection to {target or 'the control plane'}"
                "; an agent here would redial forever",
                channel="ssh",
                verdict=NEEDS_HUMAN,
                remedy="a route, a firewall, or a fabric link — none of it is "
                "reachable from the control plane",
            )
        )

    disk = await session.run(f"df -Pk {shlex.quote(paths.identity_dir)}", timeout=20)
    free = _free_bytes(disk.stdout)
    if free is None:
        report.add(Finding("disk", "unknown", "df did not answer", "ssh"))
    elif free < LOW_DISK_BYTES:
        report.add(
            Finding(
                "disk",
                "warn",
                f"{free // (1024 ** 3)} GiB free where the agent lives; an image "
                "pull will fail long before the agent notices",
                channel="ssh",
                verdict=NEEDS_HUMAN,
                remedy="free space on that machine; the doctor does not delete "
                "anybody's data",
            )
        )
    else:
        report.add(Finding("disk", "ok", f"{free // (1024 ** 3)} GiB free", "ssh"))

    clock = await session.run("date +%s", timeout=20)
    if clock.ok and clock.stdout.strip().isdigit():
        skew = float(clock.stdout.strip()) - time.time()
        if abs(skew) > CLOCK_SKEW_SECONDS:
            report.add(
                Finding(
                    "clock",
                    "broken",
                    f"the node's clock is {int(skew)}s from the control plane's. "
                    "Certificates are backdated five minutes for exactly this "
                    "reason, and this is past that",
                    channel="ssh",
                    verdict=NEEDS_HUMAN,
                    remedy="fix time sync on that machine; setting another "
                    "machine's clock is not a repair",
                )
            )
        else:
            report.add(Finding("clock", "ok", f"within {int(abs(skew))}s", "ssh"))
    else:
        report.add(Finding("clock", "unknown", "date did not answer", "ssh"))


async def _check_identity(
    server: ControlPlaneServer,
    session: NodeSession,
    paths: InstallPaths,
    report: DoctorReport,
) -> None:
    """The directory, its modes, and whether its pin still matches ours."""
    import json

    listing = await session.run(
        f"cat {shlex.quote(paths.identity_dir)}/identity.json 2>/dev/null", timeout=20
    )
    if not listing.ok or not listing.stdout.strip():
        report.add(
            Finding(
                "identity",
                "broken",
                f"{paths.identity_dir} holds no readable identity",
                channel="ssh",
                verdict=NEEDS_DECISION,
                remedy="enrol the node; the doctor never mints an identity on its own",
            )
        )
        return
    try:
        identity = json.loads(listing.stdout)
    except json.JSONDecodeError:
        report.add(
            Finding(
                "identity",
                "broken",
                f"{paths.identity_dir}/identity.json is present but unparseable — "
                "half an identity, which is a failed install rather than a node "
                "that has never enrolled",
                channel="ssh",
                verdict=NEEDS_DECISION,
                remedy="a human decides whether to restore it or re-enrol; "
                "guessing here would orphan a uuid",
            )
        )
        return

    mode = await session.run(
        f"stat -c '%a' {shlex.quote(paths.identity_dir)}/node.key", timeout=20
    )
    bits = mode.stdout.strip()
    if bits and bits not in ("600", "400"):
        report.add(
            Finding(
                "identity-permissions",
                "warn",
                f"{paths.identity_dir}/node.key is mode {bits}; the node's private "
                "key is readable by more than its owner",
                channel="ssh",
                verdict=FIXABLE,
                remedy=f"chmod 600 {paths.identity_dir}/node.key",
            )
        )
    else:
        report.add(
            Finding(
                "identity-permissions", "ok", f"node.key is mode {bits or '600'}", "ssh"
            )
        )

    pin = str(identity.get("trust_bundle_pin") or "")
    if pin and pin != server.trust_bundle_pin:
        report.add(
            Finding(
                "trust-bundle",
                "broken",
                "the node pinned a trust bundle this control plane no longer has: "
                "the CA rotated while the node was away, so its certificate can "
                "never be renewed and a fresh bundle would be refused by the pin "
                "that exists to refuse it",
                channel="ssh",
                verdict=NEEDS_DECISION,
                remedy="re-enrol the node — destructive, and an operator's call",
            )
        )
    else:
        report.add(Finding("trust-bundle", "ok", "the pin matches this CA", "ssh"))
        bundle = await session.run(
            f"cat {shlex.quote(paths.identity_dir)}/ca.pem 2>/dev/null", timeout=20
        )
        if bundle.stdout.encode() != server.trust_bundle_pem:
            report.add(
                Finding(
                    "trust-bundle-file",
                    "warn",
                    "the pin matches but the bundle on disk does not — a truncated "
                    "or partly written ca.pem",
                    channel="ssh",
                    verdict=FIXABLE,
                    remedy="re-push the trust bundle; the pin makes that safe",
                )
            )
        else:
            report.add(Finding("trust-bundle-file", "ok", "ca.pem is intact", "ssh"))

    report.add(
        Finding(
            "identity",
            "ok",
            f"node {identity.get('node_id')} at {paths.identity_dir}",
            "ssh",
        )
    )


async def _control_target(session: NodeSession, paths: InstallPaths) -> str:
    """The address the node's own unit says to dial.

    Read from the unit rather than assumed from this process's configuration:
    "can this node reach its control plane" is only meaningful about the
    address the node is actually using.
    """
    unit = await session.run(
        f"cat {shlex.quote(paths.unit_path)} 2>/dev/null", timeout=20
    )
    for line in unit.stdout.splitlines():
        if line.startswith("ExecStart=") and "--control" in line:
            parts = line.split()
            index = parts.index("--control")
            if index + 1 < len(parts):
                return parts[index + 1]
    return ""


def _free_bytes(df_output: str) -> int | None:
    """``df -Pk``'s available column, in bytes. POSIX output is one line."""
    lines = [line for line in df_output.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    fields = lines[-1].split()
    try:
        return int(fields[3]) * 1024
    except (IndexError, ValueError):
        return None


# ── Treatment ───────────────────────────────────────────────────────────────


async def treat(
    server: ControlPlaneServer,
    node_id: str,
    *,
    access: NodeAccess,
    connector: Connector | None = None,
    sudo_password_prompt: Prompt | None = None,
    reverify: bool = True,
) -> DoctorReport:
    """Diagnose, repair what is safely repairable, and check again.

    Only findings whose verdict is ``fixable-here`` are acted on. A
    ``needs-a-decision`` finding is never attempted — re-enrolment destroys a
    node's identity and belongs to a person — and a
    ``needs-a-human-on-that-machine`` finding is reported without a doomed
    attempt, because a failed repair reads as a second fault.
    """
    report = await diagnose(server, node_id, access=access, connector=connector)
    actionable = [f for f in report.problems if f.verdict == FIXABLE]
    for finding in report.problems:
        if finding.verdict in (NEEDS_DECISION, NEEDS_HUMAN):
            report.repairs.append(
                Repair(
                    finding.check,
                    "none",
                    False,
                    f"not attempted ({finding.verdict}): {finding.remedy}",
                )
            )
    if not actionable:
        return report

    session = await open_node_session(server, access, connector=connector)
    runner: PrivilegedRunner | None = None
    try:
        caps = await probe_node(session, username=access.username)
        paths = await detect_installation(session, caps) or paths_for("user", caps)
        runner = PrivilegedRunner(session, caps, prompt=sudo_password_prompt)
        needs_elevation = any(
            f.check in ("linger", "docker-socket") for f in actionable
        )
        if (
            needs_elevation
            and sudo_password_prompt is not None
            and not caps.sudo.passwordless_all
        ):
            runner.offer_password(
                await sudo_password_prompt(f"sudo password for {caps.user}")
            )
        for finding in actionable:
            await _repair(server, session, runner, caps, paths, finding, report)
    finally:
        if runner is not None:
            runner.drop()
        await session.close()

    if reverify:
        fresh = await diagnose(server, node_id, access=access, connector=connector)
        fresh.repairs = report.repairs
        fresh.channels = report.channels
        return fresh
    return report


async def _repair(
    server: ControlPlaneServer,
    session: NodeSession,
    runner: PrivilegedRunner,
    caps: NodeCapabilities,
    paths: InstallPaths,
    finding: Finding,
    report: DoctorReport,
) -> None:
    """One named repair. Nothing here touches identity or discards state."""
    check = finding.check
    try:
        if check in ("unit", "agent-connection", "certificate", "agent-version"):
            if check == "agent-version":
                report.repairs.append(
                    Repair(
                        check,
                        "none",
                        False,
                        "shipping a new bundle is an install, not a repair; run "
                        "install_agent() against this node — it keeps the identity",
                    )
                )
                return
            action = f"{paths.systemctl} restart {paths.unit_name}"
            result = await session.run(action, timeout=60)
            if not result.ok:
                action = f"{paths.systemctl} enable --now {paths.unit_name}"
                result = await session.run(action, timeout=60)
            report.repairs.append(
                Repair(
                    check,
                    action,
                    result.ok,
                    "restarted the agent" if result.ok else result.stderr.strip()[:200],
                )
            )
            return

        if check == "linger":
            action = f"loginctl enable-linger {shlex.quote(caps.user)}"
            result = await runner.run(action, why=f"enable lingering for {caps.user}")
            report.repairs.append(
                Repair(
                    check,
                    action,
                    result.ok,
                    "lingering enabled" if result.ok else result.stderr.strip()[:200],
                )
            )
            return

        if check == "docker-socket":
            action = f"usermod -aG docker {shlex.quote(caps.user)}"
            result = await runner.run(
                action, why=f"add {caps.user} to the docker group"
            )
            report.repairs.append(
                Repair(
                    check,
                    action,
                    result.ok,
                    (
                        "added to the docker group; it takes effect on the next login, "
                        f"so `loginctl terminate-user {caps.user}` or a reboot is still "
                        "needed"
                        if result.ok
                        else result.stderr.strip()[:200]
                    ),
                )
            )
            return

        if check == "identity-permissions":
            action = f"chmod 600 {shlex.quote(paths.identity_dir)}/node.key"
            result = await session.run(action, timeout=20)
            report.repairs.append(
                Repair(check, action, result.ok, "tightened the private key's mode")
            )
            return

        if check == "trust-bundle-file":
            # Safe only because the *pin* matched: this replaces bytes that are
            # already trusted, and never introduces a new authority.
            await session.upload(
                server.trust_bundle_pem, f"{paths.identity_dir}/ca.pem", mode=0o644
            )
            report.repairs.append(
                Repair(check, "re-push ca.pem", True, "the pinned bundle was rewritten")
            )
            return
    except SudoDeclined as exc:
        report.repairs.append(Repair(check, finding.remedy, False, exc.reason))
        return
    except BootstrapError as exc:
        report.repairs.append(Repair(check, finding.remedy, False, str(exc)))
        return

    report.repairs.append(
        Repair(check, "none", False, "no repair is defined for this finding")
    )
