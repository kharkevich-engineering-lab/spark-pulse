"""Tests for the mod *reader* in ``tools/mods.py``.

``tests/test_tools_mods.py`` covers the orchestrator and the security
validation; the half that actually reads a checkout — the description
extractor, the asset-kind table, and the two-directory listing that merges the
checkout's ``mods/`` with the operator's own ``custom-mods/`` — had no tests at
all. That half is what the Mods page renders, so a regression there is silent
until someone opens the page.
"""

from __future__ import annotations

import pytest

from spark_pulse.config import config
from spark_pulse.tools import custom_files
from spark_pulse.tools.mods import (
    _asset_kind,
    _extract_description,
    _mod_info,
    _mod_dirs,
    get_mod,
    list_mods,
)


def _write_mod(directory, name: str, script: str = "#!/bin/bash\ntrue\n", **files):
    """A mod directory holding ``run.sh`` plus whatever else is asked for."""
    mod = directory / name
    mod.mkdir(parents=True)
    (mod / "run.sh").write_text(script)
    for filename, body in files.items():
        (mod / filename.replace("__", ".")).write_text(body)
    return mod


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A spark-vllm-docker checkout with a ``mods/`` directory."""
    mods_dir = tmp_path / "checkout" / "mods"
    mods_dir.mkdir(parents=True)
    monkeypatch.setitem(config._data, "spark_vllm_path", str(tmp_path / "checkout"))
    return mods_dir


@pytest.fixture
def custom(tmp_path, monkeypatch):
    """The operator's own ``custom-mods`` directory."""
    directory = tmp_path / "custom-mods"
    directory.mkdir()
    monkeypatch.setattr(custom_files, "_CUSTOM_MODS_DIR", directory)
    return directory


# ── Description extraction ───────────────────────────────────────────────────


class TestExtractDescription:
    """The one-line summary shown next to each mod in the list."""

    def test_leading_comment_block_becomes_the_description(self, tmp_path):
        run_sh = tmp_path / "run.sh"
        run_sh.write_text("#!/bin/bash\n# Fixes quantization\n# for Qwen3\ncd /x\n")

        assert _extract_description(run_sh) == "Fixes quantization for Qwen3"

    def test_a_blank_line_ends_the_comment_block(self, tmp_path):
        run_sh = tmp_path / "run.sh"
        run_sh.write_text("# First paragraph\n\n# Second paragraph\n")

        assert _extract_description(run_sh) == "First paragraph"

    def test_leading_blank_lines_before_any_comment_are_skipped(self, tmp_path):
        run_sh = tmp_path / "run.sh"
        run_sh.write_text("\n\n# Real description\n")

        assert _extract_description(run_sh) == "Real description"

    def test_the_first_code_line_ends_the_block(self, tmp_path):
        run_sh = tmp_path / "run.sh"
        run_sh.write_text("# Only this\nset -e\n# not this\n")

        assert _extract_description(run_sh) == "Only this"

    def test_bare_comment_markers_contribute_nothing(self, tmp_path):
        run_sh = tmp_path / "run.sh"
        run_sh.write_text("#\n# Real text\n#\n")

        assert _extract_description(run_sh) == "Real text"

    def test_falls_back_to_the_first_echo_when_there_are_no_comments(self, tmp_path):
        run_sh = tmp_path / "run.sh"
        run_sh.write_text('#!/bin/bash\nset -e\necho "== Installing hooks ==="\n')

        # The decoration around an echoed banner is stripped.
        assert _extract_description(run_sh) == "Installing hooks"

    def test_echo_fallback_accepts_single_quotes_and_drops_a_full_stop(self, tmp_path):
        run_sh = tmp_path / "run.sh"
        run_sh.write_text("#!/bin/bash\n  echo 'Applying the patch.'\n")

        assert _extract_description(run_sh) == "Applying the patch"

    def test_a_script_with_neither_comments_nor_echo_has_no_description(self, tmp_path):
        run_sh = tmp_path / "run.sh"
        run_sh.write_text("#!/bin/bash\nmake install\n")

        assert _extract_description(run_sh) == ""

    def test_an_unreadable_run_sh_degrades_to_no_description(self, tmp_path):
        # A directory where run.sh is expected: reading it raises OSError.
        unreadable = tmp_path / "run.sh"
        unreadable.mkdir()

        assert _extract_description(unreadable) == ""


# ── Asset kinds ──────────────────────────────────────────────────────────────


class TestAssetKind:
    """The badge the UI puts on each file shipped with a mod."""

    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("fix.patch", "patch"),
            ("fix.diff", "patch"),
            ("nccl.conf.jinja", "template"),
            ("hooks.py", "python"),
            ("config.yaml", "yaml"),
            ("config.yml", "yaml"),
            ("install.sh", "script"),
            ("metrics.json", "file"),
            ("README", "file"),
        ],
    )
    def test_extension_decides_the_kind(self, name, kind):
        assert _asset_kind(name) == kind


# ── Mod info ─────────────────────────────────────────────────────────────────


class TestModInfo:
    def test_run_sh_is_not_listed_among_the_assets(self, tmp_path):
        mod = _write_mod(tmp_path, "m", fix__patch="--- a\n", hooks__py="x = 1\n")

        info = _mod_info(mod)

        assert [f["name"] for f in info["files"]] == ["fix.patch", "hooks.py"]
        assert info["has_patches"] is True
        assert "script" not in info

    def test_subdirectories_are_not_listed_among_the_assets(self, tmp_path):
        mod = _write_mod(tmp_path, "m")
        (mod / "nested").mkdir()

        assert _mod_info(mod)["files"] == []

    def test_has_patches_is_false_without_a_patch(self, tmp_path):
        mod = _write_mod(tmp_path, "m", hooks__py="x = 1\n")

        assert _mod_info(mod)["has_patches"] is False

    def test_the_script_is_included_only_when_asked_for(self, tmp_path):
        mod = _write_mod(tmp_path, "m", script="#!/bin/bash\necho hi\n")

        assert _mod_info(mod, include_script=True)["script"] == (
            "#!/bin/bash\necho hi\n"
        )

    def test_a_mod_without_run_sh_still_describes_itself(self, tmp_path):
        mod = tmp_path / "no-script"
        mod.mkdir()
        (mod / "notes.md").write_text("hello")

        info = _mod_info(mod, include_script=True)

        assert info["description"] == ""
        assert "script" not in info


# ── Listing ──────────────────────────────────────────────────────────────────


class TestModDirs:
    def test_without_a_checkout_only_the_operators_own_directory_is_searched(
        self, tmp_path, monkeypatch, custom
    ):
        monkeypatch.setitem(config._data, "spark_vllm_path", "")

        assert _mod_dirs() == [(custom, custom_files.CUSTOM_PREFIX)]

    def test_with_a_checkout_the_checkout_comes_first(self, checkout, custom):
        assert _mod_dirs() == [(checkout, ""), (custom, custom_files.CUSTOM_PREFIX)]


class TestListMods:
    def test_checkout_mods_keep_their_bare_id(self, checkout, custom):
        _write_mod(checkout, "nccl-optimization", "# Tune NCCL\n")

        assert [m["id"] for m in list_mods()] == ["nccl-optimization"]

    def test_the_operators_own_mods_are_prefixed(self, checkout, custom):
        _write_mod(custom, "my-mod", "# Mine\n")

        listed = list_mods()

        assert [m["id"] for m in listed] == ["custom-my-mod"]
        assert listed[0]["description"] == "Mine"

    def test_a_missing_checkout_is_skipped_rather_than_raising(
        self, tmp_path, monkeypatch, custom
    ):
        monkeypatch.setitem(config._data, "spark_vllm_path", str(tmp_path / "gone"))

        assert list_mods() == []

    def test_a_missing_custom_directory_is_skipped_rather_than_raising(
        self, tmp_path, monkeypatch, checkout
    ):
        monkeypatch.setattr(custom_files, "_CUSTOM_MODS_DIR", tmp_path / "never-made")
        _write_mod(checkout, "real", "# Real\n")

        assert [m["id"] for m in list_mods()] == ["real"]

    def test_files_and_dot_directories_are_not_mods(self, checkout, custom):
        (checkout / "README.md").write_text("not a mod")
        (checkout / ".hidden").mkdir()
        _write_mod(checkout, "real", "# Real\n")

        assert [m["id"] for m in list_mods()] == ["real"]

    def test_the_same_id_is_listed_once(self, checkout, custom):
        # Both directories hold "shared"; only the checkout's bare id and the
        # operator's prefixed id exist, so a genuine collision needs the same
        # prefix on both sides.
        _write_mod(checkout, "custom-shared", "# From the checkout\n")
        _write_mod(custom, "shared", "# From the operator\n")

        listed = list_mods()

        assert [m["id"] for m in listed] == ["custom-shared"]
        assert listed[0]["description"] == "From the checkout"


class TestGetMod:
    def test_a_checkout_mod_is_returned_with_its_script(self, checkout, custom):
        _write_mod(checkout, "nccl", "#!/bin/bash\n# Tune NCCL\n")

        mod = get_mod("nccl")

        assert mod["id"] == "nccl"
        assert mod["description"] == "Tune NCCL"
        assert mod["script"] == "#!/bin/bash\n# Tune NCCL\n"

    def test_the_prefix_is_stripped_before_looking_on_disk(self, checkout, custom):
        _write_mod(custom, "mine", "# Mine\n")

        mod = get_mod("custom-mine")

        assert mod["id"] == "custom-mine"
        assert mod["description"] == "Mine"

    def test_an_unprefixed_id_is_never_looked_for_in_the_custom_directory(
        self, checkout, custom
    ):
        _write_mod(custom, "mine", "# Mine\n")

        assert get_mod("mine") is None

    def test_an_unknown_mod_is_none(self, checkout, custom):
        assert get_mod("nope") is None

    @pytest.mark.parametrize("mod_id", ["../etc", "a/b", "custom-../x"])
    def test_path_traversal_is_refused(self, mod_id, checkout, custom):
        assert get_mod(mod_id) is None


# ── Orchestration edge cases ─────────────────────────────────────────────────
#
# ``tests/test_tools_mods.py`` covers the happy path across head and workers.
# What was left untested is what happens when a node refuses the mod, and
# whether the files that travel *with* run.sh actually make the trip.


class _Node:
    def __init__(self, ip: str, container_name: str):
        self.ip = ip
        self.container_name = container_name


class _Cluster:
    def __init__(self):
        self.head = _Node("10.0.0.1", "head-container")
        self.workers = [_Node("10.0.0.2", "worker0-container")]


class _Service:
    """A node-bound container service that records, and optionally refuses."""

    def __init__(self, calls, host, failing_hosts):
        self._calls = calls
        self._host = host
        self._failing = failing_hosts

    def exec_in_container(self, container, command, detach=False, timeout=None):
        if self._host in self._failing:
            raise RuntimeError(f"{self._host} is unreachable")
        self._calls.append(("exec", self._host, container, tuple(command)))

    def copy_to_container(self, container, local_path, remote_path, timeout=120):
        self._calls.append(("copy", self._host, container, remote_path))
        return True


class _Services:
    def __init__(self, failing_hosts=()):
        self.calls: list[tuple] = []
        self._failing = set(failing_hosts)

    def __call__(self, node):
        return _Service(self.calls, node.address or node.id, self._failing)


class TestOrchestratorFailures:
    def test_a_node_that_refuses_the_mod_is_recorded_as_failed(self, tmp_path):
        from spark_pulse.tools.mods import ModDeployment, ModOrchestrator

        mod = _write_mod(tmp_path, "test-mod")
        services = _Services(failing_hosts={"10.0.0.1"})

        result = ModOrchestrator(services=services).apply_mod_cluster(
            ModDeployment(mod_name="test-mod", mod_path=mod, target="all"),
            _Cluster(),
        )

        assert result.failed_nodes == ["10.0.0.1"]
        assert result.completed_nodes == ["10.0.0.2"]

    def test_every_asset_beside_run_sh_is_copied_to_the_container(self, tmp_path):
        from spark_pulse.tools.mods import ModDeployment, ModOrchestrator

        mod = _write_mod(tmp_path, "test-mod", fix__patch="--- a\n")
        services = _Services()

        ModOrchestrator(services=services).apply_mod_cluster(
            ModDeployment(mod_name="test-mod", mod_path=mod, target="head"),
            _Cluster(),
        )

        copied = [c[3] for c in services.calls if c[0] == "copy"]
        assert copied == [
            "/workspace/mods/test-mod/run.sh",
            "/workspace/mods/test-mod/fix.patch",
        ]

    def test_a_rollback_a_node_refuses_is_not_reported_as_rolled_back(self, tmp_path):
        from spark_pulse.tools.mods import ModDeployment, ModOrchestrator

        mod = _write_mod(tmp_path, "test-mod")
        services = _Services(failing_hosts={"10.0.0.2"})

        rolled_back = ModOrchestrator(services=services).rollback_mod(
            ModDeployment(
                mod_name="test-mod",
                mod_path=mod,
                target="all",
                completed_nodes=["10.0.0.1", "10.0.0.2"],
            ),
            _Cluster(),
        )

        assert rolled_back == ["10.0.0.1"]
