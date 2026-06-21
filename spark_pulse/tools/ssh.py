"""SSH transport abstraction for remote node operations.

Provides a unified interface for executing commands and copying files
to remote nodes via SSH. The default implementation uses OpenSSH subprocess.

Can be swapped for Paramiko, AsyncSSH, or mock implementations without
changing orchestration code.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


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
            return SSHResult(
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
            )
        except Exception as e:
            return SSHResult(
                returncode=-1,
                stdout="",
                stderr=str(e),
            )

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
                "rsync", "-avz", "-e",
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
