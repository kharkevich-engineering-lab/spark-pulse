"""Getting an agent onto a node, and giving it an identity. §3.1, in order.

SSH's only job here is bootstrap and recovery. Once an agent is enrolled every
container operation goes over the agent's own stream; SSH survives because it
is the channel that still works when the agent does not, which is what
:mod:`spark_pulse.agent.doctor` uses it for.

The sequence is §3.1's, and each step is here because skipping it costs
something specific:

1. **Reach the node and show its host key fingerprint.** Before any secret
   moves. The confirmed key is then pinned for the session that follows, so
   what the operator approved is what we talk to.
2. **A key, or a password — never a shared private key.** An operator-supplied
   key is used as-is. Otherwise the control plane generates one ed25519 pair,
   keeps the private half forever, and pushes only the public half. NVIDIA's
   ``discover-sparks`` copies one *private* key to every node, so one
   compromised Spark is a key to all of them; that is the single thing §3.1
   says not to carry forward.
3. **The password is used once, in process.** No ``sshpass``, no argv, no file,
   no log line. It is a Python string that exists for the length of the
   install.
4. **Install the public key, then prove passwordless SSH works** on a fresh
   connection carrying no password at all, before anything else is attempted.
5. **Probe, then choose the least-privilege install that works.** The default
   on this hardware turns out to need no root whatsoever: the login user is in
   ``docker`` and lingering is on, so a ``systemctl --user`` unit is a complete
   installation with zero ``sudo`` calls. Root is the unusual case, not the
   assumed one — and none of this is assumed, it is measured
   (:mod:`spark_pulse.agent.bootstrap_probe`).
6. **Detect an existing identity and converge, or refuse loudly.** k0s silently
   ignores the token when a config exists, which is why re-enrolment there
   needs a full reset. An identity this control plane already knows is kept and
   reused; one it does not is refused by name, with the two commands that
   resolve it.
7. **Mint, hand over, verify, invalidate.** A single-use ten-minute token
   scoped to this node's name, delivered as a 0600 file the node reads and the
   installer then shreds — never as an argument, where ``ps`` would show it.
   The enrolment listener is opened only for the duration of the install.

What the operator gives up is reported rather than silently accepted: a node
where lingering cannot be enabled still gets a working agent, and the report
says it will stop at logout instead of leaving someone to discover that.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from spark_pulse.agent.bootstrap_probe import (
    NodeCapabilities,
    PrivilegedRunner,
    SudoDeclined,
    probe_node,
)
from spark_pulse.agent.bootstrap_transport import (
    AsyncSSHConnector,
    BootstrapError,
    Connector,
    HostKey,
    HostKeyConfirm,
    HostKeyDeclined,
    KeyPair,
    NodeSession,
    Prompt,
    ROOT_PASSWORD_MESSAGE,
    RootPasswordBootstrap,
    generate_keypair,
    keypair_from_private_pem,
)
from spark_pulse.agent.bundle import VERIFY_COMMAND, AgentBundle, build_bundle
from spark_pulse.agent.server import ControlPlaneServer

logger = logging.getLogger(__name__)

__all__ = [
    "AgentInstallation",
    "Concession",
    "ExistingIdentity",
    "InstallPaths",
    "InstallReport",
    "NodeAccess",
    "control_plane_keypair",
    "detect_installation",
    "enrollment_window",
    "install_agent",
    "open_node_session",
    "paths_for",
    "reenroll_node",
    "remove_node_and_identity",
    "render_sudoers",
    "render_unit",
    "uninstall_agent_keep_identity",
]

#: The unit's name in both scopes. One name, so an operator debugging a node
#: types the same thing whichever way it was installed.
UNIT_NAME = "spark-pulse-agent.service"

#: How long to wait for the agent to appear in the hub after being started.
CONNECT_TIMEOUT = 90.0


class ExistingIdentity(BootstrapError):
    """The node already holds an identity, and it is not one we can converge on.

    Refusing here is the whole point. §3.1: "the installer must detect an
    existing identity and either converge or refuse loudly. k0s silently
    ignores the token when a config already exists, which is why re-enrollment
    there needs a full reset."
    """

    def __init__(self, message: str, *, node_id: str = "", directory: str = ""):
        super().__init__(message)
        self.node_id = node_id
        self.directory = directory


# ── What the install decided, and what it cost ──────────────────────────────


@dataclass(frozen=True)
class Concession:
    """A capability that could not be obtained, and what it costs the operator.

    Not an error. A node whose lingering cannot be enabled still gets a working
    agent; it just stops when that user logs out, and somebody has to be told
    so rather than discovering it a week later.
    """

    capability: str
    detail: str
    cost: str


@dataclass(frozen=True)
class InstallPaths:
    """Where everything lives, for one scope.

    The identity directory is deliberately *outside* the install root: §3.1's
    "uninstall, keep identity" is exactly "remove the install root and leave
    this alone", and making that true of the filesystem layout means it cannot
    be got wrong by an uninstall that deletes one directory too many.
    """

    scope: str
    install_root: str
    identity_dir: str
    unit_dir: str
    staging: str
    systemctl: str
    unit_name: str = UNIT_NAME

    @property
    def unit_path(self) -> str:
        return f"{self.unit_dir}/{self.unit_name}"

    @property
    def launcher(self) -> str:
        return f"{self.install_root}/current/bin/spark-pulse-agent"

    @property
    def wanted_by(self) -> str:
        return "default.target" if self.scope == "user" else "multi-user.target"


def paths_for(scope: str, caps: NodeCapabilities) -> InstallPaths:
    """The layout for ``scope`` on a node described by ``caps``."""
    if scope == "user":
        home = caps.home or f"/home/{caps.user}"
        return InstallPaths(
            scope="user",
            install_root=f"{home}/.local/share/spark-pulse/agent",
            identity_dir=f"{home}/.local/share/spark-pulse/agent-identity",
            unit_dir=f"{home}/.config/systemd/user",
            staging=f"{home}/.cache/spark-pulse/bootstrap",
            systemctl=f"XDG_RUNTIME_DIR=/run/user/{caps.uid} systemctl --user",
        )
    return InstallPaths(
        scope="system",
        install_root="/opt/spark-pulse/agent",
        identity_dir="/var/lib/spark-pulse/agent",
        unit_dir="/etc/systemd/system",
        staging="/tmp/spark-pulse-bootstrap",
        systemctl="systemctl",
    )


@dataclass
class InstallReport:
    """Everything an operator (or the UI) needs to see about one install.

    The probe results are in here on purpose. A node none of us has seen
    behaves in a way that has to be explainable from the report alone, and
    "why did it choose a system unit" is only answerable if what it measured is
    recorded next to what it decided.
    """

    host: str
    username: str
    name: str = ""
    node_id: str = ""
    scope: str = ""
    scope_reason: str = ""
    converged: bool = False
    connected: bool = False
    host_key_fingerprint: str = ""
    public_key_fingerprint: str = ""
    key_generated: bool = False
    used_password: bool = False
    capabilities: dict[str, Any] = field(default_factory=dict)
    privileged_calls: list[dict[str, Any]] = field(default_factory=list)
    concessions: list[Concession] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    bundle: dict[str, Any] = field(default_factory=dict)
    unit_path: str = ""
    identity_dir: str = ""

    def note(self, step: str) -> None:
        logger.info("%s: %s", self.host, step)
        self.steps.append(step)

    def concede(self, capability: str, detail: str, cost: str) -> None:
        logger.warning("%s: %s — %s", self.host, detail, cost)
        self.concessions.append(Concession(capability, detail, cost))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["concessions"] = [asdict(c) for c in self.concessions]
        return data


@dataclass(frozen=True)
class NodeAccess:
    """How to reach a node after it has been bootstrapped.

    ``private_key`` of ``None`` means the control plane's own key, which is
    what an install left behind in the node's ``authorized_keys``.
    """

    host: str
    username: str
    port: int = 22
    private_key: bytes | None = None
    host_key: HostKey | None = None


# ── The control plane's own key ─────────────────────────────────────────────


def control_plane_keypair(server: ControlPlaneServer) -> KeyPair:
    """The one keypair this control plane pushes, generated once and kept.

    One key, held here, never sent. Not one key *copied everywhere*, which is
    the arrangement §3.1 exists to avoid.
    """
    directory = server.directory / "bootstrap"
    private = directory / "id_ed25519"
    if private.exists():
        return keypair_from_private_pem(private.read_bytes())
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    pair = generate_keypair("spark-pulse-control-plane")
    # 0600 from the moment it exists, never afterwards.
    import os

    handle = os.open(str(private), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "wb") as fh:
        fh.write(pair.private_openssh)
    (directory / "id_ed25519.pub").write_text(pair.public_openssh + "\n")
    return pair


@asynccontextmanager
async def enrollment_window(server: ControlPlaneServer):
    """Open the bootstrap listener for the length of an install, then close it.

    An operator who does not want a token endpoint reachable at all times gets
    that for free: if the listener was already running it is left running, and
    if it was not, it is closed again on the way out.
    """
    already_open = server.enrollment_open
    if not already_open:
        await server.start_enrollment()
    try:
        yield server
    finally:
        if not already_open:
            await server.stop_enrollment(grace=0.5)


# ── Sessions ────────────────────────────────────────────────────────────────


async def open_node_session(
    server: ControlPlaneServer,
    access: NodeAccess,
    *,
    connector: Connector | None = None,
) -> NodeSession:
    """Connect to an already-bootstrapped node with the control plane's key."""
    connector = connector or AsyncSSHConnector()
    key = access.private_key or control_plane_keypair(server).private_openssh
    host_key = access.host_key
    if host_key is None:
        host_key = await connector.host_key(access.host, access.port)
    return await connector.connect(
        access.host,
        access.username,
        port=access.port,
        private_key=key,
        host_key=host_key,
    )


# ── Rendering ───────────────────────────────────────────────────────────────


def render_unit(
    paths: InstallPaths, control_target: str, *, node_name: str = ""
) -> str:
    """The systemd unit. ``Restart=`` is a backstop, not the mechanism.

    ``NodeAgent.run_forever`` already redials with backoff, so a control plane
    that restarts does not need systemd's help and a flapping unit would only
    hide that. ``Restart=on-failure`` is here for a process that actually died.

    ``RestartPreventExitStatus=2`` is the important line. Exit 2 from the agent
    means "already enrolled and handed a token" or "missing inputs" — a
    configuration error a restart cannot fix. Without this, systemd would loop
    on it forever and the operator would see a unit that is "activating"
    instead of a unit that has told them what is wrong.
    """
    description = "spark-pulse node agent"
    if node_name:
        description += f" ({node_name})"
    return f"""[Unit]
Description={description}
Documentation=https://github.com/kharkevich-engineering-lab/spark-pulse
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=SPARK_PULSE_AGENT_DIR={paths.identity_dir}
ExecStart={paths.launcher} --control {control_target}
# A backstop, not the reconnection mechanism: the agent redials on its own.
Restart=on-failure
RestartSec=5
# Exit 2 is a configuration error the agent has already explained. Looping on
# it would replace that explanation with a unit stuck in 'activating'.
RestartPreventExitStatus=2
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy={paths.wanted_by}
"""


def render_sudoers(user: str, *, unit: str = UNIT_NAME) -> str:
    """A drop-in covering three verbs on one unit. Offered, never required.

    Only useful for a *system* install: a user unit needs no elevation to
    start or stop at all, which is why the user scope is preferred.
    """
    verbs = ", ".join(
        f"/usr/bin/systemctl {verb} {unit}" for verb in ("start", "stop", "restart")
    )
    return (
        "# Installed by spark-pulse. Covers start/stop/restart of one unit and\n"
        "# nothing else — no shell, no arbitrary systemctl, no other unit.\n"
        f"{user} ALL=(root) NOPASSWD: {verbs}\n"
    )


# ── The install ─────────────────────────────────────────────────────────────


@dataclass
class AgentInstallation:
    """A node's installed agent, as the installer left it."""

    paths: InstallPaths
    node_id: str
    bundle_name: str


async def install_agent(
    server: ControlPlaneServer,
    *,
    host: str,
    username: str,
    control_host: str,
    name: str = "",
    port: int = 22,
    connector: Connector | None = None,
    private_key: bytes | None = None,
    confirm_host_key: HostKeyConfirm | None = None,
    password_prompt: Prompt | None = None,
    sudo_password_prompt: Prompt | None = None,
    scope: str = "auto",
    bundle: AgentBundle | None = None,
    include_runtime: bool = True,
    offer_sudoers: bool = False,
    connect_timeout: float = CONNECT_TIMEOUT,
) -> InstallReport:
    """Install, enrol and start an agent on ``host``. §3.1 end to end.

    ``scope`` is ``"auto"`` (prefer a rootless user unit), ``"user"`` or
    ``"system"``. An explicit scope is never quietly downgraded: if it cannot
    be had, the call fails naming the capability that is missing.
    """
    connector = connector or AsyncSSHConnector()
    report = InstallReport(host=host, username=username, name=name or host)

    host_key = await connector.host_key(host, port)
    report.host_key_fingerprint = host_key.fingerprint
    report.note(f"reached {host}:{port}, host key {host_key.fingerprint}")
    if confirm_host_key is not None and not await confirm_host_key(host_key):
        raise HostKeyDeclined(
            f"the host key for {host}:{port} ({host_key.fingerprint}) was not "
            "confirmed; nothing was sent"
        )

    password: str | None = None
    if private_key is not None:
        keypair = keypair_from_private_pem(private_key)
        report.note("using the SSH key supplied by the operator")
    else:
        keypair = control_plane_keypair(server)
        report.key_generated = True
        if username == "root":
            # Detected before a password is even asked for. The node would
            # refuse it, and the refusal is indistinguishable from a wrong
            # password unless something says which it is.
            raise RootPasswordBootstrap(ROOT_PASSWORD_MESSAGE.format(host=host))
        if password_prompt is None:
            raise BootstrapError(
                f"no SSH key was supplied for {username}@{host} and there is no "
                "way to ask for a password"
            )
        password = await password_prompt(f"password for {username}@{host}")
        if password is None:
            raise BootstrapError(
                f"no SSH key and no password for {username}@{host}; nothing to do"
            )
        report.used_password = True
    report.public_key_fingerprint = keypair.fingerprint

    session = await connector.connect(
        host,
        username,
        port=port,
        password=password,
        private_key=None if password is not None else keypair.private_openssh,
        host_key=host_key,
    )
    try:
        if password is not None:
            await _install_public_key(session, keypair)
            report.note("installed the control plane's public key")
            await session.close()
            # A *fresh* connection with the key and no password at all. §3.1
            # step 4: verify passwordless SSH works before going further, so a
            # failure here is a failure now rather than after the node has been
            # changed and the password is gone.
            session = await connector.connect(
                host,
                username,
                port=port,
                private_key=keypair.private_openssh,
                host_key=host_key,
            )
            report.note("verified passwordless SSH")

        return await _install_over(
            server,
            session,
            report=report,
            host=host,
            username=username,
            control_host=control_host,
            name=name or host,
            login_password=password,
            sudo_password_prompt=sudo_password_prompt,
            scope=scope,
            bundle=bundle,
            include_runtime=include_runtime,
            offer_sudoers=offer_sudoers,
            connect_timeout=connect_timeout,
        )
    finally:
        # The password's whole life is this function.
        password = None
        await session.close()


async def _install_over(
    server: ControlPlaneServer,
    session: NodeSession,
    *,
    report: InstallReport,
    host: str,
    username: str,
    control_host: str,
    name: str,
    login_password: str | None,
    sudo_password_prompt: Prompt | None,
    scope: str,
    bundle: AgentBundle | None,
    include_runtime: bool,
    offer_sudoers: bool,
    connect_timeout: float,
) -> InstallReport:
    caps = await probe_node(session, username=username)
    report.capabilities = caps.to_dict()
    report.note(
        f"probed {caps.user}@{caps.hostname or host}: docker="
        f"{'yes' if caps.docker_socket else 'no'} linger={caps.linger} "
        f"user-manager={'yes' if caps.user_manager else 'no'} "
        f"sudo={caps.sudo.detail}"
    )

    chosen, reason = _choose_scope(caps, scope)
    paths = paths_for(chosen, caps)
    report.scope = chosen
    report.scope_reason = reason
    report.identity_dir = paths.identity_dir
    report.unit_path = paths.unit_path
    report.note(f"installing a {chosen} unit: {reason}")

    needs = _privileged_needs(caps, paths)
    runner = PrivilegedRunner(session, caps, password=None, prompt=sudo_password_prompt)
    if needs and not all(runner.can(command) for _, command in needs):
        # Ask once, and only when something actually needs it. A node that
        # needs nothing elevated is never asked for a password at all.
        supplied = login_password
        if supplied is None and sudo_password_prompt is not None:
            supplied = await sudo_password_prompt(
                f"sudo password for {username}@{host} "
                f"(needed to: {'; '.join(why for why, _ in needs)})"
            )
        runner.offer_password(supplied)

    try:
        await _apply_capability_repairs(session, caps, runner, paths, report)
        installation = await _place_agent(
            server,
            session,
            runner,
            caps=caps,
            paths=paths,
            report=report,
            control_host=control_host,
            name=name,
            bundle=bundle,
            include_runtime=include_runtime,
            offer_sudoers=offer_sudoers,
        )
        report.node_id = installation.node_id
        report.connected = await _wait_connected(
            server, installation.node_id, connect_timeout
        )
        if report.connected:
            report.note(f"{installation.node_id} is connected")
        else:
            report.concede(
                "dial-home",
                f"the agent was installed and started but {installation.node_id} "
                "has not appeared in the hub",
                "check that the node can reach "
                f"{control_host}:{server.session_port}, and read "
                f"`{paths.systemctl} status {paths.unit_name}` on the node",
            )
        return report
    finally:
        runner.drop()
        report.privileged_calls = [asdict(call) for call in runner.calls]


def _choose_scope(caps: NodeCapabilities, requested: str) -> tuple[str, str]:
    """The least-privilege install that works, or a named refusal.

    Preference order, from §3.1's spirit rather than its letter: a user unit
    needing no root at all; a user unit plus one privileged call to enable
    lingering; a system unit. An explicitly requested scope is never silently
    downgraded.
    """
    if requested not in ("auto", "user", "system"):
        raise BootstrapError(f"unknown install scope {requested!r}")

    user_possible = caps.user_manager and not caps.is_root
    system_possible = caps.system_manager and (
        caps.is_root or caps.sudo.permitted and not caps.sudo.requiretty
    )

    if requested == "user":
        if not user_possible:
            raise BootstrapError(
                "a user unit was asked for, but "
                + (
                    "the login user is root, which has no user manager to run it"
                    if caps.is_root
                    else "no systemd --user manager is answering on this node"
                )
                + "; nothing was installed"
            )
        return "user", _user_scope_reason(caps)
    if requested == "system":
        if not system_possible:
            raise BootstrapError(
                "a system unit was asked for, but "
                + (
                    "the system manager is not answering"
                    if not caps.system_manager
                    else caps.sudo.detail or "this user cannot elevate"
                )
                + "; nothing was installed"
            )
        return "system", "the operator asked for a system unit"

    if user_possible:
        return "user", _user_scope_reason(caps)
    if system_possible:
        return "system", (
            "the login user is root"
            if caps.is_root
            else "no systemd --user manager is answering, so a system unit it is"
        )
    raise BootstrapError(
        "this node can host neither a user unit (no systemd --user manager) nor "
        "a system unit ("
        + (caps.sudo.detail or "no way to elevate")
        + "); nothing was installed"
    )


def _user_scope_reason(caps: NodeCapabilities) -> str:
    if caps.linger:
        return (
            "a rootless user unit; lingering is already on, so no elevation is needed"
        )
    return "a rootless user unit; lingering is off and will be enabled if possible"


def _privileged_needs(
    caps: NodeCapabilities, paths: InstallPaths
) -> list[tuple[str, str]]:
    """Exactly what will need elevating, named, before anything is asked for."""
    needs: list[tuple[str, str]] = []
    if paths.scope == "user" and caps.linger is not True:
        needs.append(
            (f"enable lingering for {caps.user}", f"loginctl enable-linger {caps.user}")
        )
    if not caps.docker_socket:
        needs.append(
            (f"add {caps.user} to the docker group", f"usermod -aG docker {caps.user}")
        )
    if paths.scope == "system":
        needs.append(("write and start a system unit", "systemctl daemon-reload"))
    return needs


async def _apply_capability_repairs(
    session: NodeSession,
    caps: NodeCapabilities,
    runner: PrivilegedRunner,
    paths: InstallPaths,
    report: InstallReport,
) -> None:
    """Obtain what is missing where we can; record what we could not."""
    if paths.scope == "user" and caps.linger is not True:
        command = f"loginctl enable-linger {shlex.quote(caps.user)}"
        try:
            result = await runner.run(
                command, why=f"enable lingering for {caps.user}", timeout=30
            )
        except SudoDeclined as exc:
            report.concede(
                "linger",
                f"lingering is off for {caps.user} and could not be enabled: {exc.reason}",
                f"the agent will stop when {caps.user} logs out, and will not "
                "start at boot; it works while a session is open",
            )
        else:
            if result.ok:
                report.note(f"enabled lingering for {caps.user}")
            else:
                report.concede(
                    "linger",
                    f"loginctl enable-linger failed: {result.stderr.strip()[:200]}",
                    f"the agent will stop when {caps.user} logs out",
                )

    if not caps.docker_socket:
        detail = caps.docker_error or "docker version did not answer"
        command = f"usermod -aG docker {shlex.quote(caps.user)}"
        if runner.can(command):
            try:
                result = await runner.run(
                    command, why=f"add {caps.user} to the docker group", timeout=30
                )
            except SudoDeclined as exc:  # pragma: no cover - can() said yes
                result = None
                detail = f"{detail}; {exc.reason}"
            if result is not None and result.ok:
                report.concede(
                    "docker",
                    f"{caps.user} was not able to reach the Docker socket ({detail}) "
                    "and has been added to the docker group",
                    "the group takes effect on the next login, so the agent "
                    "cannot run containers until this user's systemd manager is "
                    f"restarted — `loginctl terminate-user {caps.user}` or a reboot",
                )
            else:
                report.concede(
                    "docker",
                    f"{caps.user} cannot reach the Docker socket: {detail}",
                    "the agent will enrol and stay connected, but every container "
                    "operation on this node will fail",
                )
        else:
            report.concede(
                "docker",
                f"{caps.user} cannot reach the Docker socket ({detail}) and "
                f"cannot be added to the docker group: {runner.why_not(command)}",
                "the agent will enrol and stay connected, but every container "
                "operation on this node will fail",
            )


# ── Placing the agent ───────────────────────────────────────────────────────


async def _place_agent(
    server: ControlPlaneServer,
    session: NodeSession,
    runner: PrivilegedRunner,
    *,
    caps: NodeCapabilities,
    paths: InstallPaths,
    report: InstallReport,
    control_host: str,
    name: str,
    bundle: AgentBundle | None,
    include_runtime: bool,
    offer_sudoers: bool,
) -> AgentInstallation:
    existing = await _read_identity(session, paths.identity_dir)
    converge_on: str = ""
    if existing is not None:
        converge_on = _converge_or_refuse(server, existing, paths, report)

    bundle = bundle or build_bundle(
        include_runtime=include_runtime, cache_dir=server.directory / "bundles"
    )
    report.bundle = {
        "version": bundle.version,
        "name": bundle.name,
        "size": bundle.size,
        "includes_runtime": bundle.includes_runtime,
        "missing_modules": list(bundle.missing_modules),
    }
    await _ship_bundle(session, runner, paths, bundle, report)

    if converge_on:
        node_id = converge_on
    else:
        node_id = await _enrol(
            server,
            session,
            paths=paths,
            report=report,
            control_host=control_host,
            name=name,
        )

    await _write_unit(session, runner, paths, control_host, server, name, report)
    if offer_sudoers and paths.scope == "system":
        await _write_sudoers(session, runner, paths, caps, report)
    await _start_unit(session, runner, paths, report)
    return AgentInstallation(paths=paths, node_id=node_id, bundle_name=bundle.name)


def _converge_or_refuse(
    server: ControlPlaneServer,
    existing: dict[str, Any],
    paths: InstallPaths,
    report: InstallReport,
) -> str:
    """Reuse an identity we know, or refuse by name. Never silently ignore it."""
    node_id = str(existing.get("node_id") or "")
    pin = str(existing.get("trust_bundle_pin") or "")
    known = server.ledger.get(node_id) if node_id else None
    if known is not None and (not pin or pin == server.trust_bundle_pin):
        report.converged = True
        report.note(
            f"this node is already enrolled as {node_id}; keeping that identity "
            "and reinstalling the agent around it"
        )
        return node_id
    if known is None:
        raise ExistingIdentity(
            f"{paths.identity_dir} already holds an identity ({node_id or 'unreadable'}) "
            "that this control plane has no record of — a node from another "
            "cluster, or one whose control-plane state was rebuilt. Honouring a "
            "token now would mint a second uuid and orphan the first. Either "
            "remove_node_and_identity() to wipe it and join fresh, or restore "
            "the control plane's enrollment ledger.",
            node_id=node_id,
            directory=paths.identity_dir,
        )
    raise ExistingIdentity(
        f"{paths.identity_dir} holds identity {node_id}, which is enrolled here "
        "but pinned to a different trust bundle — the CA was replaced while this "
        "node was away. Its certificate can no longer be renewed. Use "
        "reenroll_node() to wipe the identity and join again.",
        node_id=node_id,
        directory=paths.identity_dir,
    )


async def _ship_bundle(
    session: NodeSession,
    runner: PrivilegedRunner,
    paths: InstallPaths,
    bundle: AgentBundle,
    report: InstallReport,
) -> None:
    target = f"{paths.install_root}/{bundle.name}"
    # Staging is always the login user's own: uploads go over SFTP as that
    # user, and a root-owned staging directory would make them fail in a way
    # that reads as a transport error rather than a permissions one.
    staged = await session.run(
        f"mkdir -p -m 0700 {shlex.quote(paths.staging)}", timeout=30
    )
    if not staged.ok:
        raise BootstrapError(
            f"could not create {paths.staging}: {staged.stderr.strip()[:200]}"
        )
    await _mkdir(session, runner, paths, [target], mode=0o755)
    remote_tar = f"{paths.staging}/{bundle.name}.tar.gz"
    await session.upload(bundle.data, remote_tar, mode=0o600)
    extract = await _run_scoped(
        session,
        runner,
        paths,
        f"tar -xzf {shlex.quote(remote_tar)} -C {shlex.quote(target)}",
        why="unpack the agent bundle",
        timeout=180,
    )
    if not extract.ok:
        raise BootstrapError(
            f"unpacking the agent bundle failed: {extract.stderr.strip()[:400]}"
        )
    await _run_scoped(
        session,
        runner,
        paths,
        f"ln -sfn {shlex.quote(bundle.name)} {shlex.quote(paths.install_root)}/current",
        why="point 'current' at the new bundle",
    )
    report.note(f"unpacked {bundle.name} ({bundle.size} bytes) into {target}")

    verify = await _run_scoped(
        session,
        runner,
        paths,
        f"{shlex.quote(paths.launcher)} {VERIFY_COMMAND}",
        why="verify the bundle imports on this node",
        timeout=120,
    )
    if not verify.ok:
        raise BootstrapError(
            "the agent bundle does not import on this node — most often a "
            "grpcio or cryptography extension built for a different CPython. "
            "Nothing has been started. The node said: "
            f"{(verify.stderr or verify.stdout).strip()[:400]}"
        )
    report.note("the bundle imports cleanly on the node")


async def _enrol(
    server: ControlPlaneServer,
    session: NodeSession,
    *,
    paths: InstallPaths,
    report: InstallReport,
    control_host: str,
    name: str,
) -> str:
    """Mint, deliver, redeem, shred, invalidate. Steps 5, 7 and 8 of §3.1."""
    token = server.mint_token(name)
    token_file = f"{paths.staging}/token"
    bundle_file = f"{paths.staging}/ca.pem"
    try:
        async with enrollment_window(server):
            # A file, not an argument. `ps` on the node would show an argv.
            await session.upload(f"{token}\n".encode(), token_file, mode=0o600)
            await session.upload(server.trust_bundle_pem, bundle_file, mode=0o644)
            result = await session.run(
                " ".join(
                    [
                        shlex.quote(paths.launcher),
                        "--enroll-only",
                        "--control",
                        shlex.quote(f"{control_host}:{server.session_port}"),
                        "--enroll-target",
                        shlex.quote(f"{control_host}:{server.enrollment_port}"),
                        "--token-file",
                        shlex.quote(token_file),
                        "--trust-bundle",
                        shlex.quote(bundle_file),
                        "--pin",
                        shlex.quote(server.trust_bundle_pin),
                        "--dir",
                        shlex.quote(paths.identity_dir),
                        "--name",
                        shlex.quote(name),
                    ]
                ),
                timeout=120,
            )
    finally:
        # Whatever happened, the token stops being usable and stops existing on
        # the node. §3.1 step 8, and it runs on the failure path too.
        await session.run(
            f"shred -u {shlex.quote(token_file)} 2>/dev/null || "
            f"rm -f {shlex.quote(token_file)}",
            timeout=30,
        )
        server.revoke_token(token)

    if result.returncode == 2:
        # The agent's own loud refusal, surfaced rather than retried.
        raise ExistingIdentity(
            f"the agent on {report.host} refused to enrol: "
            f"{(result.stderr or result.stdout).strip()[:400]}",
            directory=paths.identity_dir,
        )
    if not result.ok:
        raise BootstrapError(
            f"enrolment failed on {report.host}: "
            f"{(result.stderr or result.stdout).strip()[:400]}"
        )
    identity = await _read_identity(session, paths.identity_dir)
    node_id = str((identity or {}).get("node_id") or "")
    if not node_id:
        raise BootstrapError(
            "enrolment reported success but wrote no identity to "
            f"{paths.identity_dir}"
        )
    report.note(f"enrolled as {node_id}")
    return node_id


async def _write_unit(
    session: NodeSession,
    runner: PrivilegedRunner,
    paths: InstallPaths,
    control_host: str,
    server: ControlPlaneServer,
    name: str,
    report: InstallReport,
) -> None:
    unit = render_unit(paths, f"{control_host}:{server.session_port}", node_name=name)
    await _mkdir(session, runner, paths, [paths.unit_dir], mode=0o755)
    if paths.scope == "user":
        await session.upload(unit.encode(), paths.unit_path, mode=0o644)
    else:
        staged = f"{paths.staging}/{paths.unit_name}"
        await session.upload(unit.encode(), staged, mode=0o644)
        await runner.run(
            f"install -m 0644 {shlex.quote(staged)} {shlex.quote(paths.unit_path)}",
            why="write the system unit",
        )
    report.note(f"wrote {paths.unit_path}")


async def _write_sudoers(
    session: NodeSession,
    runner: PrivilegedRunner,
    paths: InstallPaths,
    caps: NodeCapabilities,
    report: InstallReport,
) -> None:
    """Offer the narrow drop-in. Staged as a file, checked, then installed.

    Never a remote ``echo`` and never an argument: the content is uploaded to
    staging as the login user, ``visudo -cf`` is asked whether it parses, and
    only then is it moved into place. A sudoers file that does not parse locks
    the operator out of sudo entirely, so the check is not optional.
    """
    path = "/etc/sudoers.d/spark-pulse-agent"
    staged = f"{paths.staging}/sudoers"
    await session.upload(render_sudoers(caps.user).encode(), staged, mode=0o600)
    check = await session.run(f"visudo -cf {shlex.quote(staged)}", timeout=30)
    if not check.ok:
        report.concede(
            "sudoers",
            f"the sudoers drop-in did not parse: {(check.stderr or check.stdout).strip()[:200]}",
            "restarting the agent will keep asking for a password",
        )
        return
    try:
        result = await runner.run(
            f"install -m 0440 -o root -g root {shlex.quote(staged)} {shlex.quote(path)}",
            why="install the scoped sudoers drop-in",
        )
    except SudoDeclined as exc:
        report.concede(
            "sudoers",
            f"the scoped sudoers drop-in was not installed: {exc.reason}",
            "restarting the agent will keep asking for a password",
        )
        return
    if result.ok:
        report.note(f"installed {path} (systemctl start/stop/restart of one unit)")
    else:  # pragma: no cover - defensive
        report.concede(
            "sudoers",
            f"writing {path} failed: {result.stderr.strip()[:200]}",
            "restarting the agent will keep asking for a password",
        )


async def _start_unit(
    session: NodeSession,
    runner: PrivilegedRunner,
    paths: InstallPaths,
    report: InstallReport,
) -> None:
    await _run_scoped(
        session, runner, paths, f"{paths.systemctl} daemon-reload", why="reload systemd"
    )
    result = await _run_scoped(
        session,
        runner,
        paths,
        f"{paths.systemctl} enable --now {paths.unit_name}",
        why="enable and start the agent",
        timeout=60,
    )
    if not result.ok:
        raise BootstrapError(
            f"starting {paths.unit_name} failed: "
            f"{(result.stderr or result.stdout).strip()[:400]}"
        )
    report.note(f"started {paths.unit_name}")


# ── Uninstall, remove, re-enrol ─────────────────────────────────────────────


async def uninstall_agent_keep_identity(
    server: ControlPlaneServer,
    access: NodeAccess,
    *,
    connector: Connector | None = None,
    sudo_password_prompt: Prompt | None = None,
) -> InstallReport:
    """Stop and remove the agent, **keeping** the node's identity.

    The node stays a member of the cluster. Installing again rejoins as the
    same uuid with the same certificate, because :class:`InstallPaths` puts the
    identity directory outside the install root and this function does not
    touch it. k3s's uninstall does the same and that asymmetry is why reinstall
    works there; the difference here is that it has a name and a sibling.

    The sibling is :func:`remove_node_and_identity`, which does not keep it.
    """
    return await _tear_down(
        server,
        access,
        connector=connector,
        sudo_password_prompt=sudo_password_prompt,
        destroy_identity=False,
    )


async def remove_node_and_identity(
    server: ControlPlaneServer,
    access: NodeAccess,
    *,
    connector: Connector | None = None,
    sudo_password_prompt: Prompt | None = None,
) -> InstallReport:
    """Stop and remove the agent **and wipe its identity**. Re-enrolment required.

    This is §3.1's *Remove*, and it is destructive: the node's uuid, key and
    certificate are gone, the control plane forgets it, and installing again
    produces a *different* node. Use :func:`uninstall_agent_keep_identity` when
    the node is coming back.
    """
    return await _tear_down(
        server,
        access,
        connector=connector,
        sudo_password_prompt=sudo_password_prompt,
        destroy_identity=True,
    )


async def _tear_down(
    server: ControlPlaneServer,
    access: NodeAccess,
    *,
    connector: Connector | None,
    sudo_password_prompt: Prompt | None,
    destroy_identity: bool,
) -> InstallReport:
    session = await open_node_session(server, access, connector=connector)
    report = InstallReport(host=access.host, username=access.username)
    try:
        caps = await probe_node(session, username=access.username)
        report.capabilities = caps.to_dict()
        paths = await detect_installation(session, caps)
        if paths is None:
            report.note("no spark-pulse agent unit found on this node")
            return report
        report.scope = paths.scope
        report.identity_dir = paths.identity_dir
        report.unit_path = paths.unit_path

        runner = PrivilegedRunner(session, caps)
        if paths.scope == "system" and not runner.can("true"):
            supplied = (
                await sudo_password_prompt(f"sudo password for {access.username}")
                if sudo_password_prompt is not None
                else None
            )
            runner.offer_password(supplied)

        identity = await _read_identity(session, paths.identity_dir)
        node_id = str((identity or {}).get("node_id") or "")
        report.node_id = node_id

        await _run_scoped(
            session,
            runner,
            paths,
            f"{paths.systemctl} disable --now {paths.unit_name}",
            why="stop and disable the agent",
            timeout=60,
        )
        await _run_scoped(
            session,
            runner,
            paths,
            f"rm -f {shlex.quote(paths.unit_path)}",
            why="remove the unit file",
        )
        await _run_scoped(
            session,
            runner,
            paths,
            f"{paths.systemctl} daemon-reload",
            why="reload systemd",
        )
        await _run_scoped(
            session,
            runner,
            paths,
            f"rm -rf {shlex.quote(paths.install_root)} {shlex.quote(paths.staging)}",
            why="remove the agent bundle",
        )
        report.note(f"removed {paths.install_root} and {paths.unit_path}")

        if destroy_identity:
            await _run_scoped(
                session,
                runner,
                paths,
                f"rm -rf {shlex.quote(paths.identity_dir)}",
                why="wipe the node identity",
            )
            if node_id:
                server.ledger.remove(node_id)
            report.note(
                f"wiped {paths.identity_dir}"
                + (f" and forgot {node_id}" if node_id else "")
                + "; this node must be enrolled again to rejoin"
            )
        else:
            report.note(
                f"kept {paths.identity_dir}; installing again rejoins as "
                f"{node_id or 'the same node'}"
            )
        report.privileged_calls = [asdict(call) for call in runner.calls]
        return report
    finally:
        await session.close()


async def reenroll_node(
    server: ControlPlaneServer,
    access: NodeAccess,
    *,
    control_host: str,
    name: str = "",
    connector: Connector | None = None,
    **install_kwargs: Any,
) -> InstallReport:
    """*Remove*, then join: a new uuid for a node that must start over.

    Deliberately not something any other function does implicitly. Everything
    the node was — its uuid, its certificate, its place in the ledger — is
    destroyed, and the cluster sees a new machine afterwards. Call this only
    when that is the intention.
    """
    await remove_node_and_identity(server, access, connector=connector)
    return await install_agent(
        server,
        host=access.host,
        username=access.username,
        control_host=control_host,
        name=name or access.host,
        port=access.port,
        connector=connector,
        private_key=access.private_key or control_plane_keypair(server).private_openssh,
        **install_kwargs,
    )


# ── Small remote helpers ────────────────────────────────────────────────────


async def _install_public_key(session: NodeSession, keypair: KeyPair) -> None:
    """Append the public half to ``authorized_keys``, idempotently.

    Four plain commands rather than one clever one. The key travels as an
    uploaded file, not as an argument — an argument is visible in ``ps`` to
    every user on the node — and the ``grep -qxF -f`` makes a second install a
    no-op rather than a growing file.
    """
    home = (await session.run("printf '%s\\n' \"$HOME\"", timeout=20)).stdout.strip()
    if not home:
        raise BootstrapError("the node did not report a home directory")
    staged = f"{home}/.ssh/spark-pulse-control-plane.pub"
    authorized = f"{home}/.ssh/authorized_keys"

    made = await session.run(f"mkdir -p -m 0700 {shlex.quote(home)}/.ssh", timeout=20)
    if not made.ok:
        raise BootstrapError(
            f"could not create {home}/.ssh: {made.stderr.strip()[:200]}"
        )
    await session.upload(f"{keypair.public_openssh}\n".encode(), staged, mode=0o644)
    await session.run(
        f"touch {shlex.quote(authorized)} && chmod 600 {shlex.quote(authorized)}",
        timeout=20,
    )
    result = await session.run(
        f"grep -qxF -f {shlex.quote(staged)} {shlex.quote(authorized)} || "
        f"cat {shlex.quote(staged)} >> {shlex.quote(authorized)}",
        timeout=20,
    )
    if not result.ok:
        raise BootstrapError(
            f"could not install the public key: {result.stderr.strip()[:200]}"
        )


async def _read_identity(
    session: NodeSession, identity_dir: str
) -> dict[str, Any] | None:
    """The node's ``identity.json``, or ``None`` if it has never enrolled."""
    result = await session.run(
        f"cat {shlex.quote(identity_dir)}/identity.json 2>/dev/null", timeout=30
    )
    if not result.ok or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise BootstrapError(
            f"{identity_dir}/identity.json is present but unreadable; this node "
            "holds half an identity and needs a human before anything is "
            "installed over it"
        )


async def detect_installation(
    session: NodeSession, caps: NodeCapabilities
) -> InstallPaths | None:
    """Which scope this node was installed with, by looking rather than asking."""
    for scope in ("user", "system"):
        paths = paths_for(scope, caps)
        found = await session.run(
            f"test -e {shlex.quote(paths.unit_path)} || "
            f"test -e {shlex.quote(paths.install_root)}",
            timeout=20,
        )
        if found.ok:
            return paths
    return None


async def _mkdir(
    session: NodeSession,
    runner: PrivilegedRunner,
    paths: InstallPaths,
    directories: list[str],
    *,
    mode: int,
) -> None:
    quoted = " ".join(shlex.quote(d) for d in directories)
    await _run_scoped(
        session,
        runner,
        paths,
        f"mkdir -p -m {mode:04o} {quoted}",
        why="create the agent directories",
    )


async def _run_scoped(
    session: NodeSession,
    runner: PrivilegedRunner,
    paths: InstallPaths,
    command: str,
    *,
    why: str,
    timeout: float = 60.0,
):
    """Run a command with elevation only when the scope actually needs it.

    A user-scope install runs everything as the login user, which is the whole
    reason it is preferred: there is nothing here for a sudo prompt to attach
    to.
    """
    if paths.scope == "user":
        return await session.run(command, timeout=timeout)
    return await runner.run(command, why=why, timeout=timeout)


async def _wait_connected(
    server: ControlPlaneServer, node_id: str, timeout: float
) -> bool:
    """Whether the agent dialled home and the hub sees it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.hub.is_connected(node_id):
            return True
        await asyncio.sleep(0.1)
    return server.hub.is_connected(node_id)
