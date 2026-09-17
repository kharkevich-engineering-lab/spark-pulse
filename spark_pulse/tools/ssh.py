"""SSH transport abstraction for remote node operations.

Provides a unified interface for executing commands and copying files
to remote nodes via SSH. The default implementation uses OpenSSH subprocess.

Two properties matter more than the rest:

* **Unreachable is not the same as failed.** ``ssh`` exits 255 when it could
  not establish or keep the connection, and any other exit code is the remote
  command's own. ``exec`` therefore raises :class:`SSHError` for the former and
  returns an :class:`SSHResult` with a non-zero return code for the latter, so
  a caller never has to grep stderr to tell the two apart.
* **Connections are reused.** Every invocation carries ``ControlMaster=auto``
  and ``ControlPersist``, so a burst of remote operations pays one TCP and SSH
  handshake instead of one per command.

Can be swapped for Paramiko, AsyncSSH, or mock implementations without
changing orchestration code.
"""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import contextlib
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# ssh(1) reserves 255 for its own failures: it could not connect, could not
# authenticate, or the connection died. Every other non-zero code belongs to
# the remote command.
SSH_TRANSPORT_EXIT_CODE = 255

#: How the host key is verified. ``strict`` refuses an unknown or changed key,
#: ``accept-new`` trusts a first sighting but still refuses a changed one, and
#: ``off`` disables verification entirely. ``off`` is never a default and
#: ``accept-new`` only ever a deliberate bootstrap fallback.
HostKeyPolicy = Literal["strict", "accept-new", "off"]

_HOST_KEY_POLICY_OPTIONS: dict[str, str] = {
    "strict": "yes",
    "accept-new": "accept-new",
    "off": "no",
}

DEFAULT_HOST_KEY_POLICY: HostKeyPolicy = "strict"

# Connection reuse and liveness. ControlPersist keeps the master alive briefly
# after the last client so a burst of operations shares one handshake.
CONTROL_PERSIST = "60s"
CONNECT_TIMEOUT = 10
SERVER_ALIVE_INTERVAL = 15
SERVER_ALIVE_COUNT_MAX = 3

# sun_path is 104 bytes on macOS and 108 on Linux, and ssh fails obscurely when
# the control socket path overflows it. %C is a 40 character hash of the
# connection tuple, which is why it is used instead of the much longer
# %r@%h:%p. The budget is the macOS 104, less the NUL, less the ".<pid>"
# suffix ssh appends while the master is being set up.
CONTROL_PATH_TEMPLATE = "cm-%C"
_CONTROL_PATH_HASH_LEN = 40
_MAX_CONTROL_PATH_LEN = 104 - 1 - len(".") - 8

_CONTROL_DIR_ENV = "SPARK_PULSE_SSH_CONTROL_DIR"


class SSHErrorType(str, Enum):
    """Classification of SSH errors."""

    AUTH = "auth"  # Invalid credentials, key rejected
    TIMEOUT = "timeout"  # Connection timed out
    NETWORK = "network"  # Host unreachable, connection refused
    HOST_KEY = "host_key"  # Host key verification failed
    PERMISSION_DENIED = "permission_denied"  # Auth succeeded but command denied
    UNKNOWN = "unknown"  # Unclassified error


@dataclass(frozen=True)
class SSHError(Exception):
    """Structured SSH error with classification.

    Raised only for transport failures: the node could not be reached, the
    connection died, or authentication was refused. A remote command that ran
    and exited non-zero comes back as an :class:`SSHResult` instead.

    Note: Not using slots=True to maintain Python 3.14 compatibility.
    Dataclass-based exceptions with slots=True cause TypeError in Python 3.14
    due to stricter exception type checking in the CPython exception handling.
    """

    error_type: SSHErrorType
    host: str
    message: str
    stderr: str = ""

    def __str__(self) -> str:
        return f"SSHError({self.error_type.value}: {self.host} - {self.message})"


@dataclass(frozen=True, slots=True)
class SSHResult:
    """Result of an SSH command execution.

    The return code is the remote command's: reaching this object at all means
    the node was reachable and the outcome is definite.
    """

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether the command succeeded."""
        return self.returncode == 0


def default_ssh_user() -> str:
    """The account SSH should use when the caller names none.

    The current user, never root: Ubuntu 24.04 defaults ``PermitRootLogin`` to
    ``prohibit-password`` and DGX OS onboarding creates a normal sudo user.
    """
    try:
        return getpass.getuser()
    except (KeyError, OSError):  # pragma: no cover - no passwd entry, no env
        return os.environ.get("USER") or ""


def control_path_dir() -> Path:
    """Directory we own for SSH multiplexing sockets."""
    override = os.environ.get(_CONTROL_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "spark-pulse" / "ssh"


def _fits_socket_limit(directory: Path) -> bool:
    """Whether ``<directory>/cm-<hash>`` stays inside the sun_path limit."""
    length = len(str(directory)) + 1 + len(CONTROL_PATH_TEMPLATE) - 2
    return length + _CONTROL_PATH_HASH_LEN <= _MAX_CONTROL_PATH_LEN


def _short_fallback_dir() -> Path:
    """A short, private directory for when the configured one is too long.

    ``/tmp`` by name rather than :func:`tempfile.gettempdir`, because macOS
    points TMPDIR at a per-user path long enough to blow the socket limit on
    its own, which is the very thing this fallback exists to avoid.
    """
    uid = getattr(os, "getuid", lambda: 0)()
    root = Path("/tmp") if Path("/tmp").is_dir() else Path(tempfile.gettempdir())
    return root / f"sp-ssh-{uid}"


def ensure_control_dir() -> Path | None:
    """Create the multiplexing socket directory 0700, or give up quietly.

    Returns ``None`` when no usable directory could be prepared, in which case
    the client simply runs without multiplexing rather than failing.
    """
    for candidate in (control_path_dir(), _short_fallback_dir()):
        if not _fits_socket_limit(candidate):
            logger.debug("SSH control path %s exceeds the socket limit", candidate)
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            candidate.chmod(0o700)
        except OSError as exc:
            logger.debug("Cannot use SSH control dir %s: %s", candidate, exc)
            continue
        return candidate
    logger.debug("SSH connection multiplexing disabled: no usable control dir")
    return None


# ── The control plane's own known_hosts ──────────────────────────────────────
#
# Bootstrap already verifies a node's host key: the browser shows the
# fingerprint, the operator confirms it, and ``agent.bootstrap.install_agent``
# refuses to send a byte if the node then offers a different one. That
# verification lived entirely inside AsyncSSH's connection, so OpenSSH — which
# is what rsync, scp and every later bulk transfer run under — had never heard
# of the node and refused it under ``StrictHostKeyChecking=yes``. The
# workaround on a real cluster was ``ssh-keyscan``, which trusts whatever
# answers: the opposite of what had just been confirmed.
#
# So the confirmed key is written here, into a file this control plane owns.
# **Ours is the only ``UserKnownHostsFile``** for a client that asks for it,
# rather than ours plus the user's: OpenSSH takes several files only as one
# space-separated value, and rsync splits its ``-e`` argument on whitespace, so
# a two-file value cannot survive the one transport that matters most. Owning
# the file outright is also the better boundary — it trusts exactly the nodes
# this control plane onboarded and confirmed, not whatever a personal
# ``~/.ssh/known_hosts`` has accumulated.

#: The file, beside the multiplexing sockets in the directory we already own.
KNOWN_HOSTS_NAME = "known_hosts"

_KNOWN_HOSTS_LOCK = threading.Lock()


def known_hosts_path() -> Path:
    """Where this control plane records the host keys it has confirmed."""
    return control_path_dir() / KNOWN_HOSTS_NAME


def host_pattern(host: str, port: int = 22) -> str:
    """The known_hosts hostname field for ``host``: ``[host]:port`` off 22."""
    host = (host or "").strip()
    return host if port == 22 else f"[{host}]:{port}"


def _entry_key(entry: str) -> str:
    """The algorithm half of a ``<algorithm> <base64>`` key, for replacement."""
    return (entry or "").split(" ", 1)[0]


def _read_known_hosts() -> list[str]:
    try:
        return known_hosts_path().read_text().splitlines()
    except OSError:
        return []


def _write_known_hosts(lines: list[str]) -> None:
    """Replace the file, 0600, through a temp file in the same directory."""
    path = known_hosts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:  # pragma: no cover - a directory somebody else owns
        pass
    body = "\n".join(lines) + ("\n" if lines else "")
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(body)
    tmp.chmod(0o600)
    os.replace(tmp, path)


def trusted_entries(host: str, port: int = 22) -> list[str]:
    """The ``<algorithm> <base64>`` keys this control plane trusts for ``host``."""
    pattern = host_pattern(host, port)
    found = []
    for line in _read_known_hosts():
        name, _, key = line.strip().partition(" ")
        if name == pattern and key:
            found.append(key)
    return found


def trust_host_key(host: str, entry: str, port: int = 22) -> bool:
    """Record ``entry`` as a host key to trust for ``host``.

    ``entry`` is the ``<algorithm> <base64>`` half of a known_hosts line — what
    :attr:`~spark_pulse.agent.bootstrap_transport.HostKey.openssh` returns for
    the key an operator has just confirmed. Never a key from a blind scan:
    every caller here has verified the fingerprint first.

    Idempotent. A key for an algorithm already recorded is replaced, with a
    warning — that is a node whose identity changed, and reaching this call at
    all means somebody confirmed the new fingerprint by hand.

    Returns:
        Whether the file changed.
    """
    entry = (entry or "").strip()
    if not host or not entry or " " not in entry:
        raise ValueError(f"not a host key entry: {entry!r}")
    pattern = host_pattern(host, port)
    algorithm = _entry_key(entry)
    line = f"{pattern} {entry}"
    with _KNOWN_HOSTS_LOCK:
        lines = _read_known_hosts()
        kept: list[str] = []
        changed = True
        for existing in lines:
            name, _, key = existing.strip().partition(" ")
            if name == pattern and _entry_key(key) == algorithm:
                if key == entry:
                    changed = False
                else:
                    logger.warning(
                        "%s now offers a different %s host key; replacing the "
                        "one recorded for it",
                        pattern,
                        algorithm,
                    )
                continue
            kept.append(existing)
        if not changed:
            return False
        kept.append(line)
        _write_known_hosts(kept)
    logger.info("Trusting the confirmed %s host key for %s", algorithm, pattern)
    return True


def trust_alias(known_host: str, alias: str, port: int = 22) -> int:
    """Trust ``known_host``'s recorded keys under ``alias`` as well.

    One machine, two addresses — a node's management address and the fabric
    address the control plane configured for it — and the same host key on
    both. Copying what is already recorded is what keeps the fabric transfer
    under strict checking without a second scan of anything.

    Returns:
        How many entries were added.
    """
    if not known_host or not alias or known_host == alias:
        return 0
    added = 0
    for entry in trusted_entries(known_host, port):
        if trust_host_key(alias, entry, port):
            added += 1
    return added


class SSHClient:
    """SSH transport abstraction.

    Default implementation uses OpenSSH subprocess (ssh/scp).
    Can be swapped for Paramiko, AsyncSSH, or mock implementations.
    """

    def exec(
        self,
        host: str,
        command: str,
        timeout: int = 30,
        batch_mode: bool = True,
    ) -> SSHResult:
        """Execute command on remote host via SSH.

        Args:
            host: Remote host IP or hostname.
            command: Shell command to execute.
            timeout: Seconds before killing the command.
            batch_mode: Refuse any interactive prompt.

        Returns:
            SSHResult with the remote command's returncode, stdout and stderr.

        Raises:
            SSHError: The node was not reachable, or the connection failed.
        """
        raise NotImplementedError("Subclasses must implement exec()")

    def remote_shell_command(
        self, host: str, remote_command: str | None = None
    ) -> list[str]:
        """Argv that runs ``remote_command`` on ``host``, for piping into.

        Callers that need to stream bytes through SSH (``docker save | ssh …
        docker load``) build their pipeline from this rather than hand-rolling
        an ``ssh`` invocation, so the identity file and host key policy still
        apply.
        """
        raise NotImplementedError("Subclasses must implement remote_shell_command()")

    def copy(
        self,
        local_path: str,
        host: str,
        remote_path: str,
        timeout: int = 30,
    ) -> None:
        """SCP file to remote host.

        Args:
            local_path: Local file path.
            host: Remote host IP or hostname.
            remote_path: Remote destination path.
            timeout: Seconds before killing the transfer.
        """
        raise NotImplementedError("Subclasses must implement copy()")

    def copy_dir(
        self,
        local_dir: str,
        host: str,
        remote_dir: str,
        timeout: int = 60,
    ) -> None:
        """Copy a directory tree to a remote host.

        Args:
            local_dir: Local directory path.
            host: Remote host IP or hostname.
            remote_dir: Remote destination directory.
            timeout: Seconds before killing the transfer.
        """
        raise NotImplementedError("Subclasses must implement copy_dir()")


class OpenSSHClient(SSHClient):
    """Default implementation using OpenSSH subprocess."""

    def __init__(
        self,
        user: str | None = None,
        identity_file: str | None = None,
        host_key_policy: HostKeyPolicy = DEFAULT_HOST_KEY_POLICY,
        multiplex: bool = True,
        known_hosts_file: str | None = None,
    ):
        """Initialize OpenSSH client.

        Args:
            user: SSH username. ``None`` means the current user; an empty
                string means none at all, leaving the choice to ssh_config.
            identity_file: Path to SSH private key file.
            host_key_policy: ``strict`` (refuse unknown or changed keys),
                ``accept-new`` (trust on first use, still refuse a change), or
                ``off`` (no verification, for tests and throwaway hosts only).
            multiplex: Reuse one connection across commands.
            known_hosts_file: The file to verify host keys against, replacing
                ssh's default. :func:`known_hosts_path` is what the control
                plane's own transfers pass — the keys bootstrap confirmed.
                ``None`` leaves ssh's default in place. Never a path with a
                space in it: rsync splits the ``-e`` argument on whitespace.

        Raises:
            ValueError: host_key_policy is not one of the three values.
        """
        if host_key_policy not in _HOST_KEY_POLICY_OPTIONS:
            raise ValueError(
                f"Unknown host_key_policy {host_key_policy!r}; expected one of "
                f"{sorted(_HOST_KEY_POLICY_OPTIONS)}"
            )
        self._user = default_ssh_user() if user is None else user
        self._identity_file = identity_file
        self._host_key_policy: HostKeyPolicy = host_key_policy
        self._known_hosts_file = known_hosts_file
        self._control_path: str | None = None
        if multiplex:
            control_dir = ensure_control_dir()
            if control_dir is not None:
                self._control_path = str(control_dir / CONTROL_PATH_TEMPLATE)

    @property
    def host_key_policy(self) -> HostKeyPolicy:
        """The configured host key policy."""
        return self._host_key_policy

    @property
    def known_hosts_file(self) -> str | None:
        """The known_hosts file every invocation verifies against, if any."""
        return self._known_hosts_file

    @property
    def control_path(self) -> str | None:
        """The multiplexing socket path, or None when multiplexing is off."""
        return self._control_path

    def _common_options(self) -> list[str]:
        """Options shared by ssh, scp and the rsync remote shell."""
        args = [
            "-o",
            "BatchMode=yes",
            "-o",
            f"StrictHostKeyChecking={_HOST_KEY_POLICY_OPTIONS[self._host_key_policy]}",
            "-o",
            f"ConnectTimeout={CONNECT_TIMEOUT}",
            "-o",
            f"ServerAliveInterval={SERVER_ALIVE_INTERVAL}",
            "-o",
            f"ServerAliveCountMax={SERVER_ALIVE_COUNT_MAX}",
        ]
        if self._known_hosts_file:
            args.extend(["-o", f"UserKnownHostsFile={self._known_hosts_file}"])
        if self._control_path:
            args.extend(
                [
                    "-o",
                    "ControlMaster=auto",
                    "-o",
                    f"ControlPath={self._control_path}",
                    "-o",
                    f"ControlPersist={CONTROL_PERSIST}",
                ]
            )
        if self._identity_file:
            # Pin to exactly this key. Without IdentitiesOnly, ssh also offers
            # every key in the agent and in ~/.ssh first, and a control plane
            # with several can trip MaxAuthTries before it reaches the one the
            # node actually trusts.
            args.extend(["-o", "IdentitiesOnly=yes", "-i", self._identity_file])
        return args

    @contextlib.contextmanager
    def reverse_tunnel(self, host: str, remote_port: int, local_port: int):
        """Expose a local port on ``host`` as ``127.0.0.1:remote_port``.

        This exists because Docker refuses a plain-HTTP registry unless the
        address is loopback: the daemon's insecure defaults are exactly
        ``127.0.0.0/8`` and ``::1/128``, verified on a DGX Spark. Reaching the
        control node's registry through a reverse tunnel therefore needs no
        change to any node's ``daemon.json`` and no restart of a daemon that
        may be running deployments, which writing that file would require.

        Yields once the forward is listening, and closes it on exit.
        """
        # Own the connection rather than multiplexing: over a shared control
        # master ssh hands the forward to the master and exits at once, so the
        # tunnel would outlive this context manager and never be closed.
        args = self._build_ssh_args(
            [
                "-N",
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPath=none",
                "-R",
                f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}",
            ]
        )
        args.append(self._user_host(host))
        logger.debug("Opening reverse tunnel to %s: %s", host, args)
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        try:
            # ssh exits immediately on a bind clash or auth failure; give it a
            # moment and surface that rather than letting the caller time out
            # against a port nothing is listening on.
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            else:
                stderr = (proc.stderr.read() if proc.stderr else b"").decode(
                    "utf-8", "replace"
                )
                raise SSHError(
                    self._classify_ssh_error(proc.returncode, stderr),
                    host,
                    f"reverse tunnel failed: {stderr.strip() or 'ssh exited'}",
                    stderr,
                )
            yield remote_port
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                proc.kill()

    def _build_ssh_args(self, extra: list[str] | None = None) -> list[str]:
        """Build base SSH command arguments."""
        args = ["ssh"] + self._common_options()
        if extra:
            args.extend(extra)
        return args

    def _build_scp_args(self) -> list[str]:
        """Build base SCP command arguments."""
        return ["scp"] + self._common_options()

    def _user_host(self, host: str) -> str:
        return f"{self._user}@{host}" if self._user else host

    def remote_shell_command(
        self, host: str, remote_command: str | None = None
    ) -> list[str]:
        """Argv running ``remote_command`` on ``host`` with our SSH options."""
        args = self._build_ssh_args() + [self._user_host(host)]
        if remote_command:
            args.append(remote_command)
        return args

    def exec(
        self,
        host: str,
        command: str,
        timeout: int = 30,
        batch_mode: bool = True,
    ) -> SSHResult:
        """Execute command on remote host via SSH.

        Returns the remote command's result. A transport failure — exit 255
        from ssh itself — raises :class:`SSHError` instead, so "the node is
        unreachable" is never confused with "the command failed".
        """
        args = self.remote_shell_command(host, command)

        logger.debug("Executing SSH command: %s", args)
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise SSHError(
                error_type=SSHErrorType.TIMEOUT,
                host=host,
                message=f"Command '{command}' timed out after {timeout}s",
            )
        except OSError as exc:
            return SSHResult(returncode=-1, stdout="", stderr=str(exc))

        stderr = result.stderr or ""
        self._raise_if_transport_failure(host, result.returncode, stderr)
        return SSHResult(
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=stderr,
        )

    @staticmethod
    def _raise_if_transport_failure(host: str, returncode: int, stderr: str) -> None:
        """Raise SSHError when ssh reports it could not run the command."""
        if returncode != SSH_TRANSPORT_EXIT_CODE:
            return
        error_type = OpenSSHClient._classify_ssh_error(returncode, stderr)
        message = stderr.strip() or f"SSH transport failure connecting to {host}"
        raise SSHError(
            error_type=error_type,
            host=host,
            message=message,
            stderr=stderr,
        )

    @staticmethod
    def _classify_ssh_error(returncode: int, stderr: str) -> SSHErrorType:
        """Classify SSH error based on return code and stderr."""
        stderr_lower = stderr.lower() if stderr else ""

        if (
            "host key verification failed" in stderr_lower
            or "remote host identification has changed" in stderr_lower
            or ("host key for" in stderr_lower and "changed" in stderr_lower)
        ):
            return SSHErrorType.HOST_KEY

        if "permission denied" in stderr_lower:
            if "publickey" in stderr_lower or "keyboard-interactive" in stderr_lower:
                return SSHErrorType.AUTH
            return SSHErrorType.PERMISSION_DENIED

        if "connection timed out" in stderr_lower or "timed out" in stderr_lower:
            return SSHErrorType.TIMEOUT

        if (
            "connection refused" in stderr_lower
            or "no route to host" in stderr_lower
            or "could not resolve hostname" in stderr_lower
            or "network is unreachable" in stderr_lower
            or "connection closed by remote host" in stderr_lower
        ):
            return SSHErrorType.NETWORK

        return SSHErrorType.UNKNOWN

    def copy(
        self,
        local_path: str,
        host: str,
        remote_path: str,
        timeout: int = 30,
    ) -> None:
        """SCP file to remote host."""
        args = self._build_scp_args() + [
            local_path,
            f"{self._user_host(host)}:{remote_path}",
        ]

        logger.debug("Executing SCP: %s", args)
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr or ""
            self._raise_if_transport_failure(host, result.returncode, stderr)
            raise RuntimeError(f"SCP failed: {stderr}")

    def _rsync_remote_shell(self) -> str:
        """The ``-e`` argument for rsync: a real ssh command, not scp.

        rsync splits this on whitespace itself, so the options must not contain
        any. None of ours do.
        """
        return " ".join(self._build_ssh_args())

    def copy_dir(
        self,
        local_dir: str,
        host: str,
        remote_dir: str,
        timeout: int = 60,
    ) -> None:
        """Copy a directory tree to a remote host, rsync first then scp -r."""
        user_host = self._user_host(host)
        if shutil.which("rsync"):
            args = [
                "rsync",
                "-a",
                # These are immutable, content-addressed blobs on a fast link:
                # the delta algorithm only costs CPU, while --partial keeps a
                # half-finished multi-gigabyte transfer resumable.
                "-W",
                "--partial",
                "-e",
                self._rsync_remote_shell(),
                f"{local_dir}/",
                f"{user_host}:{remote_dir}/",
            ]
            logger.debug("Executing rsync: %s", args)
            try:
                result = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except FileNotFoundError:  # rsync vanished between which() and run
                result = None
            if result is not None:
                if result.returncode != 0:
                    stderr = result.stderr or ""
                    self._raise_if_transport_failure(host, result.returncode, stderr)
                    raise RuntimeError(f"rsync failed: {stderr}")
                return

        # Fallback: no rsync on this control node.
        args = self._build_scp_args() + [
            "-r",
            f"{local_dir}/",
            f"{user_host}:{remote_dir}/",
        ]
        logger.debug("Executing scp -r: %s", args)
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr or ""
            self._raise_if_transport_failure(host, result.returncode, stderr)
            raise RuntimeError(f"SCP -r failed: {stderr}")


# ── Module-level convenience functions ──────────────────────────────────────

_default_client: SSHClient | None = None


def _get_default_client() -> SSHClient:
    """Get or create the default SSH client."""
    global _default_client
    if _default_client is None:
        _default_client = OpenSSHClient()
    return _default_client


def ssh_exec(
    host: str,
    command: str,
    timeout: int = 30,
) -> SSHResult:
    """Execute command on remote host via SSH using default client."""
    return _get_default_client().exec(host, command, timeout)


def ssh_copy(
    local_path: str,
    host: str,
    remote_path: str,
    timeout: int = 30,
) -> None:
    """SCP file to remote host using default client."""
    _get_default_client().copy(local_path, host, remote_path, timeout)


def ssh_copy_dir(
    local_dir: str,
    host: str,
    remote_dir: str,
    timeout: int = 60,
) -> None:
    """SCP directory to remote host using default client."""
    _get_default_client().copy_dir(local_dir, host, remote_dir, timeout)
