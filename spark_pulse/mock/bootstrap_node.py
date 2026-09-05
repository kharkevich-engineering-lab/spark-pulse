"""A node you can install an agent onto, without a node.

``mock/node_service.py`` simulates *docker over SSH* for an already-enrolled
cluster. This simulates the machine underneath that: a filesystem, a login, a
sudo policy, a Docker socket, systemd (system and user), lingering, and a
``python3`` that really runs the agent. It exists because every interesting
thing about the SSH installer is a *configuration* — a user in ``docker`` or
not, lingering on or off, sudo passwordless or scoped or absent — and the only
honest way to test a matrix of configurations is to be able to build one.

Three things make it worth more than a stack of mocks:

* **The filesystem is real.** Each node owns a temporary directory that stands
  in for ``/``. Uploads land in it, ``tar -xzf`` actually unpacks the bundle
  the installer actually built, and the identity the agent writes is a real
  identity on disk that a later install genuinely finds.
* **The agent is real.** ``systemctl enable --now`` reads the unit file the
  installer wrote, parses its ``ExecStart``, and runs the real
  :func:`spark_pulse.agent.node_agent.enroll` and :class:`NodeAgent` against
  the real :class:`ControlPlaneServer` over real mTLS on loopback. "Installed →
  enrolled → connected" is therefore end to end, not three mocks agreeing.
* **Every command is recorded with its stdin kept separate.** That is what
  makes "the password never appears in argv" a test rather than a comment: the
  assertion reads :attr:`SimulatedNode.commands` and finds the password in no
  ``command`` field, only in the stdin of a ``sudo -S``.

The shell here understands ``&&``, ``||``, ``;``, ``>``/``>>`` and
``2>/dev/null`` and about twenty verbs. It is deliberately small: if the
installer needs a command this cannot run, that is a signal the installer is
doing something too clever to be understood on a node at 3am, and the fix
belongs there rather than here.
"""

from __future__ import annotations

import asyncio
import io
import os
import shlex
import tarfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from spark_pulse.agent.bootstrap_transport import (
    AuthFailed,
    BootstrapError,
    HostKey,
    RootPasswordBootstrap,
    RunResult,
    Unreachable,
    keypair_from_private_pem,
)

__all__ = [
    "InProcessAgentRunner",
    "SimulatedFleet",
    "SimulatedNode",
    "SimulatedSession",
    "SimulatedUser",
    "SudoPolicy",
    "UnitState",
    "spark_like_node",
]


# ── The node's configuration ────────────────────────────────────────────────


@dataclass
class SudoPolicy:
    """What ``sudo`` does for one user.

    ``mode`` is the whole matrix: ``none`` (not a sudoer), ``nopasswd``
    (``NOPASSWD: ALL``), ``password`` (a sudoer who must authenticate), and
    ``scoped`` (``NOPASSWD`` for :attr:`commands` and a password for anything
    else). ``scoped`` is the one most likely to be misread as "sudo works".
    """

    mode: str = "password"
    commands: tuple[str, ...] = ()
    #: The password sudo will accept. ``None`` means the login password.
    password: str | None = None


@dataclass
class SimulatedUser:
    name: str
    uid: int = 1000
    password: str | None = None
    authorized_keys: set[str] = field(default_factory=set)
    groups: tuple[str, ...] = ("adm", "sudo")
    sudo: SudoPolicy = field(default_factory=SudoPolicy)

    @property
    def sudo_password(self) -> str | None:
        return self.sudo.password if self.sudo.password is not None else self.password


@dataclass
class UnitState:
    name: str
    enabled: bool = False
    active: bool = False
    scope: str = "user"
    exec_start: str = ""
    #: The task running the agent this unit started, if it dialled home.
    task: asyncio.Task | None = None


class SimulatedNode:
    """One machine: a filesystem, logins, systemd, docker and a sudo policy."""

    def __init__(
        self,
        host: str,
        root: Path,
        *,
        users: dict[str, SimulatedUser] | None = None,
        hostname: str = "",
        host_key_blob: bytes | None = None,
        reachable: bool = True,
        permit_root_login: str = "prohibit-password",
        docker_running: bool = True,
        docker_socket_users: set[str] | None = None,
        docker_version: str = "29.2.1",
        linger: dict[str, bool] | None = None,
        loginctl: bool = True,
        linger_settable: bool = True,
        user_manager: bool = True,
        system_manager: bool = True,
        python_version: str = "Python 3.12.3",
        python_path: str = "/usr/bin/python3",
        requiretty: bool = False,
        agent_dials_home: bool = True,
        can_reach_control_plane: bool = True,
        free_bytes: int = 400 * 1024**3,
        clock_skew: float = 0.0,
        agent_runner: Callable[..., Awaitable[RunResult]] | None = None,
    ):
        self.host = host
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.hostname = hostname or host.replace(".", "-")
        self.users = users or {}
        self.host_key_blob = host_key_blob or f"ssh-ed25519-blob-{host}".encode()
        self.reachable = reachable
        self.permit_root_login = permit_root_login
        self.docker_running = docker_running
        self.docker_socket_users = (
            docker_socket_users
            if docker_socket_users is not None
            else {u for u in self.users}
        )
        self.docker_version = docker_version
        self.linger = linger or {}
        self.loginctl = loginctl
        self.linger_settable = linger_settable
        self.user_manager = user_manager
        self.system_manager = system_manager
        self.python_version = python_version
        self.python_path = python_path
        self.requiretty = requiretty
        self.agent_dials_home = agent_dials_home
        #: Whether the node's own python can open a socket to the control
        #: plane. The doctor asks the *node*, so this is the node's answer.
        self.can_reach_control_plane = can_reach_control_plane
        self.free_bytes = free_bytes
        #: Seconds this node's clock is ahead of the control plane's.
        self.clock_skew = clock_skew
        self.agent_runner = agent_runner or InProcessAgentRunner()
        self.units: dict[str, UnitState] = {}

        #: Every command, with ``stdin`` kept as its own field. The separation
        #: is the point: a secret may appear there and nowhere else.
        self.commands: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []
        #: How many times sudo actually asked for a password. Per-tty
        #: timestamps mean this is not one per install.
        self.sudo_prompts = 0

    # ── Paths ────────────────────────────────────────────────────────────

    @property
    def host_key(self) -> HostKey:
        return HostKey(
            host=self.host, port=22, algorithm="ssh-ed25519", blob=self.host_key_blob
        )

    def home(self, user: str) -> str:
        return "/root" if user == "root" else f"/home/{user}"

    def path(self, remote: str, user: str) -> Path:
        """A node-absolute path, mapped into this node's temporary root."""
        remote = self.expand(remote, user)
        return self.root / remote.lstrip("/")

    def expand(self, remote: str, user: str) -> str:
        home = self.home(user)
        if remote == "~":
            return home
        if remote.startswith("~/"):
            return home + remote[1:]
        return remote.replace("$HOME", home)

    def authorized_keys(self, user: SimulatedUser) -> set[str]:
        """Keys this user may log in with: the seeded set *and* the real file.

        Both, because a test provisions the set and the installer writes the
        file — and "the installer's key works afterwards" is only a real
        assertion if the login path reads what the installer actually wrote.
        """
        keys = {_key_body(key) for key in user.authorized_keys}
        path = self.path(f"{self.home(user.name)}/.ssh/authorized_keys", user.name)
        if path.exists():
            keys |= {
                _key_body(line.strip())
                for line in path.read_text().splitlines()
                if line.strip()
            }
        return keys

    def read(self, remote: str, user: str = "root") -> bytes:
        return self.path(remote, user).read_bytes()

    def exists(self, remote: str, user: str = "root") -> bool:
        path = self.path(remote, user)
        return path.exists() or path.is_symlink()

    # ── Stopping ─────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Stop anything this node started, so a test leaves no task behind."""
        for unit in self.units.values():
            await _stop_unit(unit)


def spark_like_node(
    host: str, root: Path, *, user: str = "alex", password: str = ""
) -> SimulatedNode:
    """The measured DGX Spark: non-root user, ``docker`` group, linger on,
    sudo present and password-required.

    A convenience for the common case, never a default the tests rely on —
    every dimension is still set explicitly where it is under test.
    """
    account = SimulatedUser(
        name=user,
        uid=1000,
        password=password or None,
        groups=("adm", "sudo", "docker"),
        sudo=SudoPolicy(mode="password"),
    )
    return SimulatedNode(
        host,
        root,
        users={user: account},
        docker_socket_users={user},
        linger={user: True},
    )


# ── The fleet, which is the Connector ───────────────────────────────────────


class SimulatedFleet:
    """A :class:`~spark_pulse.agent.bootstrap_transport.Connector` over nodes.

    ``fail_hosts`` is a mutable set for the same reason
    ``SimulatedDockerSSHClient`` has one: a node can go away *during* an
    install, and that is a case worth reaching.
    """

    def __init__(self, nodes: dict[str, SimulatedNode] | None = None):
        self.nodes: dict[str, SimulatedNode] = dict(nodes or {})
        self.fail_hosts: set[str] = set()

    def add(self, node: SimulatedNode) -> SimulatedNode:
        self.nodes[node.host] = node
        return node

    def __getitem__(self, host: str) -> SimulatedNode:
        return self.nodes[host]

    def _node(self, host: str) -> SimulatedNode:
        node = self.nodes.get(host)
        if node is None or not node.reachable or host in self.fail_hosts:
            raise Unreachable(f"cannot reach {host}: no route to host")
        return node

    async def host_key(
        self, host: str, port: int = 22, *, timeout: float = 10.0
    ) -> HostKey:
        return self._node(host).host_key

    async def connect(
        self,
        host: str,
        username: str,
        *,
        port: int = 22,
        password: str | None = None,
        private_key: bytes | None = None,
        host_key: HostKey | None = None,
        timeout: float = 20.0,
    ) -> SimulatedSession:
        node = self._node(host)
        if host_key is not None and host_key.blob != node.host_key_blob:
            raise BootstrapError(
                f"host key mismatch for {host}: the pinned key is not the one offered"
            )
        user = node.users.get(username)
        if user is None:
            raise AuthFailed(f"{username}@{host} refused the credentials offered")

        if private_key is not None:
            offered = keypair_from_private_pem(private_key).public_openssh
            if _key_body(offered) not in node.authorized_keys(user):
                raise AuthFailed(f"{username}@{host} refused the credentials offered")
        elif password is not None:
            if username == "root" and node.permit_root_login == "prohibit-password":
                raise RootPasswordBootstrap(
                    f"{host} refused a password for root (PermitRootLogin "
                    "prohibit-password)"
                )
            if user.password is None or password != user.password:
                raise AuthFailed(f"{username}@{host} refused the credentials offered")
        else:
            raise AuthFailed(f"{username}@{host} refused the credentials offered")
        return SimulatedSession(node, user)

    async def shutdown(self) -> None:
        for node in self.nodes.values():
            await node.shutdown()


def _key_body(line: str) -> str:
    """An OpenSSH public key without its comment, so comments do not matter."""
    parts = line.split()
    return " ".join(parts[:2]) if len(parts) >= 2 else line


# ── The session, which is the tiny shell ────────────────────────────────────


class SimulatedSession:
    """One login on one node."""

    def __init__(self, node: SimulatedNode, user: SimulatedUser):
        self.node = node
        self.user = user
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def upload(self, data: bytes, remote_path: str, *, mode: int = 0o600) -> None:
        node, user = self.node, self.user
        target = node.path(remote_path, user.name)
        if not _may_write(
            node, user, node.expand(remote_path, user.name), as_root=False
        ):
            raise BootstrapError(f"permission denied writing {remote_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
        node.uploads.append(
            {
                "path": node.expand(remote_path, user.name),
                "mode": mode,
                "size": len(data),
            }
        )

    async def run(
        self, command: str, *, stdin: str | None = None, timeout: float = 60.0
    ) -> RunResult:
        node = self.node
        node.commands.append(
            {"user": self.user.name, "command": command, "stdin": stdin}
        )
        return await self._sequence(command, stdin, as_root=self.user.uid == 0)

    # ── The shell ────────────────────────────────────────────────────────

    async def _sequence(
        self, script: str, stdin: str | None, *, as_root: bool
    ) -> RunResult:
        """``&&``, ``||`` and ``;``, left to right, with the shell's semantics."""
        result = RunResult(0, "", "")
        stdout: list[str] = []
        pending = _split_operators(script)
        skip_until_or = False
        for operator, clause in pending:
            if operator == "&&" and result.returncode != 0:
                continue
            if operator == "||":
                if result.returncode == 0:
                    skip_until_or = True
                    continue
                skip_until_or = False
            elif skip_until_or and operator == "&&":
                continue
            else:
                skip_until_or = False
            result = await self._redirected(clause, stdin, as_root=as_root)
            if result.stdout:
                stdout.append(result.stdout)
        return RunResult(result.returncode, "".join(stdout), result.stderr)

    async def _redirected(
        self, clause: str, stdin: str | None, *, as_root: bool
    ) -> RunResult:
        """One command, honouring ``> file``, ``>> file`` and ``2>/dev/null``."""
        clause = clause.strip()
        quiet = "2>/dev/null" in clause
        clause = clause.replace("2>/dev/null", "").strip()
        append = False
        target = ""
        for marker, is_append in ((">>", True), (">", False)):
            index = clause.rfind(marker)
            if index != -1 and marker not in ("&>",):
                target = clause[index + len(marker) :].strip()
                clause = clause[:index].strip()
                append = is_append
                break
        result = await self._one(clause, stdin, as_root=as_root)
        if target and target != "/dev/null":
            path = self.node.path(target, self.user.name)
            if not _may_write(
                self.node, self.user, self.node.expand(target, self.user.name), as_root
            ):
                return RunResult(1, "", f"{target}: Permission denied")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a" if append else "w") as handle:
                handle.write(result.stdout)
            result = RunResult(result.returncode, "", result.stderr)
        elif target == "/dev/null":
            result = RunResult(result.returncode, "", result.stderr)
        if quiet:
            result = RunResult(result.returncode, result.stdout, "")
        return result

    async def _one(self, clause: str, stdin: str | None, *, as_root: bool) -> RunResult:
        try:
            parts = shlex.split(clause)
        except ValueError:
            return RunResult(2, "", f"unparseable: {clause}")
        if not parts:
            return RunResult(0, "", "")

        # `VAR=value cmd` and `umask 077 && ...` style prefixes.
        while parts and ("=" in parts[0] and not parts[0].startswith("/")):
            parts = parts[1:]
        if not parts:
            return RunResult(0, "", "")

        if parts[0] == "sudo":
            return await self._sudo(parts[1:], stdin)

        verb = parts[0]
        handler = getattr(self, f"_cmd_{Path(verb).name.replace('-', '_')}", None)
        if handler is None:
            if verb.endswith("/spark-pulse-agent") or "spark-pulse-agent" in verb:
                return await self._agent(parts, background=False)
            return RunResult(127, "", f"{verb}: command not found")
        return await handler(parts, stdin, as_root)

    # ── sudo ─────────────────────────────────────────────────────────────

    async def _sudo(self, parts: list[str], stdin: str | None) -> RunResult:
        node, user = self.node, self.user
        policy = user.sudo
        if user.uid == 0:  # pragma: no cover - root never calls sudo here
            return await self._sequence(shlex.join(parts), stdin, as_root=True)
        if node.requiretty:
            return RunResult(1, "", "sudo: sorry, you must have a tty to run sudo")

        non_interactive = "-n" in parts
        read_password = "-S" in parts
        rest = [p for p in parts if p not in ("-n", "-S", "-p", "")]
        if parts and "-p" in parts:
            index = parts.index("-p")
            if index + 1 < len(parts):
                rest = [p for p in rest if p != parts[index + 1]]

        if rest[:1] == ["-l"]:
            return self._sudo_list()

        command = shlex.join(rest)
        if policy.mode == "none":
            return RunResult(
                1,
                "",
                f"{user.name} is not in the sudoers file. This incident will be reported.",
            )
        free = policy.mode == "nopasswd" or (
            policy.mode == "scoped" and _covered(command, policy.commands)
        )
        if free:
            return await self._sequence(command, None, as_root=True)
        if non_interactive and not read_password:
            return RunResult(1, "", "sudo: a password is required")
        if not read_password:  # pragma: no cover - installer always uses -S
            return RunResult(1, "", "sudo: a password is required")

        node.sudo_prompts += 1
        offered = (stdin or "").split("\n")[0]
        if not offered or offered != (user.sudo_password or ""):
            return RunResult(
                1, "", "Sorry, try again.\nsudo: 1 incorrect password attempt"
            )
        return await self._sequence(command, None, as_root=True)

    def _sudo_list(self) -> RunResult:
        node, user = self.node, self.user
        policy = user.sudo
        header = (
            f"User {user.name} may run the following commands on {node.hostname}:\n"
        )
        if policy.mode == "none":
            return RunResult(
                1,
                "",
                f"Sorry, user {user.name} is not allowed to run sudo on {node.hostname}.",
            )
        if policy.mode == "nopasswd":
            return RunResult(0, header + "    (ALL : ALL) NOPASSWD: ALL\n", "")
        if policy.mode == "scoped":
            return RunResult(
                0,
                header + "    (root) NOPASSWD: " + ", ".join(policy.commands) + "\n",
                "",
            )
        # A sudoer who must authenticate cannot even list without doing so.
        return RunResult(1, "", "sudo: a password is required")

    # ── Verbs ────────────────────────────────────────────────────────────

    async def _cmd_id(self, parts, stdin, as_root) -> RunResult:
        user = self.user
        flag = parts[1] if len(parts) > 1 else "-u"
        if flag == "-u":
            return RunResult(0, f"{user.uid}\n", "")
        if flag == "-un":
            return RunResult(0, f"{user.name}\n", "")
        if flag == "-nG":
            return RunResult(0, " ".join(user.groups) + "\n", "")
        return RunResult(0, f"uid={user.uid}({user.name})\n", "")

    async def _cmd_hostname(self, parts, stdin, as_root) -> RunResult:
        return RunResult(0, self.node.hostname + "\n", "")

    async def _cmd_printf(self, parts, stdin, as_root) -> RunResult:
        values = [self.node.expand(value, self.user.name) for value in parts[2:]]
        return RunResult(0, "\n".join(values) + ("\n" if values else ""), "")

    async def _cmd_echo(self, parts, stdin, as_root) -> RunResult:
        values = [self.node.expand(value, self.user.name) for value in parts[1:]]
        return RunResult(0, " ".join(values) + "\n", "")

    async def _cmd_command(self, parts, stdin, as_root) -> RunResult:
        target = parts[-1]
        if target == "python3":
            return RunResult(0, self.node.python_path + "\n", "")
        return RunResult(1, "", "")

    async def _cmd_python3(self, parts, stdin, as_root) -> RunResult:
        if "-V" in parts or "--version" in parts:
            return RunResult(0, self.node.python_version + "\n", "")
        if "-c" in parts:
            script = parts[parts.index("-c") + 1]
            if "create_connection" in script:
                if self.node.can_reach_control_plane:
                    return RunResult(0, "", "")
                return RunResult(
                    1, "", "ConnectionRefusedError: [Errno 111] Connection refused"
                )
        return RunResult(0, "", "")

    async def _cmd_date(self, parts, stdin, as_root) -> RunResult:
        import time as _time

        return RunResult(0, f"{int(_time.time() + self.node.clock_skew)}\n", "")

    async def _cmd_df(self, parts, stdin, as_root) -> RunResult:
        target = parts[-1]
        blocks = self.node.free_bytes // 1024
        return RunResult(
            0,
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            f"/dev/nvme0n1p2 {blocks * 2} {blocks} {blocks} 50% {target}\n",
            "",
        )

    async def _cmd_stat(self, parts, stdin, as_root) -> RunResult:
        target = parts[-1]
        path = self.node.path(target, self.user.name)
        if not path.exists():
            return RunResult(1, "", f"stat: cannot statx '{target}'")
        return RunResult(0, f"{path.stat().st_mode & 0o777:o}\n", "")

    async def _cmd_docker(self, parts, stdin, as_root) -> RunResult:
        node, user = self.node, self.user
        if not node.docker_running:
            return RunResult(
                1,
                "",
                "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
                "Is the docker daemon running?",
            )
        if not as_root and user.name not in node.docker_socket_users:
            return RunResult(
                1,
                "",
                "permission denied while trying to connect to the Docker daemon "
                "socket at unix:///var/run/docker.sock",
            )
        return RunResult(0, node.docker_version + "\n", "")

    async def _cmd_loginctl(self, parts, stdin, as_root) -> RunResult:
        node = self.node
        if not node.loginctl:
            return RunResult(127, "", "loginctl: command not found")
        if len(parts) > 1 and parts[1] == "show-user":
            target = parts[2] if len(parts) > 2 else self.user.name
            state = "yes" if node.linger.get(target) else "no"
            return RunResult(0, f"Linger={state}\n", "")
        if len(parts) > 1 and parts[1] == "enable-linger":
            if not as_root:
                return RunResult(1, "", "Failed to enable linger: Access denied")
            if not node.linger_settable:
                return RunResult(
                    1, "", "Failed to enable linger: Read-only file system"
                )
            node.linger[parts[2] if len(parts) > 2 else self.user.name] = True
            return RunResult(0, "", "")
        return RunResult(1, "", f"loginctl: unsupported: {shlex.join(parts)}")

    async def _cmd_usermod(self, parts, stdin, as_root) -> RunResult:
        node = self.node
        if not as_root:
            return RunResult(1, "", "usermod: Permission denied.")
        target = parts[-1]
        account = node.users.get(target)
        if account is None:
            return RunResult(6, "", f"usermod: user '{target}' does not exist")
        if "docker" not in account.groups:
            account.groups = account.groups + ("docker",)
        # Deliberately *not* adding them to docker_socket_users: a group added
        # now is not in this login's credentials, which is exactly the trap the
        # installer reports as a concession rather than assuming away.
        return RunResult(0, "", "")

    async def _cmd_mkdir(self, parts, stdin, as_root) -> RunResult:
        node, user = self.node, self.user
        mode = 0o755
        targets: list[str] = []
        index = 1
        while index < len(parts):
            token = parts[index]
            if token == "-m":
                index += 1
                mode = int(parts[index], 8)
            elif token.startswith("-"):
                pass
            else:
                targets.append(token)
            index += 1
        for target in targets:
            expanded = node.expand(target, user.name)
            if not _may_write(node, user, expanded, as_root):
                return RunResult(
                    1, "", f"mkdir: cannot create '{target}': Permission denied"
                )
            path = node.path(target, user.name)
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(mode)
        return RunResult(0, "", "")

    async def _cmd_touch(self, parts, stdin, as_root) -> RunResult:
        for target in parts[1:]:
            path = self.node.path(target, self.user.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        return RunResult(0, "", "")

    async def _cmd_chmod(self, parts, stdin, as_root) -> RunResult:
        mode = int(parts[1], 8)
        for target in parts[2:]:
            path = self.node.path(target, self.user.name)
            if path.exists():
                path.chmod(mode)
        return RunResult(0, "", "")

    async def _cmd_cat(self, parts, stdin, as_root) -> RunResult:
        out: list[str] = []
        for target in parts[1:]:
            path = self.node.path(target, self.user.name)
            if not path.exists():
                return RunResult(1, "", f"cat: {target}: No such file or directory")
            out.append(path.read_text())
        return RunResult(0, "".join(out), "")

    async def _cmd_grep(self, parts, stdin, as_root) -> RunResult:
        """Only the one form the installer uses: ``grep -qxF -f needles hay``."""
        if "-f" not in parts:
            return RunResult(2, "", "grep: unsupported invocation")
        needles_path = parts[parts.index("-f") + 1]
        hay_path = parts[-1]
        needles = self.node.path(needles_path, self.user.name)
        hay = self.node.path(hay_path, self.user.name)
        if not needles.exists() or not hay.exists():
            return RunResult(2, "", "grep: No such file or directory")
        wanted = {
            line.strip() for line in needles.read_text().splitlines() if line.strip()
        }
        present = {line.strip() for line in hay.read_text().splitlines()}
        return RunResult(0 if wanted & present else 1, "", "")

    async def _cmd_test(self, parts, stdin, as_root) -> RunResult:
        flag = parts[1] if len(parts) > 2 else "-e"
        target = parts[-1]
        path = self.node.path(target, self.user.name)
        exists = path.exists() or path.is_symlink()
        if flag == "-d":
            exists = path.is_dir()
        if flag == "-f":
            exists = path.is_file()
        return RunResult(0 if exists else 1, "", "")

    async def _cmd_rm(self, parts, stdin, as_root) -> RunResult:
        import shutil

        node, user = self.node, self.user
        for target in parts[1:]:
            if target.startswith("-"):
                continue
            expanded = node.expand(target, user.name)
            if not _may_write(node, user, expanded, as_root):
                return RunResult(
                    1, "", f"rm: cannot remove '{target}': Permission denied"
                )
            path = node.path(target, user.name)
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        return RunResult(0, "", "")

    async def _cmd_shred(self, parts, stdin, as_root) -> RunResult:
        for target in parts[1:]:
            if target.startswith("-"):
                continue
            self.node.path(target, self.user.name).unlink(missing_ok=True)
        return RunResult(0, "", "")

    async def _cmd_ln(self, parts, stdin, as_root) -> RunResult:
        node, user = self.node, self.user
        args = [p for p in parts[1:] if not p.startswith("-")]
        if len(args) != 2:
            return RunResult(1, "", "ln: unsupported invocation")
        target, link = args
        expanded = node.expand(link, user.name)
        if not _may_write(node, user, expanded, as_root):
            return RunResult(1, "", f"ln: {link}: Permission denied")
        path = node.path(link, user.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.exists():
            path.unlink()
        os.symlink(target, path)
        return RunResult(0, "", "")

    async def _cmd_tar(self, parts, stdin, as_root) -> RunResult:
        node, user = self.node, self.user
        archive = parts[parts.index("-xzf") + 1] if "-xzf" in parts else ""
        destination = parts[parts.index("-C") + 1] if "-C" in parts else "."
        source = node.path(archive, user.name)
        if not source.exists():
            return RunResult(2, "", f"tar: {archive}: No such file or directory")
        out = node.path(destination, user.name)
        if not _may_write(node, user, node.expand(destination, user.name), as_root):
            return RunResult(2, "", f"tar: {destination}: Permission denied")
        out.mkdir(parents=True, exist_ok=True)
        with tarfile.open(source, "r:gz") as tar:
            for member in tar.getmembers():
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                target = out / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(extracted.read())
                target.chmod(member.mode)
        return RunResult(0, "", "")

    async def _cmd_install(self, parts, stdin, as_root) -> RunResult:
        node, user = self.node, self.user
        mode = 0o755
        positional: list[str] = []
        index = 1
        while index < len(parts):
            token = parts[index]
            if token == "-m":
                index += 1
                mode = int(parts[index], 8)
            elif token in ("-o", "-g"):
                index += 1
            elif token.startswith("-"):
                pass
            else:
                positional.append(token)
            index += 1
        if len(positional) != 2:
            return RunResult(1, "", "install: unsupported invocation")
        source, destination = positional
        if not _may_write(node, user, node.expand(destination, user.name), as_root):
            return RunResult(
                1, "", f"install: cannot create '{destination}': Permission denied"
            )
        src = node.path(source, user.name)
        dst = node.path(destination, user.name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes() if src.exists() else b"")
        dst.chmod(mode)
        return RunResult(0, "", "")

    async def _cmd_visudo(self, parts, stdin, as_root) -> RunResult:
        target = parts[-1]
        path = self.node.path(target, self.user.name)
        if not path.exists():
            return RunResult(1, "", f"visudo: {target}: No such file or directory")
        text = path.read_text()
        if "NOPASSWD" not in text and "ALL" not in text:
            return RunResult(1, "", f"visudo: {target}: parse error")
        return RunResult(0, f"{target}: parsed OK\n", "")

    async def _cmd_systemctl(self, parts, stdin, as_root) -> RunResult:
        node = self.node
        scope = "user" if "--user" in parts else "system"
        args = [p for p in parts[1:] if not p.startswith("--")]
        if scope == "user" and not node.user_manager:
            return RunResult(1, "", "Failed to connect to bus: No medium found")
        if scope == "system" and not node.system_manager:
            return RunResult(1, "", "Failed to connect to bus: Host is down")
        verb = args[0] if args else ""
        unit_name = args[1] if len(args) > 1 else ""

        if verb == "is-system-running":
            return RunResult(1, "degraded\n", "")
        if verb == "daemon-reload":
            return RunResult(0, "", "")
        if verb in ("enable", "start", "restart"):
            return await self._start_unit(
                unit_name, scope, as_root, restart=verb == "restart"
            )
        if verb in ("disable", "stop"):
            unit = node.units.get(unit_name)
            if unit is not None:
                await _stop_unit(unit)
                unit.active = False
                if verb == "disable":
                    unit.enabled = False
            return RunResult(0, "", "")
        if verb == "is-active":
            unit = node.units.get(unit_name)
            active = bool(unit and unit.active)
            return RunResult(
                0 if active else 3, ("active" if active else "inactive") + "\n", ""
            )
        if verb == "is-enabled":
            unit = node.units.get(unit_name)
            enabled = bool(unit and unit.enabled)
            return RunResult(
                0 if enabled else 1, ("enabled" if enabled else "disabled") + "\n", ""
            )
        if verb in ("status", "show"):
            unit = node.units.get(unit_name)
            state = "active (running)" if unit and unit.active else "inactive (dead)"
            return RunResult(
                0 if unit and unit.active else 3,
                f"● {unit_name}\n   Active: {state}\n",
                "",
            )
        return RunResult(1, "", f"systemctl: unsupported: {shlex.join(parts)}")

    async def _start_unit(
        self, unit_name: str, scope: str, as_root: bool, *, restart: bool
    ) -> RunResult:
        node, user = self.node, self.user
        unit_dir = (
            f"{node.home(user.name)}/.config/systemd/user"
            if scope == "user"
            else "/etc/systemd/system"
        )
        unit_file = node.path(f"{unit_dir}/{unit_name}", user.name)
        if not unit_file.exists():
            return RunResult(
                1, "", f"Failed to enable unit: Unit file {unit_name} does not exist."
            )
        exec_start = ""
        identity_dir = ""
        for line in unit_file.read_text().splitlines():
            if line.startswith("ExecStart="):
                exec_start = line.split("=", 1)[1].strip()
            if line.startswith("Environment=SPARK_PULSE_AGENT_DIR="):
                identity_dir = line.split("=", 2)[2].strip()
        unit = node.units.setdefault(unit_name, UnitState(unit_name, scope=scope))
        unit.enabled = True
        unit.exec_start = exec_start
        if restart or unit.active:
            await _stop_unit(unit)
        unit.active = True
        if not node.agent_dials_home:
            # The unit is "running" as far as systemd is concerned and the
            # agent never appears in the hub. The install has to notice.
            return RunResult(0, "", "")
        argv = shlex.split(exec_start)[1:]
        if identity_dir and "--dir" not in argv:
            argv += ["--dir", identity_dir]
        result = await self._agent([shlex.split(exec_start)[0], *argv], background=True)
        if not result.ok:
            unit.active = False
            return RunResult(
                1, "", f"Job for {unit_name} failed: {result.stderr[:200]}"
            )
        unit.task = getattr(self, "_last_task", None)
        return RunResult(0, "", "")

    async def _agent(self, parts: list[str], *, background: bool) -> RunResult:
        self.node._last_agent_task = None
        result = await self.node.agent_runner(
            self.node, self.user, parts[1:], background=background
        )
        self._last_task = self.node._last_agent_task
        return result


def _covered(command: str, allowed: tuple[str, ...]) -> bool:
    """Whether a scoped ``NOPASSWD`` list covers this command."""
    if command in allowed:
        return True
    try:
        argv0 = shlex.split(command)[0]
    except (ValueError, IndexError):  # pragma: no cover
        return False
    return argv0 in allowed


def _may_write(
    node: SimulatedNode, user: SimulatedUser, path: str, as_root: bool
) -> bool:
    """A crude but honest permission model: home and /tmp, or be root."""
    if as_root or user.uid == 0:
        return True
    home = node.home(user.name)
    return path.startswith(home + "/") or path == home or path.startswith("/tmp/")


async def _stop_unit(unit: UnitState) -> None:
    task = unit.task
    unit.task = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# ── Running the real agent, in this process ─────────────────────────────────


class InProcessAgentRunner:
    """Runs the *real* agent for a simulated node.

    ``--help`` answers like the launcher does, which is what the installer's
    bundle verification uses. ``--enroll-only`` goes through
    :func:`spark_pulse.agent.__main__._run` unchanged — so the loud refusal of
    an existing identity, the exit code 2, and the token-file handling are the
    agent's own code and not a simulation of it. Starting the unit constructs a
    :class:`NodeAgent` against a private mock Docker service and lets it dial
    the control plane for real.
    """

    def __init__(self, docker_factory: Callable[[], Any] | None = None):
        self._docker_factory = docker_factory
        self._agents: dict[str, Any] = {}

    def agent_for(self, host: str):
        """The running :class:`NodeAgent` for a node, if it started one."""
        return self._agents.get(host)

    async def __call__(
        self,
        node: SimulatedNode,
        user: SimulatedUser,
        argv: list[str],
        *,
        background: bool,
    ) -> RunResult:
        from spark_pulse.agent.__main__ import build_parser, _run

        if "--help" in argv or "-h" in argv:
            return RunResult(0, "usage: python -m spark_pulse.agent\n", "")

        mapped = _map_paths(node, user, argv)
        parser = build_parser()
        try:
            args = parser.parse_args(mapped)
        except SystemExit as exc:  # pragma: no cover - argparse rejects
            return RunResult(int(exc.code or 2), "", "argument error")

        if not background:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = await _run(args)
            return RunResult(code, out.getvalue(), err.getvalue())

        return await self._start(node, args)

    async def _start(self, node: SimulatedNode, args) -> RunResult:
        from spark_pulse.agent.executor import LocalExecutor
        from spark_pulse.agent.node_agent import NodeAgent
        from spark_pulse.agent.store import AgentIdentity

        try:
            identity = AgentIdentity.load(args.dir)
        except RuntimeError as exc:
            return RunResult(1, "", str(exc))
        if identity is None:
            return RunResult(
                2, "", f"This node has no identity at {args.dir}, so it must enroll."
            )
        docker = self._docker_factory() if self._docker_factory else _default_docker()
        agent = NodeAgent(identity, args.control, executor=LocalExecutor(docker))
        task = asyncio.create_task(
            agent.run_forever(), name=f"simulated-agent-{node.host}"
        )
        self._agents[node.host] = agent
        node._last_agent_task = task  # picked up by the unit that started it
        return RunResult(0, "", "")


def _default_docker():
    from spark_pulse.mock.docker import MockDockerClient, MockDockerService

    return MockDockerService(MockDockerClient())


def _map_paths(node: SimulatedNode, user: SimulatedUser, argv: list[str]) -> list[str]:
    """Rewrite the agent's path arguments into this node's temporary root."""
    mapped = list(argv)
    for flag in ("--dir", "--trust-bundle", "--token-file"):
        if flag in mapped:
            index = mapped.index(flag) + 1
            if index < len(mapped):
                mapped[index] = str(node.path(mapped[index], user.name))
    return mapped


def _split_operators(script: str) -> list[tuple[str, str]]:
    """Split on top-level ``&&``, ``||`` and ``;``, keeping which joined what."""
    clauses: list[tuple[str, str]] = []
    current: list[str] = []
    operator = ""
    index = 0
    quote = ""
    while index < len(script):
        char = script[index]
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"":
            quote = char
            current.append(char)
            index += 1
            continue
        pair = script[index : index + 2]
        if pair in ("&&", "||"):
            clauses.append((operator, "".join(current).strip()))
            operator = pair
            current = []
            index += 2
            continue
        if char == ";":
            clauses.append((operator, "".join(current).strip()))
            operator = ";"
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    clauses.append((operator, "".join(current).strip()))
    return [(op, clause) for op, clause in clauses if clause]
