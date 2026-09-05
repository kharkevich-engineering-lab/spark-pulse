"""Mock SSH client for simulation mode.

Mirrors the real ssh.py API exactly for testing without real SSH access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The error vocabulary and the paths are shared: a caller reaching
# ``tools.ssh`` through the switch must catch the same exception type and read
# the same control-socket directory in either mode.
from spark_pulse.tools.ssh import (
    SSHError as SSHError,
    SSHErrorType as SSHErrorType,
    control_path_dir as control_path_dir,
    default_ssh_user as default_ssh_user,
    ensure_control_dir as ensure_control_dir,
)


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
    """Mock SSH client for simulation mode.

    Returns pre-configured responses instead of executing real commands.
    """

    def __init__(
        self,
        default_returncode: int = 0,
        default_stdout: str = "",
        default_stderr: str = "",
        fail_hosts: list[str] | None = None,
    ):
        """Initialize mock SSH client.

        Args:
            default_returncode: Return code for successful commands.
            default_stdout: Standard output for successful commands.
            default_stderr: Standard error for failed commands.
            fail_hosts: List of hosts that should always fail.
        """
        self._default_returncode = default_returncode
        self._default_stdout = default_stdout
        self._default_stderr = default_stderr
        self._fail_hosts = fail_hosts or []
        # This is a plain class, not a dataclass: ``dataclasses.field()`` here
        # produced a Field object, so the first ``exec()`` died on
        # ``Field.append``.
        self._executed_commands: list[dict[str, Any]] = []

    def exec(
        self,
        host: str,
        command: str,
        timeout: int = 30,
        batch_mode: bool = True,
    ) -> SSHResult:
        """Execute command on remote host (mocked)."""
        self._executed_commands.append(
            {
                "host": host,
                "command": command,
                "timeout": timeout,
            }
        )

        if host in self._fail_hosts:
            return SSHResult(
                returncode=1,
                stdout="",
                stderr=f"Connection refused to {host}",
            )

        # Special case: ray status
        if "ray status" in command:
            return SSHResult(
                returncode=0,
                stdout="Cluster is ready",
                stderr="",
            )

        # Special case: nvidia-smi
        if "nvidia-smi" in command:
            return SSHResult(
                returncode=0,
                stdout="1",
                stderr="",
            )

        # Special case: env
        if command == "env":
            return SSHResult(
                returncode=0,
                stdout="NCCL_SOCKET_IFNAME=eth0\nPATH=/usr/local/bin",
                stderr="",
            )

        return SSHResult(
            returncode=self._default_returncode,
            stdout=self._default_stdout,
            stderr=self._default_stderr,
        )

    def remote_shell_command(
        self, host: str, remote_command: str | None = None
    ) -> list[str]:
        """Argv that would run ``remote_command`` on ``host`` (mocked)."""
        args = ["ssh", "-o", "BatchMode=yes", host]
        if remote_command:
            args.append(remote_command)
        return args

    def copy(
        self,
        local_path: str,
        host: str,
        remote_path: str,
        timeout: int = 30,
    ) -> None:
        """SCP file to remote host (mocked)."""
        self._executed_commands.append(
            {
                "action": "copy",
                "local": local_path,
                "host": host,
                "remote": remote_path,
            }
        )

    def copy_dir(
        self,
        local_dir: str,
        host: str,
        remote_dir: str,
        timeout: int = 60,
    ) -> None:
        """SCP directory to remote host (mocked)."""
        self._executed_commands.append(
            {
                "action": "copy_dir",
                "local": local_dir,
                "host": host,
                "remote": remote_dir,
            }
        )

    @property
    def executed_commands(self) -> list[dict[str, Any]]:
        """Return list of all executed commands."""
        return self._executed_commands.copy()

    def reset(self) -> None:
        """Clear executed commands history."""
        self._executed_commands.clear()


#: The real module's concrete implementation is ``OpenSSHClient``; here there
#: is only one client and it is simulated, so the two names are the same class.
OpenSSHClient = SSHClient


# Module-level convenience functions
_default_client: SSHClient | None = None


def _get_default_client() -> SSHClient:
    """Get or create the default mock SSH client."""
    global _default_client
    if _default_client is None:
        _default_client = SSHClient()
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
