"""The simulated transports: SSH, locks and the event broadcaster.

These three mocks were the least-exercised code in the package — every real
consumer imports `tools.ssh` / `tools.events` / `tools.locking` directly rather
than through the simulation switch, so nothing had ever called them. That is
exactly how ``SSHClient.__init__`` came to assign ``dataclasses.field(...)`` to
an instance attribute of a plain class, which made the first ``exec()`` die on
``Field.append``. They are reachable by name now (see
``tests/test_mock_contract.py``), so they are worth holding to their contract.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from spark_pulse.mock import events as mock_events
from spark_pulse.mock import locking as mock_locking
from spark_pulse.mock import ssh as mock_ssh


# ── SSH ──────────────────────────────────────────────────────────────────────


class TestSimulatedSSH:
    def test_a_command_is_recorded_rather_than_run(self):
        client = mock_ssh.SSHClient(default_stdout="hello")

        result = client.exec("10.0.0.2", "uname -a", timeout=5)

        assert (result.returncode, result.stdout, result.ok) == (0, "hello", True)
        assert client.executed_commands == [
            {"host": "10.0.0.2", "command": "uname -a", "timeout": 5}
        ]

    def test_a_host_declared_unreachable_refuses_the_connection(self):
        client = mock_ssh.SSHClient(fail_hosts=["10.0.0.9"])

        result = client.exec("10.0.0.9", "true")

        assert result.ok is False
        assert result.returncode == 1
        assert "10.0.0.9" in result.stderr

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("ray status", "Cluster is ready"),
            ("nvidia-smi -L | wc -l", "1"),
            ("env", "NCCL_SOCKET_IFNAME=eth0\nPATH=/usr/local/bin"),
        ],
    )
    def test_the_commands_the_cluster_asks_get_plausible_answers(
        self, command, expected
    ):
        assert mock_ssh.SSHClient().exec("10.0.0.2", command).stdout == expected

    def test_a_failing_default_is_reported_as_a_failure(self):
        client = mock_ssh.SSHClient(default_returncode=1, default_stderr="boom")

        result = client.exec("10.0.0.2", "false")

        assert result.ok is False
        assert result.stderr == "boom"

    def test_the_remote_shell_command_is_the_argv_ssh_would_use(self):
        client = mock_ssh.SSHClient()

        assert client.remote_shell_command("10.0.0.2") == [
            "ssh",
            "-o",
            "BatchMode=yes",
            "10.0.0.2",
        ]
        assert client.remote_shell_command("10.0.0.2", "ls")[-1] == "ls"

    def test_copies_are_recorded_with_both_ends(self):
        client = mock_ssh.SSHClient()

        client.copy("/local/f", "10.0.0.2", "/remote/f")
        client.copy_dir("/local/d", "10.0.0.2", "/remote/d")

        assert [c["action"] for c in client.executed_commands] == ["copy", "copy_dir"]
        assert client.executed_commands[0]["remote"] == "/remote/f"
        assert client.executed_commands[1]["local"] == "/local/d"

    def test_the_history_is_a_copy_a_caller_cannot_corrupt(self):
        client = mock_ssh.SSHClient()
        client.exec("10.0.0.2", "true")

        client.executed_commands.clear()

        assert len(client.executed_commands) == 1

    def test_resetting_forgets_what_was_run(self):
        client = mock_ssh.SSHClient()
        client.exec("10.0.0.2", "true")

        client.reset()

        assert client.executed_commands == []

    def test_the_concrete_client_is_the_simulated_one(self):
        assert mock_ssh.OpenSSHClient is mock_ssh.SSHClient

    def test_the_module_level_helpers_share_one_client(self):
        default = mock_ssh._get_default_client()
        default.reset()

        mock_ssh.ssh_exec("10.0.0.2", "true")
        mock_ssh.ssh_copy("/l", "10.0.0.2", "/r")
        mock_ssh.ssh_copy_dir("/l", "10.0.0.2", "/r")

        assert mock_ssh._get_default_client() is default
        assert len(default.executed_commands) == 3
        default.reset()


# ── Locks ────────────────────────────────────────────────────────────────────


class TestSimulatedLocks:
    def test_a_lock_is_granted_and_reported_as_held(self):
        manager = mock_locking.LockManager()

        result = manager.acquire(
            mock_locking.LockType.DEPLOYMENT_START, "dep-1", owner="alice", timeout=60
        )

        assert result.success is True
        assert result.lock.resource == "dep-1"
        assert result.lock.owner == "alice"
        assert result.lock.expires_at > result.lock.acquired_at
        assert (
            manager.is_locked(mock_locking.LockType.DEPLOYMENT_START, "dep-1") is True
        )

    def test_releasing_it_gives_it_back(self):
        manager = mock_locking.LockManager()
        manager.acquire(mock_locking.LockType.DEPLOYMENT_START, "dep-1")

        assert manager.release(mock_locking.LockType.DEPLOYMENT_START, "dep-1") is True
        assert (
            manager.is_locked(mock_locking.LockType.DEPLOYMENT_START, "dep-1") is False
        )

    def test_nothing_is_held_before_anything_is_acquired(self):
        manager = mock_locking.LockManager()

        assert (
            manager.is_locked(mock_locking.LockType.DEPLOYMENT_START, "dep-1") is False
        )
        assert manager.get_active_locks() == []

    def test_the_contention_scenario_refuses_and_says_which_resource(self):
        manager = mock_locking.LockManager(scenario="contention")

        result = manager.acquire(mock_locking.LockType.DEPLOYMENT_START, "dep-1")

        assert result.success is False
        assert "dep-1" in result.error
        assert result.lock is None

    def test_the_expired_scenario_hands_back_a_lock_that_is_already_stale(self):
        manager = mock_locking.LockManager(scenario="expired")

        result = manager.acquire(mock_locking.LockType.DEPLOYMENT_START, "dep-1")

        assert result.success is True
        assert result.lock.expires_at < datetime.now(timezone.utc)
        assert result.lock.acquired_at < result.lock.expires_at
        assert manager.cleanup_expired() == 1

    def test_a_healthy_manager_has_nothing_to_clean_up(self):
        manager = mock_locking.LockManager()
        manager.acquire(mock_locking.LockType.DEPLOYMENT_START, "dep-1")

        assert manager.cleanup_expired() == 0


# ── Events ───────────────────────────────────────────────────────────────────


class TestSimulatedBroadcaster:
    def test_an_emitted_event_is_kept_for_inspection(self):
        broadcaster = mock_events.EventBroadcaster()

        broadcaster.emit({"type": "custom", "payload": 1})

        assert broadcaster.emitted_count == 1
        assert broadcaster.emitted_events == [{"type": "custom", "payload": 1}]

    def test_a_deployment_event_carries_the_deployment_it_is_about(self):
        broadcaster = mock_events.EventBroadcaster()

        broadcaster.emit_deployment_event(
            "started", "dep-1", message="up", metadata={"rank": 0}
        )

        assert broadcaster.emitted_events == [
            {
                "type": "started",
                "deployment": "dep-1",
                "message": "up",
                "metadata": {"rank": 0},
            }
        ]

    def test_a_cluster_event_carries_the_cluster_it_is_about(self):
        broadcaster = mock_events.EventBroadcaster()

        broadcaster.emit_cluster_event("ready", "prod")

        assert broadcaster.emitted_events == [
            {"type": "ready", "cluster": "prod", "message": "", "metadata": {}}
        ]

    def test_subscribing_and_unsubscribing_are_harmless(self):
        broadcaster = mock_events.EventBroadcaster()

        queue = broadcaster.subscribe()
        broadcaster.emit({"type": "custom"})
        broadcaster.unsubscribe(queue)

        assert broadcaster.emitted_count == 1

    def test_the_record_of_events_is_a_copy(self):
        broadcaster = mock_events.EventBroadcaster()
        broadcaster.emit({"type": "custom"})

        broadcaster.emitted_events.clear()

        assert broadcaster.emitted_count == 1

    def test_the_event_vocabulary_is_the_real_one(self):
        """A simulated event must be the same kind of thing a real one is."""
        import sys

        import spark_pulse.tools.events  # noqa: F401

        real = sys.modules["spark_pulse.tools.events"]

        assert mock_events.EventType is real.EventType
        assert mock_events.DeploymentEvent is real.DeploymentEvent
