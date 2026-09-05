"""What a node can do, measured — and the one way a privileged command runs.

This module exists because of one rule: **discover what a node can do, choose
the least-privilege thing that works, and report what was given up.** Nothing
here decides anything; it establishes facts, and :mod:`spark_pulse.agent.bootstrap`
(install) and :mod:`spark_pulse.agent.doctor` (diagnose and repair) both choose
from the same facts. They are two callers of one probe, not two probes.

Four measurements are easy to get wrong, and each is wrong in a way that only
shows up on someone else's machine:

* **Docker access is tested, never inferred from group membership.** A user
  added to ``docker`` in this session is not in *this login's* credentials, so
  ``id -nG`` can say yes while the socket says no; and a socket can be
  reachable through an ACL with no group at all. So the probe runs ``docker
  version`` and believes the answer. The group is collected only to explain a
  failure to a human.

* **``sudo -n true`` is not a sudo probe.** ``sudo -n -l`` enumerates what is
  free, and the configuration most likely to be misread is a *scoped*
  ``NOPASSWD`` list: ``sudo -n -l`` exits 0, an installer concludes "sudo
  works", and the install fails halfway through having already changed things.
  :class:`SudoCapability` therefore has no boolean called ``works``. It answers
  :meth:`SudoCapability.free_for` for a specific command, and everything else
  is "a password would be needed".

* **A sudo timestamp does not persist across exec channels.** ``tty_tickets``
  is the default and each SSH exec channel is its own session, so the second
  ``sudo -S`` of an install can prompt again seconds after the first.
  :class:`PrivilegedRunner` therefore sends the password on **every** call and
  never assumes an earlier one counted.

* **``requiretty`` hangs rather than failing.** ``sudo`` writes its prompt to a
  tty that is not there and waits. Ubuntu does not set it, but a node that does
  must be *named*, not waited on, so the probe looks for it and every
  privileged call is bounded by a timeout.

The password itself: held in memory for the duration, passed only on stdin,
never interpolated into a command, never written to disk, never logged, and
dropped by :meth:`PrivilegedRunner.drop` when the caller is finished.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field, replace
from typing import Any

from spark_pulse.agent.bootstrap_transport import (
    BootstrapError,
    NodeSession,
    RunResult,
)

logger = logging.getLogger(__name__)

__all__ = [
    "NodeCapabilities",
    "PrivilegedCall",
    "PrivilegedRunner",
    "SudoAuthFailed",
    "SudoCapability",
    "SudoDeclined",
    "SudoUnavailable",
    "probe_node",
]


# ── Errors ──────────────────────────────────────────────────────────────────


class SudoDeclined(BootstrapError):
    """A privileged action was needed and no way to take it was available.

    Raised when the operator declined to supply a password, or when the node's
    policy does not cover the command. Callers catch this and record a
    *concession* — what the install gave up and what it costs — rather than
    letting it abort a run that can still produce a working agent.
    """

    def __init__(self, why: str, reason: str):
        super().__init__(f"cannot {why}: {reason}")
        self.why = why
        self.reason = reason


class SudoAuthFailed(BootstrapError):
    """The sudo password was refused by the node."""


class SudoUnavailable(BootstrapError):
    """``sudo`` cannot be driven over this channel — ``requiretty``, typically."""


# ── Capabilities ────────────────────────────────────────────────────────────

_NOPASSWD_ALL = re.compile(r"\(.*\)\s*NOPASSWD:\s*ALL\s*$")
_NOPASSWD_LINE = re.compile(r"\(.*?\)\s*NOPASSWD:\s*(?P<commands>.+)$")
_REQUIRETTY = ("must have a tty", "no tty present")
_NO_SUDOER = ("is not in the sudoers file", "not allowed to run sudo")
_PASSWORD_REQUIRED = ("a password is required", "a terminal is required")
_BAD_PASSWORD = ("sorry, try again", "incorrect password")


@dataclass(frozen=True)
class SudoCapability:
    """What ``sudo`` on this node will do for this user, without guessing."""

    #: A ``sudo`` binary exists.
    present: bool = False
    #: The login user is already root, so nothing needs elevating at all.
    is_root: bool = False
    #: ``(ALL) NOPASSWD: ALL`` — anything, with no password.
    passwordless_all: bool = False
    #: The specific commands listed as ``NOPASSWD``. A scoped policy.
    passwordless_commands: tuple[str, ...] = ()
    #: The user is a sudoer (or we could not prove otherwise).
    permitted: bool = False
    #: ``sudo`` cannot be driven without a tty on this node.
    requiretty: bool = False
    #: What the probe saw, for the report.
    detail: str = ""

    @property
    def password_required(self) -> bool:
        """Whether a command outside the free list needs a password.

        Deliberately not the inverse of "sudo worked once". A scoped
        ``NOPASSWD`` list means ``sudo -n`` succeeds for those commands and
        needs a password for everything else, and that is the case an
        installer that probed with ``sudo -n true`` gets wrong.
        """
        return self.permitted and not self.is_root and not self.passwordless_all

    def free_for(self, command: str) -> bool:
        """Whether ``command`` runs under ``sudo -n`` with no password.

        Conservative on purpose: an exact match on the command, or a listed
        entry that is a bare executable path equal to this command's argv0
        (which is how sudoers spells "this program, any arguments"). Anything
        cleverer risks reading a narrow grant as a broad one, and a false yes
        here is an install that stops halfway with the node already changed.
        """
        if self.is_root:
            return True
        if not self.permitted:
            return False
        if self.passwordless_all:
            return True
        command = command.strip()
        if command in self.passwordless_commands:
            return True
        try:
            argv0 = shlex.split(command)[0]
        except (ValueError, IndexError):
            return False
        return argv0 in self.passwordless_commands

    def to_dict(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "is_root": self.is_root,
            "passwordless_all": self.passwordless_all,
            "passwordless_commands": list(self.passwordless_commands),
            "password_required": self.password_required,
            "permitted": self.permitted,
            "requiretty": self.requiretty,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class NodeCapabilities:
    """Everything the installer and the doctor need to know about a node.

    Every field is measured. None is assumed from another: the ``docker``
    group does not imply socket access, lingering does not imply a running
    user manager, and being a sudoer does not imply a password will be
    accepted for an arbitrary command.
    """

    user: str = ""
    uid: int = -1
    home: str = ""
    hostname: str = ""
    groups: tuple[str, ...] = ()
    #: ``docker version`` answered. The only trustworthy statement about access.
    docker_socket: bool = False
    docker_version: str = ""
    docker_error: str = ""
    sudo: SudoCapability = field(default_factory=SudoCapability)
    #: ``None`` when ``loginctl`` is not available to answer.
    linger: bool | None = None
    #: A ``systemd --user`` manager is answering. ``degraded`` still counts.
    user_manager: bool = False
    #: The system manager is answering, so a system unit is installable.
    system_manager: bool = False
    python: str = ""
    #: A path that exists and is executable, or empty.
    python_path: str = ""

    @property
    def is_root(self) -> bool:
        return self.uid == 0

    @property
    def in_docker_group(self) -> bool:
        """Group membership. Recorded to explain a failure, never to decide one."""
        return "docker" in self.groups

    def to_dict(self) -> dict[str, Any]:
        """The probe results, as they belong in an install or doctor report."""
        return {
            "user": self.user,
            "uid": self.uid,
            "home": self.home,
            "hostname": self.hostname,
            "groups": list(self.groups),
            "docker_socket": self.docker_socket,
            "docker_version": self.docker_version,
            "docker_error": self.docker_error,
            "in_docker_group": self.in_docker_group,
            "sudo": self.sudo.to_dict(),
            "linger": self.linger,
            "user_manager": self.user_manager,
            "system_manager": self.system_manager,
            "python": self.python,
            "python_path": self.python_path,
        }


# ── The probe ───────────────────────────────────────────────────────────────


async def probe_node(session: NodeSession, *, username: str = "") -> NodeCapabilities:
    """Measure what this node can do. Read-only: it changes nothing.

    Safe to run against a healthy node, which is what makes the doctor safe to
    run when nothing is wrong.
    """
    identity = await session.run(
        "id -u; id -un; id -nG; printf '%s\\n' \"$HOME\"; hostname", timeout=20
    )
    lines = identity.stdout.splitlines()

    def line(index: int) -> str:
        return lines[index].strip() if len(lines) > index else ""

    uid_text = line(0)
    caps = NodeCapabilities(
        uid=int(uid_text) if uid_text.lstrip("-").isdigit() else -1,
        user=line(1) or username,
        groups=tuple(line(2).split()),
        home=line(3),
        hostname=line(4),
    )

    docker = await session.run(
        "docker version --format '{{.Server.Version}}'", timeout=30
    )
    caps = replace(
        caps,
        docker_socket=docker.ok and bool(docker.stdout.strip()),
        docker_version=docker.stdout.strip() if docker.ok else "",
        docker_error="" if docker.ok else (docker.stderr or docker.stdout).strip(),
    )

    caps = replace(caps, sudo=await _probe_sudo(session, caps))

    linger = await session.run(
        f"loginctl show-user {shlex.quote(caps.user)} --property=Linger", timeout=20
    )
    if linger.ok and "Linger=" in linger.stdout:
        caps = replace(caps, linger="Linger=yes" in linger.stdout)
    else:
        # loginctl missing, or no such user session record. Either way we do
        # not know, and ``None`` says so rather than claiming "off".
        caps = replace(caps, linger=None)

    user_manager = await session.run(
        f"XDG_RUNTIME_DIR=/run/user/{caps.uid} systemctl --user is-system-running",
        timeout=20,
    )
    # ``degraded`` exits 1 and is a perfectly usable manager — the Spark
    # reports exactly that. Only "offline"/"Failed to connect" means no manager.
    verdict = (user_manager.stdout + user_manager.stderr).lower()
    caps = replace(
        caps,
        user_manager=bool(
            verdict.strip()
            and "failed to connect" not in verdict
            and "offline" not in verdict
            and "no such file" not in verdict
        ),
    )

    system = await session.run("systemctl is-system-running", timeout=20)
    system_verdict = (system.stdout + system.stderr).lower()
    caps = replace(
        caps,
        system_manager=bool(
            system_verdict.strip()
            and "failed to connect" not in system_verdict
            and "offline" not in system_verdict
            and "not found" not in system_verdict
        ),
    )

    python = await session.run("command -v python3 && python3 -V", timeout=20)
    if python.ok:
        parts = python.stdout.split()
        caps = replace(
            caps,
            python_path=python.stdout.splitlines()[0].strip(),
            python=parts[-1] if parts else "",
        )

    return caps


async def _probe_sudo(session: NodeSession, caps: NodeCapabilities) -> SudoCapability:
    """Enumerate what sudo is free for, rather than testing one command."""
    if caps.is_root:
        return SudoCapability(
            present=True,
            is_root=True,
            permitted=True,
            passwordless_all=True,
            detail="the login user is root; nothing needs elevating",
        )
    listing = await session.run("sudo -n -l", timeout=20)
    text = f"{listing.stdout}\n{listing.stderr}".lower()

    if "not found" in text and listing.returncode == 127:
        return SudoCapability(present=False, detail="no sudo on this node")
    if any(marker in text for marker in _REQUIRETTY):
        return SudoCapability(
            present=True,
            permitted=True,
            requiretty=True,
            detail="sudo on this node requires a tty; it cannot be driven over "
            "an SSH exec channel",
        )
    if any(marker in text for marker in _NO_SUDOER):
        return SudoCapability(
            present=True,
            permitted=False,
            detail=f"{caps.user} is not a sudoer on this node",
        )
    if not listing.ok:
        if any(marker in text for marker in _PASSWORD_REQUIRED):
            return SudoCapability(
                present=True,
                permitted=True,
                detail="sudo requires a password for every command",
            )
        return SudoCapability(
            present=True,
            permitted=False,
            detail=(listing.stderr or listing.stdout).strip()[:200]
            or "sudo -n -l failed without saying why",
        )

    free_all = False
    commands: list[str] = []
    for raw in listing.stdout.splitlines():
        entry = raw.strip()
        if _NOPASSWD_ALL.search(entry):
            free_all = True
            continue
        match = _NOPASSWD_LINE.search(entry)
        if match:
            commands.extend(
                part.strip()
                for part in match.group("commands").split(",")
                if part.strip()
            )
    return SudoCapability(
        present=True,
        permitted=True,
        passwordless_all=free_all,
        passwordless_commands=tuple(commands),
        detail=(
            "sudo is passwordless for everything"
            if free_all
            else (
                f"sudo is passwordless for {len(commands)} specific command(s); "
                "anything else needs a password"
                if commands
                else "sudo is permitted and needs a password"
            )
        ),
    )


# ── Running something privileged ────────────────────────────────────────────


@dataclass(frozen=True)
class PrivilegedCall:
    """One elevation, recorded so the report can say exactly what was used."""

    why: str
    command: str
    via: str
    returncode: int


class PrivilegedRunner:
    """The single place a command runs with more rights than the login user has.

    Holds the sudo password in memory for one operation's duration and sends
    it on stdin on **every** call, because ``tty_tickets`` means the previous
    one does not count. :meth:`drop` releases it.
    """

    def __init__(
        self,
        session: NodeSession,
        capabilities: NodeCapabilities,
        *,
        password: str | None = None,
        prompt: Any | None = None,
    ):
        self._session = session
        self._caps = capabilities
        self._password = password
        #: Asked once, and only once, if the password we started with is
        #: refused. Sudo does not have to use the login password — an LDAP or
        #: SSSD setup routinely does not — so a refusal is a question to put to
        #: the operator rather than a failure to report.
        self._prompt = prompt
        self._reprompted = False
        self.calls: list[PrivilegedCall] = []

    @property
    def has_password(self) -> bool:
        return self._password is not None

    def offer_password(self, password: str | None) -> None:
        """Supply (or replace) the sudo password mid-operation."""
        self._password = password

    def drop(self) -> None:
        """Forget the password. Called when the operation ends, always."""
        self._password = None

    def can(self, command: str) -> bool:
        """Whether :meth:`run` would be able to run ``command`` at all."""
        sudo = self._caps.sudo
        if sudo.is_root or sudo.free_for(command):
            return True
        if sudo.requiretty or not sudo.present or not sudo.permitted:
            return False
        return self._password is not None

    def why_not(self, command: str) -> str:
        """Why :meth:`can` said no, phrased for an operator."""
        sudo = self._caps.sudo
        if not sudo.present:
            return "this node has no sudo"
        if sudo.requiretty:
            return sudo.detail
        if not sudo.permitted:
            return sudo.detail or f"{self._caps.user} may not use sudo here"
        if sudo.passwordless_commands:
            return (
                "sudo here is passwordless only for "
                f"{', '.join(sudo.passwordless_commands)}, and no password was given"
            )
        return "sudo needs a password and none was given"

    async def run(self, command: str, *, why: str, timeout: float = 60.0) -> RunResult:
        """Run ``command`` with elevation, or raise :class:`SudoDeclined`.

        ``why`` is the operator-facing reason, and it is what appears in the
        report next to the call — so a report says "enable lingering for alex"
        rather than an argv nobody can read.
        """
        sudo = self._caps.sudo
        if sudo.is_root:
            result = await self._session.run(command, timeout=timeout)
            via = "root"
        elif sudo.free_for(command):
            result = await self._session.run(f"sudo -n {command}", timeout=timeout)
            via = "sudo -n"
        elif not self.can(command):
            raise SudoDeclined(why, self.why_not(command))
        else:
            result = await self._authenticate(command, timeout)
            if _refused(result) and self._prompt is not None and not self._reprompted:
                self._reprompted = True
                fresh = await self._prompt(
                    f"the sudo password for {self._caps.user} was refused; "
                    "sudo may not use the login password on this node"
                )
                if fresh and fresh != self._password:
                    self._password = fresh
                    result = await self._authenticate(command, timeout)
            via = "sudo -S"
        self.calls.append(
            PrivilegedCall(
                why=why, command=command, via=via, returncode=result.returncode
            )
        )
        _raise_for_sudo(result, why)
        return result

    async def _authenticate(self, command: str, timeout: float) -> RunResult:
        """One ``sudo -S`` attempt.

        ``-S`` reads the password from stdin; ``-p ''`` suppresses the prompt
        so no part of it lands in stderr. The password is sent on **every**
        call and never assumed cached: sudo timestamps are per-tty and each
        exec channel is a new session, so the previous authentication does not
        count.
        """
        return await self._session.run(
            f"sudo -S -p '' {command}",
            stdin=f"{self._password}\n",
            timeout=timeout,
        )


def _refused(result: RunResult) -> bool:
    stderr = result.stderr.lower()
    return not result.ok and any(marker in stderr for marker in _BAD_PASSWORD)


def _raise_for_sudo(result: RunResult, why: str) -> None:
    """Turn sudo's own refusals into named errors, never the command's."""
    if result.ok:
        return
    stderr = result.stderr.lower()
    if any(marker in stderr for marker in _BAD_PASSWORD):
        raise SudoAuthFailed(f"the sudo password was refused while trying to {why}")
    if any(marker in stderr for marker in _REQUIRETTY):
        raise SudoUnavailable(
            f"sudo on this node requires a tty, so it cannot {why} over SSH"
        )
    if "not allowed to execute" in stderr or "not allowed to run" in stderr:
        raise SudoDeclined(why, "the node's sudoers policy does not allow it")
    # Anything else is the *command's* failure, not sudo's, and belongs to the
    # caller: a `systemctl restart` that fails is a diagnosis, not an error here.
