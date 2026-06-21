"""Tests for mod validation, cluster deployment, and rollback."""

from __future__ import annotations

from pathlib import Path

import pytest

from spark_pulse.tools.mods import (
    ModDeployment,
    ModOrchestrator,
    validate_mod_content,
)


class TestModDeployment:
    """Tests for ModDeployment dataclass."""

    def test_default_values(self, tmp_path):
        deployment = ModDeployment(
            mod_name="test-mod",
            mod_path=tmp_path,
            target="all",
        )
        assert deployment.mod_name == "test-mod"
        assert deployment.target == "all"
        assert deployment.completed_nodes == []
        assert deployment.failed_nodes == []

    def test_with_completed_nodes(self, tmp_path):
        deployment = ModDeployment(
            mod_name="test-mod",
            mod_path=tmp_path,
            target="head",
            completed_nodes=["10.0.0.1"],
        )
        assert deployment.completed_nodes == ["10.0.0.1"]

    def test_with_failed_nodes(self, tmp_path):
        deployment = ModDeployment(
            mod_name="test-mod",
            mod_path=tmp_path,
            target="workers",
            failed_nodes=["10.0.0.2", "10.0.0.3"],
        )
        assert deployment.failed_nodes == ["10.0.0.2", "10.0.0.3"]


class TestValidateModContent:
    """Tests for validate_mod_content function."""

    def test_valid_mod_directory(self, tmp_path):
        mod_dir = tmp_path / "valid-mod"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\necho 'installing'")
        result = validate_mod_content(mod_dir)
        assert result.healthy is True

    def test_dangerous_rm_rf_root(self, tmp_path):
        mod_dir = tmp_path / "dangerous-rm"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\nrm -rf /")
        result = validate_mod_content(mod_dir)
        assert result.healthy is False
        assert any("rm" in e for e in result.errors)

    def test_dangerous_mkfs(self, tmp_path):
        mod_dir = tmp_path / "dangerous-mkfs"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\nmkfs.ext4 /dev/sda")
        result = validate_mod_content(mod_dir)
        assert result.healthy is False

    def test_dangerous_reboot(self, tmp_path):
        mod_dir = tmp_path / "dangerous-reboot"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\nreboot")
        result = validate_mod_content(mod_dir)
        assert result.healthy is False

    def test_dangerous_shutdown(self, tmp_path):
        mod_dir = tmp_path / "dangerous-shutdown"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\nshutdown -h now")
        result = validate_mod_content(mod_dir)
        assert result.healthy is False

    def test_sudo_warning(self, tmp_path):
        mod_dir = tmp_path / "sudo-mod"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\nsudo apt-get update")
        result = validate_mod_content(mod_dir)
        assert result.healthy is True
        assert any("sudo" in w for w in result.warnings)

    def test_network_warning(self, tmp_path):
        mod_dir = tmp_path / "network-mod"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\ncurl http://example.com/file")
        result = validate_mod_content(mod_dir)
        assert result.healthy is True
        assert any("network" in w or "curl" in w for w in result.warnings)

    def test_nonexistent_mod(self, tmp_path):
        result = validate_mod_content(tmp_path / "nonexistent")
        assert result.healthy is False


class MockNode:
    """Mock node for testing."""

    def __init__(self, ip: str, role: str, container_name: str):
        self.ip = ip
        self.role = role
        self.container_name = container_name


class MockClusterState:
    """Mock cluster state for testing."""

    def __init__(self):
        self.head = MockNode("10.0.0.1", "head", "head-container")
        self.workers = [
            MockNode("10.0.0.2", "worker", "worker0-container"),
            MockNode("10.0.0.3", "worker", "worker1-container"),
        ]


class MockRemoteDocker:
    """Mock RemoteDockerService for testing."""

    def __init__(self):
        self.calls: list[dict] = []

    def exec_container(self, host: str, container: str, command: list[str], timeout: int = 30) -> None:
        self.calls.append({"type": "exec", "host": host, "container": container, "command": command})

    def copy_to_container(self, host: str, container: str, local_path: str, remote_path: str) -> None:
        self.calls.append({"type": "copy", "host": host, "container": container, "local": local_path, "remote": remote_path})


class TestModOrchestrator:
    """Tests for ModOrchestrator class."""

    def test_validate_mod(self, tmp_path):
        mod_dir = tmp_path / "valid-mod"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\necho 'installing'")
        orchestrator = ModOrchestrator()
        result = orchestrator.validate_mod(mod_dir)
        assert result.healthy is True

    def test_validate_dangerous_mod(self, tmp_path):
        mod_dir = tmp_path / "dangerous-mod"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\nrm -rf /")
        orchestrator = ModOrchestrator()
        result = orchestrator.validate_mod(mod_dir)
        assert result.healthy is False

    def test_apply_mod_head_only(self, tmp_path):
        mod_dir = tmp_path / "test-mod"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\necho 'installing'")

        cluster = MockClusterState()
        deployment = ModDeployment(
            mod_name="test-mod",
            mod_path=mod_dir,
            target="head",
        )
        mock_docker = MockRemoteDocker()
        orchestrator = ModOrchestrator(remote_docker=mock_docker)
        result = orchestrator.apply_mod_cluster(deployment, cluster)

        assert result.mod_name == "test-mod"
        assert result.target == "head"
        assert "10.0.0.1" in result.completed_nodes
        assert len(result.failed_nodes) == 0

    def test_apply_mod_workers_only(self, tmp_path):
        mod_dir = tmp_path / "test-mod"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\necho 'installing'")

        cluster = MockClusterState()
        deployment = ModDeployment(
            mod_name="test-mod",
            mod_path=mod_dir,
            target="workers",
        )
        mock_docker = MockRemoteDocker()
        orchestrator = ModOrchestrator(remote_docker=mock_docker)
        result = orchestrator.apply_mod_cluster(deployment, cluster)

        assert result.target == "workers"
        assert "10.0.0.2" in result.completed_nodes
        assert "10.0.0.3" in result.completed_nodes
        assert "10.0.0.1" not in result.completed_nodes

    def test_apply_mod_all(self, tmp_path):
        mod_dir = tmp_path / "test-mod"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\necho 'installing'")

        cluster = MockClusterState()
        deployment = ModDeployment(
            mod_name="test-mod",
            mod_path=mod_dir,
            target="all",
        )
        mock_docker = MockRemoteDocker()
        orchestrator = ModOrchestrator(remote_docker=mock_docker)
        result = orchestrator.apply_mod_cluster(deployment, cluster)

        assert result.target == "all"
        assert "10.0.0.1" in result.completed_nodes
        assert "10.0.0.2" in result.completed_nodes
        assert "10.0.0.3" in result.completed_nodes

    def test_rollback_mod(self, tmp_path):
        mod_dir = tmp_path / "test-mod"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\necho 'installing'")

        cluster = MockClusterState()
        deployment = ModDeployment(
            mod_name="test-mod",
            mod_path=mod_dir,
            target="all",
            completed_nodes=["10.0.0.1", "10.0.0.2"],
        )
        mock_docker = MockRemoteDocker()
        orchestrator = ModOrchestrator(remote_docker=mock_docker)
        rolled_back = orchestrator.rollback_mod(deployment, cluster)

        assert "10.0.0.1" in rolled_back
        assert "10.0.0.2" in rolled_back

    def test_rollback_no_completed_nodes(self, tmp_path):
        mod_dir = tmp_path / "test-mod"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\necho 'installing'")

        cluster = MockClusterState()
        deployment = ModDeployment(
            mod_name="test-mod",
            mod_path=mod_dir,
            target="all",
            completed_nodes=[],
        )
        mock_docker = MockRemoteDocker()
        orchestrator = ModOrchestrator(remote_docker=mock_docker)
        rolled_back = orchestrator.rollback_mod(deployment, cluster)
        assert rolled_back == []
