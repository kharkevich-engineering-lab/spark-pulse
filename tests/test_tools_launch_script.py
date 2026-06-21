"""Tests for launch script analysis, patching, and validation."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from spark_pulse.tools.launch_script import (
    LaunchScriptManager,
    LaunchScriptDistributor,
    PatchedScriptBundle,
    ValidationResult,
    analyze_launch_script,
    validate_launch_script,
    validate_mod_content,
)


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_ok_result(self):
        result = ValidationResult.ok()
        assert result.healthy is True
        assert result.warnings == []
        assert result.errors == []

    def test_ok_result_with_warnings(self):
        result = ValidationResult.ok(warnings=["warning1"])
        assert result.healthy is True
        assert result.warnings == ["warning1"]

    def test_fail_result(self):
        result = ValidationResult.fail(errors=["error1"])
        assert result.healthy is False
        assert result.errors == ["error1"]

    def test_fail_result_with_warnings(self):
        result = ValidationResult.fail(
            errors=["error1"],
            warnings=["warning1"],
        )
        assert result.healthy is False
        assert result.errors == ["error1"]
        assert result.warnings == ["warning1"]


class TestValidateLaunchScript:
    """Tests for validate_launch_script function."""

    def test_missing_script(self, tmp_path):
        result = validate_launch_script(tmp_path / "nonexistent.sh")
        assert result.healthy is False
        assert len(result.errors) > 0

    def test_directory_not_file(self, tmp_path):
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        result = validate_launch_script(subdir)
        assert result.healthy is False

    def test_valid_script(self, tmp_path):
        script = tmp_path / "valid.sh"
        script.write_text("#!/bin/bash\npython model.py --model test")
        result = validate_launch_script(script)
        assert result.healthy is True

    def test_missing_model_flag(self, tmp_path):
        script = tmp_path / "no_model.sh"
        script.write_text("#!/bin/bash\npython model.py")
        result = validate_launch_script(script)
        assert result.healthy is True
        assert any("--model" in w for w in result.warnings)

    def test_no_python_command(self, tmp_path):
        script = tmp_path / "no_python.sh"
        script.write_text("#!/bin/bash\necho 'hello'")
        result = validate_launch_script(script)
        assert result.healthy is False


class TestAnalyzeLaunchScript:
    """Tests for analyze_launch_script function."""

    def test_qwen_script(self, tmp_path):
        script = tmp_path / "qwen.sh"
        script.write_text(
            "#!/bin/bash\n"
            "python -m vllm.entrypoints.openai.api_server "
            "--model Qwen/Qwen2.5-7B "
            "--tensor-parallel-size 4 "
            "--distributed-executor-backend ray\n"
        )
        info = analyze_launch_script(script)
        assert info.is_valid is True
        assert info.parallelism["tp"] == 4
        assert info.backend == "ray"
        assert info.has_model_flag is True
        assert info.command_line is not None

    def test_llama_script(self, tmp_path):
        script = tmp_path / "llama.sh"
        script.write_text(
            "#!/bin/bash\n"
            "python -m vllm.entrypoints.openai.api_server "
            "--model meta-llama/Llama-3.1-8B "
            "--pipeline-parallel-size 2\n"
        )
        info = analyze_launch_script(script)
        assert info.is_valid is True
        assert info.parallelism["pp"] == 2
        assert info.backend is None

    def test_mp_backend(self, tmp_path):
        script = tmp_path / "mp.sh"
        script.write_text(
            "#!/bin/bash\n"
            "python model.py --model test "
            "--distributed-executor-backend mp\n"
        )
        info = analyze_launch_script(script)
        assert info.backend == "mp"

    def test_invalid_script(self, tmp_path):
        script = tmp_path / "invalid.sh"
        script.write_text("#!/bin/bash\necho 'no interpreter command'")
        info = analyze_launch_script(script)
        assert info.is_valid is False


class TestLaunchScriptManager:
    """Tests for LaunchScriptManager class."""

    def test_resolve_absolute_path(self, tmp_path):
        script = tmp_path / "test.sh"
        script.write_text("#!/bin/bash\npython model.py")
        manager = LaunchScriptManager()
        resolved = manager.resolve(str(script))
        assert resolved == script

    def test_resolve_nonexistent(self):
        with pytest.raises(FileNotFoundError):
            manager = LaunchScriptManager()
            manager.resolve("/nonexistent/path/script.sh")

    def test_analyze(self, tmp_path):
        script = tmp_path / "test.sh"
        script.write_text(
            "#!/bin/bash\n" "python model.py --model test --tensor-parallel-size 2\n"
        )
        manager = LaunchScriptManager()
        info = manager.analyze(script)
        assert info.parallelism["tp"] == 2

    def test_create_patched_bundle(self, tmp_path):
        script = tmp_path / "test.sh"
        script.write_text(
            "#!/bin/bash\n"
            "python model.py --model test "
            "--distributed-executor-backend ray\n"
        )
        manager = LaunchScriptManager()
        bundle = manager.create_patched_bundle(
            script_path=script,
            total_nodes=3,
            master_addr="10.0.0.1",
            master_port=29500,
        )
        try:
            assert bundle.total_nodes == 3
            assert 0 in bundle.scripts
            assert 1 in bundle.scripts
            assert 2 in bundle.scripts
            assert bundle.master_addr == "10.0.0.1"
            assert bundle.master_port == 29500

            # Check that each script has distributed args
            for rank, script_path in bundle.scripts.items():
                content = script_path.read_text()
                assert "--nnodes 3" in content
                assert f"--node-rank {rank}" in content
                assert "--master-addr 10.0.0.1" in content
                assert "--master-port 29500" in content
                assert "--headless" in content
                assert "--distributed-executor-backend" not in content
        finally:
            manager.cleanup(bundle)

    def test_patched_bundle_cleanup(self, tmp_path):
        script = tmp_path / "test.sh"
        script.write_text("#!/bin/bash\npython model.py")
        manager = LaunchScriptManager()
        bundle = manager.create_patched_bundle(
            script_path=script,
            total_nodes=2,
        )
        manager.cleanup(bundle)
        # Should not raise after cleanup

    def test_validation_failure_raises(self, tmp_path):
        script = tmp_path / "dangerous.sh"
        script.write_text("#!/bin/bash\necho 'no interpreter'")
        manager = LaunchScriptManager()
        with pytest.raises(ValueError):
            manager.create_patched_bundle(
                script_path=script,
                total_nodes=2,
            )


class TestPatchedScriptBundle:
    """Tests for PatchedScriptBundle dataclass."""

    def test_script_path(self, tmp_path):
        with tempfile.TemporaryDirectory() as temp_dir:
            scripts = {
                0: Path(temp_dir) / "node0.sh",
                1: Path(temp_dir) / "node1.sh",
            }
            for s in scripts.values():
                s.write_text("# test")
            bundle = PatchedScriptBundle(
                temp_dir=tempfile.TemporaryDirectory(),
                scripts=scripts,
                total_nodes=2,
            )
            assert bundle.script_path(0) == scripts[0]
            assert bundle.script_path(1) == scripts[1]
            assert bundle.script_path(99) is None
            bundle.cleanup()

    def test_context_manager(self, tmp_path):
        with tempfile.TemporaryDirectory() as temp_dir:
            scripts = {
                0: Path(temp_dir) / "node0.sh",
            }
            scripts[0].write_text("# test")
            with PatchedScriptBundle(
                temp_dir=tempfile.TemporaryDirectory(),
                scripts=scripts,
            ):
                pass
            # Should not raise on exit


class TestLaunchScriptDistributor:
    """Tests for LaunchScriptDistributor class."""

    def test_requires_remote_docker(self):
        distributor = LaunchScriptDistributor()
        with pytest.raises(RuntimeError, match="RemoteDockerService"):
            distributor.deploy_to_node(
                node=None,  # type: ignore
                script=Path("/tmp/test.sh"),
                container_name="test",
            )


class TestValidateModContent:
    """Tests for validate_mod_content function."""

    def test_valid_mod(self, tmp_path):
        mod_dir = tmp_path / "valid-mod"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\necho 'installing mod'")
        result = validate_mod_content(mod_dir)
        assert result.healthy is True

    def test_dangerous_rm_rf(self, tmp_path):
        mod_dir = tmp_path / "dangerous-mod"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\nrm -rf /")
        result = validate_mod_content(mod_dir)
        assert result.healthy is False
        assert any("rm" in e for e in result.errors)

    def test_dangerous_mkfs(self, tmp_path):
        mod_dir = tmp_path / "dangerous-mod"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\nmkfs.ext4 /dev/sda")
        result = validate_mod_content(mod_dir)
        assert result.healthy is False

    def test_dangerous_reboot(self, tmp_path):
        mod_dir = tmp_path / "dangerous-mod"
        mod_dir.mkdir()
        run_sh = mod_dir / "run.sh"
        run_sh.write_text("#!/bin/bash\nreboot")
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
        run_sh.write_text("#!/bin/bash\ncurl http://example.com/script.sh")
        result = validate_mod_content(mod_dir)
        assert result.healthy is True
        assert any("network" in w or "curl" in w for w in result.warnings)

    def test_nonexistent_mod(self, tmp_path):
        result = validate_mod_content(tmp_path / "nonexistent")
        assert result.healthy is False

    def test_zip_bomb_detection(self, tmp_path):
        import zipfile

        mod_dir = tmp_path / "zipbomb.zip"
        with zipfile.ZipFile(mod_dir, "w") as zf:
            zf.writestr("small.txt", "x" * 100)
        result = validate_mod_content(mod_dir)
        # The compression ratio won't be > 10x for this small file
        assert result.healthy is True
        mod_dir.unlink()
