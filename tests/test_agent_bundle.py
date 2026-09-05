"""What gets copied to a node.

The packaging decision (``spark_pulse/agent/bundle.py``) is: one self-contained
tarball, unpacked into a versioned directory, run by the node's own python with
``vendor/`` ahead of the system path. No pip, no venv, no index — because an
air-gapped fabric link is normal on this hardware and every step ``pip`` would
take is a step that can fail on a machine with no way to fetch a fix.

These tests hold the properties that decision rests on.
"""

from __future__ import annotations

import io
import json
import tarfile

from spark_pulse.agent.bundle import (
    LAUNCHER,
    build_bundle,
    prune_cache,
    unpack_for_test,
)
from spark_pulse.version import __version__


def names(bundle) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(bundle.data), mode="r:gz") as tar:
        return set(tar.getnames())


def test_the_bundle_holds_the_agent_a_launcher_and_a_manifest():
    bundle = build_bundle(include_runtime=False)
    members = names(bundle)
    assert "bin/spark-pulse-agent" in members
    assert "BUNDLE.json" in members
    assert "lib/spark_pulse/agent/node_agent.py" in members
    assert "lib/spark_pulse/agent/agent.proto" in members
    assert bundle.version == __version__


def test_build_output_is_deterministic_so_the_directory_name_is_stable():
    """Two builds of the same inputs are the same bytes.

    The install directory is ``<version>-<digest>``, so a non-deterministic
    build would put every reinstall in a new directory and never reuse a cached
    bundle.
    """
    first = build_bundle(include_runtime=False)
    second = build_bundle(include_runtime=False)
    assert first.digest == second.digest
    assert first.name == second.name
    assert first.name.startswith(f"{__version__}-")


def test_the_compiled_ui_is_never_shipped_to_a_node():
    """Tens of megabytes of SPA that a node has no use for."""
    members = names(build_bundle(include_runtime=False))
    assert not any(name.startswith("lib/spark_pulse/ui") for name in members)
    assert not any("__pycache__" in name for name in members)
    assert not any(name.endswith(".pyc") for name in members)


def test_a_named_runtime_module_is_vendored_from_this_machine():
    """The third-party bytes come from the control plane, not from PyPI."""
    bundle = build_bundle(runtime_modules=("shlex",))
    assert bundle.includes_runtime
    assert "vendor/shlex.py" in names(bundle)
    assert bundle.runtime_modules == ("shlex",)


def test_a_module_that_is_not_here_is_recorded_rather_than_fatal():
    """The node may already have it, and the import check is what decides."""
    bundle = build_bundle(runtime_modules=("no_such_module_anywhere",))
    assert bundle.missing_modules == ("no_such_module_anywhere",)
    assert bundle.includes_runtime is False
    manifest = json.loads(
        tarfile.open(fileobj=io.BytesIO(bundle.data), mode="r:gz")
        .extractfile("BUNDLE.json")
        .read()
    )
    assert manifest["missing_modules"] == ["no_such_module_anywhere"]


def test_the_launcher_puts_the_bundle_ahead_of_the_system_path():
    assert "sys.path.insert(0, vendor)" in LAUNCHER
    assert 'sys.path.insert(0, os.path.join(root, "lib"))' in LAUNCHER
    assert "from spark_pulse.agent.__main__ import main" in LAUNCHER


def test_unpacking_produces_a_runnable_layout(tmp_path):
    bundle = build_bundle(include_runtime=False)
    root = unpack_for_test(bundle, tmp_path / "install")
    launcher = root / "bin" / "spark-pulse-agent"
    assert launcher.exists()
    assert launcher.stat().st_mode & 0o111
    assert (root / "lib" / "spark_pulse" / "agent" / "__main__.py").exists()


def test_the_cache_keeps_the_newest_bundles_and_prunes_the_rest(tmp_path):
    cache = tmp_path / "bundles"
    build_bundle(include_runtime=False, cache_dir=cache)
    for index in range(4):
        (cache / f"spark-pulse-agent-old-{index}.tar.gz").write_bytes(b"x")
    removed = prune_cache(cache, keep=2)
    assert removed == 3
    assert len(list(cache.glob("spark-pulse-agent-*.tar.gz"))) == 2
