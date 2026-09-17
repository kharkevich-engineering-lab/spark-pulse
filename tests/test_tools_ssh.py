"""Tests for SSH transport abstraction."""

from __future__ import annotations

import getpass
import logging
import importlib
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from spark_pulse.tools.ssh import SSHResult, OpenSSHClient, SSHError, SSHErrorType

# In simulation mode ``spark_pulse.tools.ssh`` as an attribute is the mock;
# import_module returns the real submodule the from-import above also uses.
ssh_mod = importlib.import_module("spark_pulse.tools.ssh")


@pytest.fixture
def control_dir(tmp_path, monkeypatch):
    """Point multiplexing sockets at a directory the test owns.

    The socket length guard is lifted for the duration, because pytest's own
    tmp_path is long enough on macOS to trip it and that is a separate test.
    """
    target = tmp_path / "ssh"
    monkeypatch.setenv("SPARK_PULSE_SSH_CONTROL_DIR", str(target))
    monkeypatch.setattr(ssh_mod, "_MAX_CONTROL_PATH_LEN", 4096)
    return target


class _FakeRun:
    """Records the argv it was handed and replays a canned result."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.calls: list[list[str]] = []
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(
            args, self._returncode, self._stdout, self._stderr
        )

    @property
    def argv(self) -> list[str]:
        assert self.calls, "subprocess.run was never called"
        return self.calls[-1]


def _option(argv: list[str], name: str) -> str | None:
    """Value of the first ``-o <name>=<value>`` in argv."""
    for flag, value in zip(argv, argv[1:]):
        if flag == "-o" and value.startswith(f"{name}="):
            return value.split("=", 1)[1]
    return None


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


class TestArgumentBuilding:
    """The built argv is the contract; assert on it directly."""

    def test_build_ssh_args_basic(self, control_dir):
        args = OpenSSHClient()._build_ssh_args()
        assert args[0] == "ssh"
        assert _option(args, "BatchMode") == "yes"

    def test_build_ssh_args_with_identity(self, control_dir):
        args = OpenSSHClient(identity_file="/path/to/key")._build_ssh_args()
        assert "-i" in args
        assert args[args.index("-i") + 1] == "/path/to/key"

    def test_scp_carries_the_same_options(self, control_dir):
        client = OpenSSHClient(identity_file="/path/to/key")
        scp = client._build_scp_args()
        assert scp[0] == "scp"
        assert _option(scp, "StrictHostKeyChecking") == "yes"
        assert _option(scp, "ControlMaster") == "auto"
        assert _option(scp, "ConnectTimeout") == str(ssh_mod.CONNECT_TIMEOUT)

    def test_remote_shell_command_targets_user_at_host(self, control_dir):
        client = OpenSSHClient(user="ubuntu")
        argv = client.remote_shell_command("10.0.0.5", "docker load")
        assert argv[0] == "ssh"
        assert argv[-2:] == ["ubuntu@10.0.0.5", "docker load"]
        assert _option(argv, "StrictHostKeyChecking") == "yes"

    def test_remote_shell_command_without_a_command(self, control_dir):
        argv = OpenSSHClient(user="ubuntu").remote_shell_command("10.0.0.5")
        assert argv[-1] == "ubuntu@10.0.0.5"


class TestHostKeyPolicy:
    """The flag used to be inverted: True meant StrictHostKeyChecking=no."""

    @pytest.mark.parametrize(
        "policy,expected",
        [
            ("strict", "yes"),
            ("accept-new", "accept-new"),
            ("off", "no"),
        ],
    )
    def test_policy_maps_to_the_ssh_option(self, control_dir, policy, expected):
        args = OpenSSHClient(host_key_policy=policy)._build_ssh_args()
        assert _option(args, "StrictHostKeyChecking") == expected

    def test_default_is_strict(self, control_dir):
        client = OpenSSHClient()
        assert client.host_key_policy == "strict"
        args = client._build_ssh_args()
        assert _option(args, "StrictHostKeyChecking") == "yes"
        # The regression: strict must never emit the disabling value.
        assert "StrictHostKeyChecking=no" not in args

    def test_unknown_policy_is_refused(self, control_dir):
        with pytest.raises(ValueError):
            OpenSSHClient(host_key_policy="yes-please")

    def test_policy_applies_to_scp_too(self, control_dir):
        args = OpenSSHClient(host_key_policy="accept-new")._build_scp_args()
        assert _option(args, "StrictHostKeyChecking") == "accept-new"


class TestConnectionReuse:
    def test_multiplexing_options_are_present(self, control_dir):
        args = OpenSSHClient()._build_ssh_args()
        assert _option(args, "ControlMaster") == "auto"
        assert _option(args, "ControlPersist") == ssh_mod.CONTROL_PERSIST

    def test_control_path_lives_in_our_directory(self, control_dir):
        client = OpenSSHClient()
        path = _option(client._build_ssh_args(), "ControlPath")
        assert path == str(control_dir / "cm-%C")
        assert client.control_path == path

    def test_control_directory_is_created_private(self, control_dir):
        OpenSSHClient()
        assert control_dir.is_dir()
        assert control_dir.stat().st_mode & 0o777 == 0o700

    def test_keepalive_and_connect_timeout_are_set(self, control_dir):
        args = OpenSSHClient()._build_ssh_args()
        assert _option(args, "ConnectTimeout") == str(ssh_mod.CONNECT_TIMEOUT)
        assert _option(args, "ServerAliveInterval") == str(
            ssh_mod.SERVER_ALIVE_INTERVAL
        )
        assert _option(args, "ServerAliveCountMax") == str(
            ssh_mod.SERVER_ALIVE_COUNT_MAX
        )

    def test_an_overlong_directory_falls_back_to_a_short_one(
        self, tmp_path, monkeypatch
    ):
        long_dir = tmp_path / ("d" * 120)
        monkeypatch.setenv("SPARK_PULSE_SSH_CONTROL_DIR", str(long_dir))
        chosen = ssh_mod.ensure_control_dir()
        assert chosen is not None
        assert chosen != long_dir
        assert not long_dir.exists()
        assert ssh_mod._fits_socket_limit(chosen)

    def test_multiplexing_can_be_turned_off(self, control_dir):
        client = OpenSSHClient(multiplex=False)
        assert client.control_path is None
        assert _option(client._build_ssh_args(), "ControlPath") is None

    def test_no_control_dir_means_no_multiplexing(self, control_dir, monkeypatch):
        monkeypatch.setattr(ssh_mod, "ensure_control_dir", lambda: None)
        args = OpenSSHClient()._build_ssh_args()
        assert _option(args, "ControlMaster") is None
        assert _option(args, "BatchMode") == "yes"


class TestDefaultUser:
    """root cannot log in on Ubuntu 24.04 or DGX OS."""

    def test_default_user_is_not_root(self, control_dir):
        argv = OpenSSHClient().remote_shell_command("10.0.0.5", "true")
        assert not argv[-2].startswith("root@")

    def test_default_user_is_the_current_user(self, control_dir):
        argv = OpenSSHClient().remote_shell_command("10.0.0.5", "true")
        assert argv[-2] == f"{getpass.getuser()}@10.0.0.5"

    def test_empty_user_leaves_the_choice_to_ssh_config(self, control_dir):
        argv = OpenSSHClient(user="").remote_shell_command("10.0.0.5", "true")
        assert argv[-2] == "10.0.0.5"


class TestExecClassification:
    """Unreachable and command-failed must be structurally different."""

    def _client(self):
        return OpenSSHClient(user="ubuntu")

    def test_success_returns_a_result(self, control_dir, monkeypatch):
        run = _FakeRun(returncode=0, stdout="hello\n")
        monkeypatch.setattr(ssh_mod.subprocess, "run", run)

        result = self._client().exec("10.0.0.5", "echo hello")

        assert result.ok is True
        assert result.stdout == "hello\n"
        assert run.argv[-2:] == ["ubuntu@10.0.0.5", "echo hello"]

    def test_a_failing_remote_command_is_not_unreachable(
        self, control_dir, monkeypatch
    ):
        run = _FakeRun(returncode=1, stderr="No such image")
        monkeypatch.setattr(ssh_mod.subprocess, "run", run)

        result = self._client().exec("10.0.0.5", "docker image inspect nope")

        assert result.returncode == 1
        assert result.ok is False
        assert result.stderr == "No such image"

    def test_any_non_transport_exit_code_is_a_command_failure(
        self, control_dir, monkeypatch
    ):
        run = _FakeRun(returncode=127, stderr="command not found")
        monkeypatch.setattr(ssh_mod.subprocess, "run", run)

        result = self._client().exec("10.0.0.5", "nope")

        assert result.returncode == 127

    def test_host_key_mismatch_raises_host_key(self, control_dir, monkeypatch):
        stderr = (
            "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
            "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\n"
            "Host key verification failed.\n"
        )
        monkeypatch.setattr(
            ssh_mod.subprocess, "run", _FakeRun(returncode=255, stderr=stderr)
        )

        with pytest.raises(SSHError) as excinfo:
            self._client().exec("10.0.0.5", "true")

        error = excinfo.value
        assert error.error_type == SSHErrorType.HOST_KEY
        assert error.host == "10.0.0.5"
        assert "IDENTIFICATION HAS CHANGED" in error.stderr

    def test_permission_denied_raises_auth(self, control_dir, monkeypatch):
        monkeypatch.setattr(
            ssh_mod.subprocess,
            "run",
            _FakeRun(returncode=255, stderr="ubuntu@h: Permission denied (publickey)."),
        )

        with pytest.raises(SSHError) as excinfo:
            self._client().exec("10.0.0.5", "true")

        assert excinfo.value.error_type == SSHErrorType.AUTH

    def test_connection_refused_raises_network(self, control_dir, monkeypatch):
        monkeypatch.setattr(
            ssh_mod.subprocess,
            "run",
            _FakeRun(returncode=255, stderr="ssh: connect to host: Connection refused"),
        )

        with pytest.raises(SSHError) as excinfo:
            self._client().exec("10.0.0.5", "true")

        assert excinfo.value.error_type == SSHErrorType.NETWORK

    def test_transport_failure_without_stderr_still_raises(
        self, control_dir, monkeypatch
    ):
        monkeypatch.setattr(
            ssh_mod.subprocess, "run", _FakeRun(returncode=255, stderr="")
        )

        with pytest.raises(SSHError) as excinfo:
            self._client().exec("10.0.0.5", "true")

        assert excinfo.value.error_type == SSHErrorType.UNKNOWN
        assert "10.0.0.5" in excinfo.value.message

    def test_timeout_raises_timeout(self, control_dir, monkeypatch):
        def _boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=1)

        monkeypatch.setattr(ssh_mod.subprocess, "run", _boom)

        with pytest.raises(SSHError) as excinfo:
            self._client().exec("10.0.0.5", "true", timeout=1)

        assert excinfo.value.error_type == SSHErrorType.TIMEOUT
        assert "timed out" in excinfo.value.message.lower()

    def test_a_missing_ssh_binary_is_reported_not_raised(
        self, control_dir, monkeypatch
    ):
        def _boom(*args, **kwargs):
            raise FileNotFoundError("ssh")

        monkeypatch.setattr(ssh_mod.subprocess, "run", _boom)

        result = self._client().exec("10.0.0.5", "true")

        assert result.returncode == -1
        assert "ssh" in result.stderr


class TestCopy:
    def test_scp_targets_user_at_host(self, control_dir, monkeypatch):
        run = _FakeRun()
        monkeypatch.setattr(ssh_mod.subprocess, "run", run)

        OpenSSHClient(user="ubuntu").copy("/local/f", "10.0.0.5", "/remote/f")

        assert run.argv[0] == "scp"
        assert run.argv[-2:] == ["/local/f", "ubuntu@10.0.0.5:/remote/f"]

    def test_failure_raises_runtime_error(self, control_dir, monkeypatch):
        monkeypatch.setattr(
            ssh_mod.subprocess, "run", _FakeRun(returncode=1, stderr="No space left")
        )

        with pytest.raises(RuntimeError, match="No space left"):
            OpenSSHClient(user="ubuntu").copy("/local/f", "10.0.0.5", "/remote/f")

    def test_transport_failure_raises_ssh_error(self, control_dir, monkeypatch):
        monkeypatch.setattr(
            ssh_mod.subprocess,
            "run",
            _FakeRun(returncode=255, stderr="Connection refused"),
        )

        with pytest.raises(SSHError) as excinfo:
            OpenSSHClient(user="ubuntu").copy("/local/f", "10.0.0.5", "/remote/f")

        assert excinfo.value.error_type == SSHErrorType.NETWORK


class TestCopyDir:
    """rsync used to be handed scp as its remote shell, which cannot work."""

    def test_rsync_remote_shell_is_ssh_not_scp(self, control_dir, monkeypatch):
        run = _FakeRun()
        monkeypatch.setattr(ssh_mod.shutil, "which", lambda _: "/usr/bin/rsync")
        monkeypatch.setattr(ssh_mod.subprocess, "run", run)

        OpenSSHClient(user="ubuntu").copy_dir("/local/d", "10.0.0.5", "/remote/d")

        argv = run.argv
        assert argv[0] == "rsync"
        remote_shell = argv[argv.index("-e") + 1]
        assert remote_shell.split()[0] == "ssh"
        assert "scp" not in remote_shell
        assert "StrictHostKeyChecking=yes" in remote_shell
        assert "ControlMaster=auto" in remote_shell

    def test_rsync_uses_whole_file_and_partial(self, control_dir, monkeypatch):
        run = _FakeRun()
        monkeypatch.setattr(ssh_mod.shutil, "which", lambda _: "/usr/bin/rsync")
        monkeypatch.setattr(ssh_mod.subprocess, "run", run)

        OpenSSHClient(user="ubuntu").copy_dir("/local/d", "10.0.0.5", "/remote/d")

        assert "-W" in run.argv
        assert "--partial" in run.argv
        assert run.argv[-2:] == ["/local/d/", "ubuntu@10.0.0.5:/remote/d/"]

    def test_falls_back_to_scp_when_rsync_is_absent(self, control_dir, monkeypatch):
        run = _FakeRun()
        monkeypatch.setattr(ssh_mod.shutil, "which", lambda _: None)
        monkeypatch.setattr(ssh_mod.subprocess, "run", run)

        OpenSSHClient(user="ubuntu").copy_dir("/local/d", "10.0.0.5", "/remote/d")

        assert run.argv[0] == "scp"
        assert "-r" in run.argv
        assert run.argv[-2:] == ["/local/d/", "ubuntu@10.0.0.5:/remote/d/"]

    def test_scp_fallback_failure_raises(self, control_dir, monkeypatch):
        monkeypatch.setattr(ssh_mod.shutil, "which", lambda _: None)
        monkeypatch.setattr(
            ssh_mod.subprocess, "run", _FakeRun(returncode=1, stderr="scp exploded")
        )

        with pytest.raises(RuntimeError, match="scp exploded"):
            OpenSSHClient(user="ubuntu").copy_dir("/local/d", "10.0.0.5", "/remote/d")

    def test_rsync_failure_raises_runtime_error(self, control_dir, monkeypatch):
        monkeypatch.setattr(ssh_mod.shutil, "which", lambda _: "/usr/bin/rsync")
        monkeypatch.setattr(
            ssh_mod.subprocess, "run", _FakeRun(returncode=23, stderr="partial")
        )

        with pytest.raises(RuntimeError, match="partial"):
            OpenSSHClient(user="ubuntu").copy_dir("/local/d", "10.0.0.5", "/remote/d")

    def test_rsync_transport_failure_raises_ssh_error(self, control_dir, monkeypatch):
        monkeypatch.setattr(ssh_mod.shutil, "which", lambda _: "/usr/bin/rsync")
        monkeypatch.setattr(
            ssh_mod.subprocess,
            "run",
            _FakeRun(returncode=255, stderr="Host key verification failed."),
        )

        with pytest.raises(SSHError) as excinfo:
            OpenSSHClient(user="ubuntu").copy_dir("/local/d", "10.0.0.5", "/remote/d")

        assert excinfo.value.error_type == SSHErrorType.HOST_KEY

    def test_rsync_vanishing_after_the_lookup_falls_back(
        self, control_dir, monkeypatch
    ):
        calls: list[list[str]] = []

        def _run(args, **kwargs):
            calls.append(list(args))
            if args[0] == "rsync":
                raise FileNotFoundError("rsync")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(ssh_mod.shutil, "which", lambda _: "/usr/bin/rsync")
        monkeypatch.setattr(ssh_mod.subprocess, "run", _run)

        OpenSSHClient(user="ubuntu").copy_dir("/local/d", "10.0.0.5", "/remote/d")

        assert [c[0] for c in calls] == ["rsync", "scp"]


class TestModuleFunctions:
    """Tests for module-level convenience functions."""

    def test_ssh_exec_returns_result(self, control_dir, monkeypatch):
        monkeypatch.setattr(ssh_mod, "_default_client", None)
        monkeypatch.setattr(ssh_mod.subprocess, "run", _FakeRun(stdout="hello\n"))

        result = ssh_mod.ssh_exec("localhost", "echo hello", timeout=5)

        assert isinstance(result, SSHResult)
        assert result.stdout == "hello\n"

    def test_default_client_lazy_init(self, control_dir, monkeypatch):
        monkeypatch.setattr(ssh_mod, "_default_client", None)
        client = ssh_mod._get_default_client()
        assert isinstance(client, OpenSSHClient)
        assert client.host_key_policy == "strict"


class TestReverseTunnel:
    """A worker's docker daemon only trusts a plain-HTTP registry on loopback.

    Verified on a DGX Spark: the daemon's insecure defaults are exactly
    ``127.0.0.0/8`` and ``::1/128``. Reaching the control node's registry
    through a reverse tunnel therefore needs no change to any node's
    daemon.json, and no restart of a daemon that may be running work.
    """

    def test_the_forward_binds_loopback_on_the_remote(self):
        client = OpenSSHClient(user="alex")
        args = client._build_ssh_args(["-R", "127.0.0.1:5000:127.0.0.1:5000"])
        assert "-R" in args
        forward = args[args.index("-R") + 1]
        # Binding the wildcard would expose the control node's registry to the
        # whole network from every worker.
        assert forward.startswith("127.0.0.1:")

    def test_the_tunnel_does_not_multiplex(self):
        """Over a shared control master ssh hands the forward to the master and
        exits at once, so the tunnel would outlive its context manager."""
        client = OpenSSHClient(user="alex")
        with patch("subprocess.Popen") as popen:
            proc = MagicMock()
            proc.wait.side_effect = subprocess.TimeoutExpired("ssh", 2)
            popen.return_value = proc
            with client.reverse_tunnel("node", remote_port=5000, local_port=5000):
                pass
        args = popen.call_args[0][0]
        assert "ControlMaster=no" in args
        assert "ControlPath=none" in args

    def test_the_tunnel_is_closed_on_exit(self):
        client = OpenSSHClient(user="alex")
        with patch("subprocess.Popen") as popen:
            proc = MagicMock()
            proc.wait.side_effect = subprocess.TimeoutExpired("ssh", 2)
            popen.return_value = proc
            with client.reverse_tunnel("node", remote_port=5000, local_port=5000):
                pass
        proc.terminate.assert_called_once()

    def test_a_forward_that_cannot_bind_raises(self):
        """ssh exits at once when the remote port is taken; the caller must be
        told rather than left timing out against a port nothing serves."""
        client = OpenSSHClient(user="alex")
        with patch("subprocess.Popen") as popen:
            proc = MagicMock()
            proc.wait.return_value = 255
            proc.returncode = 255
            proc.stderr.read.return_value = b"remote port forwarding failed"
            popen.return_value = proc
            with pytest.raises(SSHError, match="reverse tunnel"):
                with client.reverse_tunnel("node", remote_port=5000, local_port=5000):
                    pass


# ── The control plane's own known_hosts ─────────────────────────────────────


KEY_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
KEY_B = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
KEY_RSA = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCCCCCCCCCCCCCCCCCCCCCC"


class TestKnownHosts:
    """The keys bootstrap confirmed, written where OpenSSH will look.

    Before this file existed, a node whose fingerprint an operator had just
    confirmed in the browser was a complete stranger to rsync — strict checking
    refused it with *No ED25519 host key is known* — and the only way through
    was ``ssh-keyscan``, which trusts whatever answers.
    """

    def test_the_file_lives_in_the_directory_we_own(self, control_dir):
        assert ssh_mod.known_hosts_path() == control_dir / "known_hosts"

    def test_a_confirmed_key_is_written_and_readable_back(self, control_dir):
        assert ssh_mod.trust_host_key("10.0.0.5", KEY_A) is True
        assert ssh_mod.trusted_entries("10.0.0.5") == [KEY_A]
        assert ssh_mod.known_hosts_path().read_text() == f"10.0.0.5 {KEY_A}\n"

    def test_the_file_is_private(self, control_dir):
        ssh_mod.trust_host_key("10.0.0.5", KEY_A)
        assert ssh_mod.known_hosts_path().stat().st_mode & 0o777 == 0o600
        assert control_dir.stat().st_mode & 0o777 == 0o700

    def test_writing_the_same_key_twice_changes_nothing(self, control_dir):
        assert ssh_mod.trust_host_key("10.0.0.5", KEY_A) is True
        assert ssh_mod.trust_host_key("10.0.0.5", KEY_A) is False
        assert ssh_mod.known_hosts_path().read_text().count(KEY_A) == 1

    def test_a_second_algorithm_is_kept_alongside_the_first(self, control_dir):
        ssh_mod.trust_host_key("10.0.0.5", KEY_A)
        ssh_mod.trust_host_key("10.0.0.5", KEY_RSA)
        assert sorted(ssh_mod.trusted_entries("10.0.0.5")) == sorted([KEY_A, KEY_RSA])

    def test_a_changed_key_replaces_rather_than_accumulates(self, control_dir, caplog):
        """Reaching this call means somebody confirmed the new fingerprint.

        Two ed25519 lines for one host is a file ssh reads as a mismatch, so
        the old one goes — loudly, because a node's identity changing is worth
        a line in the log even when it was expected.
        """
        ssh_mod.trust_host_key("10.0.0.5", KEY_A)
        with caplog.at_level(logging.WARNING):
            assert ssh_mod.trust_host_key("10.0.0.5", KEY_B) is True
        assert ssh_mod.trusted_entries("10.0.0.5") == [KEY_B]
        assert "different" in caplog.text

    def test_one_host_key_never_leaks_onto_another_host(self, control_dir):
        ssh_mod.trust_host_key("10.0.0.5", KEY_A)
        assert ssh_mod.trusted_entries("10.0.0.6") == []

    def test_a_non_default_port_is_bracketed_as_ssh_writes_it(self, control_dir):
        ssh_mod.trust_host_key("10.0.0.5", KEY_A, 2222)
        assert ssh_mod.known_hosts_path().read_text() == f"[10.0.0.5]:2222 {KEY_A}\n"
        assert ssh_mod.trusted_entries("10.0.0.5", 2222) == [KEY_A]
        assert ssh_mod.trusted_entries("10.0.0.5") == []

    @pytest.mark.parametrize("entry", ["", "   ", "ssh-ed25519", "nokeyhere"])
    def test_something_that_is_not_a_host_key_is_refused(self, control_dir, entry):
        with pytest.raises(ValueError, match="not a host key entry"):
            ssh_mod.trust_host_key("10.0.0.5", entry)
        assert not ssh_mod.known_hosts_path().exists()

    def test_an_unknown_host_reads_back_as_nothing(self, control_dir):
        assert ssh_mod.trusted_entries("10.0.0.5") == []


class TestTrustingAnAlias:
    """The same machine, under the second address the fabric gave it."""

    def test_the_recorded_key_is_trusted_under_the_fabric_address(self, control_dir):
        ssh_mod.trust_host_key("192.168.29.152", KEY_A)
        assert ssh_mod.trust_alias("192.168.29.152", "192.168.177.12") == 1
        assert ssh_mod.trusted_entries("192.168.177.12") == [KEY_A]

    def test_every_algorithm_travels_with_it(self, control_dir):
        ssh_mod.trust_host_key("192.168.29.152", KEY_A)
        ssh_mod.trust_host_key("192.168.29.152", KEY_RSA)
        assert ssh_mod.trust_alias("192.168.29.152", "192.168.177.12") == 2

    def test_nothing_is_invented_for_a_host_we_have_no_key_for(self, control_dir):
        assert ssh_mod.trust_alias("192.168.29.152", "192.168.177.12") == 0
        assert ssh_mod.trusted_entries("192.168.177.12") == []

    def test_aliasing_an_address_to_itself_is_a_no_op(self, control_dir):
        ssh_mod.trust_host_key("192.168.29.152", KEY_A)
        assert ssh_mod.trust_alias("192.168.29.152", "192.168.29.152") == 0


class TestTheClientVerifiesAgainstIt:
    def test_the_known_hosts_file_is_passed_to_ssh(self, control_dir):
        client = OpenSSHClient(known_hosts_file="/c/known_hosts")
        args = client._build_ssh_args()
        assert "UserKnownHostsFile=/c/known_hosts" in args
        assert "StrictHostKeyChecking=yes" in args

    def test_scp_and_rsync_verify_against_the_same_file(self, control_dir):
        client = OpenSSHClient(known_hosts_file="/c/known_hosts")
        assert "UserKnownHostsFile=/c/known_hosts" in client._build_scp_args()
        # rsync splits ``-e`` on whitespace, so the path must survive as one
        # word — which is why this is one file and not ours plus the user's.
        assert (
            "UserKnownHostsFile=/c/known_hosts" in client._rsync_remote_shell().split()
        )

    def test_no_file_leaves_ssh_its_own_default(self, control_dir):
        args = OpenSSHClient()._build_ssh_args()
        assert not any(a.startswith("UserKnownHostsFile") for a in args)
        assert OpenSSHClient().known_hosts_file is None
