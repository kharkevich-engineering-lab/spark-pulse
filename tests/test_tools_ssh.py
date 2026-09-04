"""Tests for SSH transport abstraction."""

from __future__ import annotations

import getpass
import importlib
import subprocess

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
