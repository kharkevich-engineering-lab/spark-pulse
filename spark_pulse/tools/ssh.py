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
import subprocess
import tempfile
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
            args.extend(["-i", self._identity_file])
        return args

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
