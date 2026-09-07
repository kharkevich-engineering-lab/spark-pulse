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
    binary_dir,
    build_bundle,
    cached_bundle,
    host_binary,
    host_target,
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


def test_the_digest_names_the_directory_and_the_bytes_are_reproducible(agent_bundle):
    """Two builds of one version must not share a directory, and identical
    inputs must produce identical bytes — otherwise every install writes a new
    directory and the cache never hits."""
    first = agent_bundle
    second = build_bundle(target=first.target, binary=host_binary())
    assert first.data == second.data
    assert first.digest == second.digest
    assert first.name == f"{__version__}-{first.digest[:12]}"


def test_the_manifest_records_what_a_node_is_being_given(agent_bundle):
    recorded = manifest(agent_bundle)
    assert recorded["version"] == __version__
    assert recorded["target"] == agent_bundle.target
    assert recorded["binary_size"] == agent_bundle.binary_size > 0
    # The binary's own digest, separate from the bundle's: an operator
    # comparing what is on a node with what was built compares this.
    assert len(recorded["binary_sha256"]) == 64


def test_the_target_is_the_nodes_platform_not_the_control_planes():
    """The whole reason for the rewrite, stated as an assertion.

    What a bundle ships by default is aarch64 Linux — the *node's* platform —
    whatever the control plane happens to be. The Python bundle vendored the
    control plane's own extension modules, so a control plane that was not
    itself a Spark shipped objects that could not load, and nothing in the old
    suite noticed.

    Asserted on the constant rather than by building one, because building for
    a foreign target on a machine that is not it is a fifteen-minute emulated
    compile — and it is the *default* that carries the meaning.
    """
    assert DEFAULT_TARGET == "aarch64-unknown-linux-musl"
    assert "musl" in DEFAULT_TARGET, "the node must not depend on its own libc"


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


def test_the_cache_returns_the_same_bytes_or_nothing(tmp_path, a_runnable_agent_binary):
    bundle = build_bundle(
        target=host_target(), binary=host_binary(), cache_dir=tmp_path
    )
    assert cached_bundle(tmp_path, bundle.name) == bundle.data
    assert cached_bundle(tmp_path, "spark-pulse-agent-0.0.0-nothere") is None


def test_pruning_keeps_the_newest(tmp_path):
    for index in range(5):
        (tmp_path / f"spark-pulse-agent-1.0.{index}-abc.tar.gz").write_bytes(b"x")
    assert prune_cache(tmp_path, keep=2) == 3
    assert len(list(tmp_path.glob("*.tar.gz"))) == 2


def test_a_binary_is_located_by_triple_never_searched_for(a_runnable_agent_binary):
    """A bundle assembled from whatever happened to be on the path is the
    failure mode this design removed: the old one vendored the control plane's
    own site-packages and shipped whatever it found there.

    ``host_binary`` never falls back to the *node's* triple either. On a Spark
    the two are the same string, so a fallback buys nothing; anywhere else it
    would hand this machine a binary for another operating system, and the
    failure would be an exec error with no clue in it.
    """
    # One directory, one name per triple. Asserted through the refusal,
    # because that is observable whether or not a binary for that triple has
    # been built here — and the path it names is what an operator will go
    # looking in.
    with pytest.raises(MissingAgentBinary) as caught:
        agent_binary("sparc64-unknown-nothing")
    expected = binary_dir() / "spark-pulse-agent-sparc64-unknown-nothing"
    assert str(expected) in str(caught.value)

    # And the control node's own agent is found, wherever it came from: the
    # packaged binary for this triple, or a local cargo build.
    assert host_binary().is_file()
    assert host_target().endswith(("-linux-musl", "-apple-darwin"))


def test_unpacking_refuses_a_sibling_that_merely_shares_a_prefix(tmp_path):
    """The traversal guard compared strings, not paths.

    ``str(target).startswith(str(destination))`` accepts ``/tmp/dest-evil``
    for a destination of ``/tmp/dest``, because the separator is not part of
    the comparison — so a member named ``../dest-evil/x`` escaped a check
    written to stop exactly that.
    """
    import tarfile as tar_module

    from spark_pulse.agent.bundle import AgentBundle, unpack_for_test

    destination = tmp_path / "dest"
    destination.mkdir()
    (tmp_path / "dest-evil").mkdir()

    buffer = io.BytesIO()
    with tar_module.open(fileobj=buffer, mode="w:gz") as tar:
        info = tar_module.TarInfo("../dest-evil/planted")
        payload = b"planted"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    bundle = AgentBundle(
        version="0", digest="0" * 64, data=buffer.getvalue(), target="t"
    )

    with pytest.raises(RuntimeError, match="path traversal"):
        unpack_for_test(bundle, destination)
    assert not (tmp_path / "dest-evil" / "planted").exists()


def test_unpacking_an_ordinary_bundle_still_works(tmp_path):
    """The guard must not refuse the layout every install depends on."""
    from spark_pulse.agent.bundle import build_bundle, unpack_for_test

    binary = tmp_path / "fake-agent"
    binary.write_bytes(b"\x7fELF not really")
    bundle = build_bundle(target="aarch64-unknown-linux-musl", binary=binary)

    destination = unpack_for_test(bundle, tmp_path / "unpacked")

    assert (
        destination / "bin" / "spark-pulse-agent"
    ).read_bytes() == binary.read_bytes()
    assert json.loads((destination / "BUNDLE.json").read_text())["binary_size"] == 15
