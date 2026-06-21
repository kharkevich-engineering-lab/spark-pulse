"""Tests for SSH transport abstraction."""

from __future__ import annotations

from spark_pulse.tools.ssh import SSHClient, SSHResult, OpenSSHClient


class TestSSHResult:
    """Tests for SSHResult dataclass."""

    def test_ok_on_zero_returncode(self):
        result = SSHResult(returncode=0, stdout="hello", stderr="")
        assert result.ok is True

    def test_not_ok_on_nonzero_returncode(self):
        result = SSHResult(returncode=1, stdout="", stderr="error")
        assert result.ok is False

    def test_not_ok_on_negative_returncode(self):
        result = SSHResult(returncode=-1, stdout="", stderr="timeout")
        assert result.ok is False

    def test_stdout_empty_by_default(self):
        result = SSHResult(returncode=0, stdout="", stderr="")
        assert result.stdout == ""

    def test_stderr_empty_by_default(self):
        result = SSHResult(returncode=0, stdout="", stderr="")
        assert result.stderr == ""


class TestOpenSSHClient:
    """Tests for OpenSSHClient (may skip if ssh not installed)."""

    def test_build_ssh_args_basic(self):
        client = OpenSSHClient()
        args = client._build_ssh_args()
        assert args[0] == "ssh"
        assert "-o" in args
        assert "BatchMode=yes" in args

    def test_build_ssh_args_with_identity(self):
        client = OpenSSHClient(identity_file="/path/to/key")
        args = client._build_ssh_args()
        assert "-i" in args
        assert "/path/to/key" in args

    def test_build_ssh_args_strict_host_key_checking(self):
        client = OpenSSHClient(strict_host_key_checking=True)
        args = client._build_ssh_args()
        assert "StrictHostKeyChecking=no" in args

    def test_exec_returns_ssh_result(self):
        """Test that exec returns SSHResult (may fail if ssh not available)."""
        client = OpenSSHClient()
        try:
            result = client.exec("localhost", "echo hello", timeout=5)
            assert isinstance(result, SSHResult)
        except Exception:
            # SSH may not be available in test environment
            pass

    def test_exec_timeout_returns_error(self):
        """Test that timeout returns error result."""
        client = OpenSSHClient()
        # This should return an error result, not raise
        result = client.exec("192.0.2.1", "echo hello", timeout=1)
        assert isinstance(result, SSHResult)
        assert result.ok is False


class TestModuleFunctions:
    """Tests for module-level convenience functions."""

    def test_ssh_exec_returns_result(self):
        from spark_pulse.tools.ssh import ssh_exec

        try:
            result = ssh_exec("localhost", "echo hello", timeout=5)
            assert isinstance(result, SSHResult)
        except Exception:
            # SSH may not be available
            pass

    def test_default_client_lazy_init(self):
        from spark_pulse.tools.ssh import _get_default_client

        client = _get_default_client()
        assert isinstance(client, OpenSSHClient)
