"""SSH transport abstraction for remote node operations.

Provides a unified interface for executing commands and copying files
to remote nodes via SSH. The default implementation uses OpenSSH subprocess.

Includes structured error classification for debugging SSH issues.

Can be swapped for Paramiko, AsyncSSH, or mock implementations without
changing orchestration code.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


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
    """Result of an SSH command execution."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether the command succeeded."""
        return self.returncode == 0


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
            batch_mode: Use StrictHostKeyChecking=no (non-interactive).

        Returns:
            SSHResult with returncode, stdout, stderr.
        """
        raise NotImplementedError("Subclasses must implement exec()")

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
        """SCP directory to remote host.

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
        user: str = "root",
        identity_file: str | None = None,
        strict_host_key_checking: bool = False,
    ):
        """Initialize OpenSSH client.

        Args:
            user: SSH username.
            identity_file: Path to SSH private key file.
            strict_host_key_checking: Disable host key verification.
        """
        self._user = user
        self._identity_file = identity_file
        self._strict_host_key_checking = strict_host_key_checking

    def _build_ssh_args(self, extra: list[str] | None = None) -> list[str]:
        """Build base SSH command arguments."""
        args = ["ssh", "-o", "BatchMode=yes"]
        if self._strict_host_key_checking:
            args.extend(["-o", "StrictHostKeyChecking=no"])
        if self._identity_file:
            args.extend(["-i", self._identity_file])
        if extra:
            args.extend(extra)
        return args

    def _build_scp_args(self) -> list[str]:
        """Build base SCP command arguments."""
        args = ["scp", "-o", "BatchMode=yes"]
        if self._strict_host_key_checking:
            args.extend(["-o", "StrictHostKeyChecking=no"])
        if self._identity_file:
            args.extend(["-i", self._identity_file])
        return args

    def exec(
        self,
        host: str,
        command: str,
        timeout: int = 30,
        batch_mode: bool = True,
    ) -> SSHResult:
        """Execute command on remote host via SSH."""
        user_host = f"{self._user}@{host}" if self._user else host
        args = self._build_ssh_args() + [user_host, command]

        logger.debug("Executing SSH command: %s", args)
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return SSHResult(
                returncode=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )
        except subprocess.TimeoutExpired:
            raise SSHError(
                error_type=SSHErrorType.TIMEOUT,
                host=host,
                message=f"Command '{command}' timed out after {timeout}s",
            )
        except subprocess.CalledProcessError as e:
            error_type = self._classify_ssh_error(e.returncode, e.stderr)
            raise SSHError(
                error_type=error_type,
                host=host,
                message=(
                    e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
                ),
                stderr=(
                    e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
                ),
            )
        except Exception as e:
            return SSHResult(
                returncode=-1,
                stdout="",
                stderr=str(e),
            )

    @staticmethod
    def _classify_ssh_error(returncode: int, stderr: str) -> SSHErrorType:
        """Classify SSH error based on return code and stderr."""
        stderr_lower = stderr.lower() if stderr else ""

        if "permission denied" in stderr_lower:
            if "publickey" in stderr_lower or "keyboard-interactive" in stderr_lower:
                return SSHErrorType.AUTH
            return SSHErrorType.PERMISSION_DENIED

        if "host key verification failed" in stderr_lower:
            return SSHErrorType.HOST_KEY

        if "connection timed out" in stderr_lower or "timed out" in stderr_lower:
            return SSHErrorType.TIMEOUT

        if "connection refused" in stderr_lower or "no route to host" in stderr_lower:
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
        user_host = f"{self._user}@{host}" if self._user else host
        args = self._build_scp_args() + [local_path, f"{user_host}:{remote_path}"]

        logger.debug("Executing SCP: %s", args)
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"SCP failed: {result.stderr}")

    def copy_dir(
        self,
        local_dir: str,
        host: str,
        remote_dir: str,
        timeout: int = 60,
    ) -> None:
        """SCP directory to remote host."""
        # Use rsync for directories if available, otherwise scp -r
        user_host = f"{self._user}@{host}" if self._user else host
        try:
            args = [
                "rsync",
                "-avz",
                "-e",
                " ".join(self._build_scp_args()),
                f"{local_dir}/",
                f"{user_host}:{remote_dir}/",
            ]
            logger.debug("Executing rsync: %s", args)
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                raise RuntimeError(f"rsync failed: {result.stderr}")
        except FileNotFoundError:
            # Fallback to scp -r
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
                raise RuntimeError(f"SCP -r failed: {result.stderr}")


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
