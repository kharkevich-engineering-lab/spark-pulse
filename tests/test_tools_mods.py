"""Tests for mod validation, cluster deployment, and rollback."""

from __future__ import annotations


from spark_pulse.tools.mods import (
    validate_mod_content,
)


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
