"""The identity is written as one commit, and a crash cannot tear it.

The same protocol as the Rust agent (``agent/tests/identity_commit.rs``):
every file staged and made durable, ``identity.commit`` marking the staging
complete, then the renames. A load finishes a marked commit and discards an
unmarked one. Before this, a renewal rewrote the four files in place, and a CI
run caught a reader opening ``identity.json`` mid-write and finding it empty.
"""

from __future__ import annotations

import json
from pathlib import Path

from spark_pulse.agent.store import COMMIT_MARKER, FILES, AgentIdentity


def identity(directory: Path, generation: str) -> AgentIdentity:
    return AgentIdentity(
        directory=directory,
        node_id="node-1",
        key_pem=f"key {generation}".encode(),
        certificate_pem=f"cert {generation}".encode(),
        trust_bundle_pem=f"bundle {generation}".encode(),
        trust_bundle_pin="",
        epoch=len(generation),
    )


def stage(directory: Path, generation: str, renamed: tuple[str, ...] = ()) -> None:
    """What a save leaves when the machine dies part way through."""
    contents = {
        "ca.pem": f"bundle {generation}".encode(),
        "node.crt": f"cert {generation}".encode(),
        "node.key": f"key {generation}".encode(),
        "identity.json": json.dumps(
            {"node_id": "node-1", "epoch": len(generation)}
        ).encode(),
    }
    for name, data in contents.items():
        target = directory / (name if name in renamed else f"{name}.next")
        target.write_bytes(data)


def leftovers(directory: Path) -> list[str]:
    return sorted(
        p.name
        for p in directory.iterdir()
        if p.name.endswith(".next") or p.name == COMMIT_MARKER
    )


def test_a_save_is_read_back_and_leaves_no_staging(tmp_path):
    identity(tmp_path, "one").save()
    loaded = AgentIdentity.load(tmp_path)
    assert loaded.key_pem == b"key one"
    assert leftovers(tmp_path) == []
    assert (tmp_path / "node.key").stat().st_mode & 0o777 == 0o600


def test_a_crash_after_the_marker_rolls_forward(tmp_path):
    identity(tmp_path, "one").save()
    stage(tmp_path, "two", renamed=("ca.pem", "node.crt"))
    (tmp_path / COMMIT_MARKER).write_bytes(b"")
    loaded = AgentIdentity.load(tmp_path)
    assert (loaded.key_pem, loaded.certificate_pem) == (b"key two", b"cert two")
    assert loaded.epoch == 3
    assert leftovers(tmp_path) == []


def test_a_crash_before_the_marker_keeps_the_old_identity(tmp_path):
    identity(tmp_path, "one").save()
    stage(tmp_path, "two")
    loaded = AgentIdentity.load(tmp_path)
    assert (loaded.key_pem, loaded.certificate_pem) == (b"key one", b"cert one")
    assert leftovers(tmp_path) == []


def test_a_torn_first_enrolment_is_no_identity(tmp_path):
    stage(tmp_path, "one")
    assert AgentIdentity.load(tmp_path) is None


def test_destroy_takes_a_staging_with_it(tmp_path):
    identity(tmp_path, "one").save()
    stage(tmp_path, "two")
    (tmp_path / COMMIT_MARKER).write_bytes(b"")
    identity(tmp_path, "one").destroy()
    assert AgentIdentity.load(tmp_path) is None


def test_the_two_writers_name_the_same_files():
    """A staging one leaves, the other must recover: same names, same marker."""
    rust = (
        Path(__file__).resolve().parent.parent / "agent" / "src" / "identity.rs"
    ).read_text()
    assert f'pub const COMMIT_MARKER: &str = "{COMMIT_MARKER}";' in rust
    assert 'const STAGED: &str = ".next";' in rust
    for name in FILES:
        assert f'"{name}"' in rust
