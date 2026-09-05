"""The bundle: one static binary, addressed by its own content.

What this file used to assert is worth remembering, because it is the reason
the agent is not Python any more. The bundle was a tar of ``spark_pulse`` plus
every third-party module the agent imported, vendored *out of the control
plane's own environment* — and this suite happily passed while producing a
bundle that could not run anywhere but on a machine identical to the one that
built it. The tests were right about the tar and silent about the thing that
mattered.

So the assertions here are about the properties an installer depends on:
reproducibility (the digest names the directory), the layout the installer
unpacks into, and — the one the old design could not offer — that the payload
needs nothing on the node.
"""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from spark_pulse.agent.bundle import (
    DEFAULT_TARGET,
    VERIFY_COMMAND,
    MissingAgentBinary,
    agent_binary,
    build_bundle,
    cached_bundle,
    prune_cache,
    unpack_for_test,
)
from spark_pulse.version import __version__


def names(bundle) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(bundle.data), mode="r:gz") as tar:
        return sorted(tar.getnames())


def manifest(bundle) -> dict:
    with tarfile.open(fileobj=io.BytesIO(bundle.data), mode="r:gz") as tar:
        return json.loads(tar.extractfile("BUNDLE.json").read())


def test_a_bundle_is_the_binary_and_a_manifest(agent_bundle):
    """Two members. Nothing to interpret, nothing to import, nothing to match."""
    assert names(agent_bundle) == ["BUNDLE.json", "bin/spark-pulse-agent"]


def test_the_binary_is_executable_where_it_lands(agent_bundle, tmp_path):
    unpack_for_test(agent_bundle, tmp_path)
    binary = tmp_path / "bin" / "spark-pulse-agent"
    assert binary.is_file()
    # The installer runs `<bundle>/bin/spark-pulse-agent --help` immediately
    # after unpacking. A payload that arrives without its executable bit fails
    # that check for a reason nobody would guess from the message.
    assert binary.stat().st_mode & 0o111


def test_the_digest_names_the_directory_and_the_bytes_are_reproducible():
    """Two builds of one version must not share a directory, and identical
    inputs must produce identical bytes — otherwise every install writes a new
    directory and the cache never hits."""
    first = build_bundle()
    second = build_bundle()
    assert first.data == second.data
    assert first.digest == second.digest
    assert first.name == f"{__version__}-{first.digest[:12]}"


def test_the_manifest_records_what_a_node_is_being_given(agent_bundle):
    recorded = manifest(agent_bundle)
    assert recorded["version"] == __version__
    assert recorded["target"] == DEFAULT_TARGET
    assert recorded["binary_size"] == agent_bundle.binary_size > 0
    # The binary's own digest, separate from the bundle's: an operator
    # comparing what is on a node with what was built compares this.
    assert len(recorded["binary_sha256"]) == 64


def test_the_target_is_the_nodes_platform_not_the_control_planes():
    """The whole reason for the rewrite, stated as an assertion.

    The bundle is built for aarch64 Linux whatever the control plane is. The
    Python bundle vendored the control plane's own extension modules, so a
    control plane that was not itself a Spark shipped binaries that could not
    load — and nothing in the old suite noticed.
    """
    assert build_bundle().target == "aarch64-unknown-linux-musl"


def test_a_missing_binary_is_refused_rather_than_worked_around(tmp_path):
    """There is no second way to put an agent on a node.

    An installer that appeared to work while shipping something unrunnable is
    worse than one that refuses, so the refusal names the command that builds
    one.
    """
    with pytest.raises(MissingAgentBinary) as caught:
        build_bundle(target="sparc64-unknown-nothing")
    assert "build-agent.sh" in str(caught.value)


def test_the_verify_command_is_what_the_installer_runs():
    assert VERIFY_COMMAND == "--help"


def test_the_cache_returns_the_same_bytes_or_nothing(tmp_path):
    bundle = build_bundle(cache_dir=tmp_path)
    assert cached_bundle(tmp_path, bundle.name) == bundle.data
    assert cached_bundle(tmp_path, "spark-pulse-agent-0.0.0-nothere") is None


def test_pruning_keeps_the_newest(tmp_path):
    for index in range(5):
        (tmp_path / f"spark-pulse-agent-1.0.{index}-abc.tar.gz").write_bytes(b"x")
    assert prune_cache(tmp_path, keep=2) == 3
    assert len(list(tmp_path.glob("*.tar.gz"))) == 2


def test_the_built_binary_is_where_the_installer_looks():
    """Located, not searched for. A bundle assembled from whatever happened to
    be on the path is the failure mode this design removed."""
    assert agent_binary().name == f"spark-pulse-agent-{DEFAULT_TARGET}"
