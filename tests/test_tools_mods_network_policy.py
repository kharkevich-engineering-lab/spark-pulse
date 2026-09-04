"""Tests for mod network access policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from spark_pulse.tools.launch_script import validate_mod_content


class TestModNetworkPolicy:
    @pytest.fixture
    def mod_dir(self, tmp_path):
        """Create a test mod directory with run.sh."""
        mod_path = tmp_path / "test-mod"
        mod_path.mkdir()
        return mod_path

    def _create_run_sh(self, mod_dir: Path, content: str) -> Path:
        run_sh = mod_dir / "run.sh"
        run_sh.write_text(content)
        return run_sh

    def test_allow_policy_no_network(self, mod_dir):
        self._create_run_sh(mod_dir, "#!/bin/bash\necho hello")
        result = validate_mod_content(mod_dir, network_policy="allow")
        assert result.healthy is True
        assert len(result.warnings) == 0

    def test_allow_policy_with_network(self, mod_dir):
        self._create_run_sh(mod_dir, "#!/bin/bash\ncurl http://example.com")
        result = validate_mod_content(mod_dir, network_policy="allow")
        assert result.healthy is True
        # No warnings with allow policy

    def test_warn_policy_with_network(self, mod_dir):
        self._create_run_sh(mod_dir, "#!/bin/bash\nwget http://example.com")
        result = validate_mod_content(mod_dir, network_policy="warn")
        assert result.healthy is True
        assert any("network" in w.lower() for w in result.warnings)

    def test_deny_policy_with_network(self, mod_dir):
        self._create_run_sh(mod_dir, "#!/bin/bash\npip install requests")
        result = validate_mod_content(mod_dir, network_policy="deny")
        assert result.healthy is False
        assert any("denied" in e.lower() for e in result.errors)

    def test_deny_policy_no_network(self, mod_dir):
        self._create_run_sh(mod_dir, "#!/bin/bash\necho hello")
        result = validate_mod_content(mod_dir, network_policy="deny")
        assert result.healthy is True

    def test_all_network_patterns(self, tmp_path):
        """Test all network patterns: curl, wget, pip, apt, yum, dnf."""
        patterns = ["curl", "wget", "pip install", "apt", "yum", "dnf"]
        for pattern in patterns:
            mod = tmp_path / f"mod-{pattern}"
            mod.mkdir()
            self._create_run_sh(mod, f"#!/bin/bash\n{pattern} something")

            result_deny = validate_mod_content(mod, network_policy="deny")
            assert result_deny.healthy is False, f"Failed for pattern: {pattern}"

            result_warn = validate_mod_content(mod, network_policy="warn")
            assert result_warn.healthy is True, f"Failed for warn pattern: {pattern}"
            assert len(result_warn.warnings) > 0

    def test_dangerous_patterns_still_rejected(self, mod_dir):
        self._create_run_sh(mod_dir, "#!/bin/bash\nrm -rf /")
        result = validate_mod_content(mod_dir, network_policy="allow")
        assert result.healthy is False
        assert any("dangerous" in e.lower() for e in result.errors)
