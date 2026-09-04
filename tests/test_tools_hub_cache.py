"""Verification of a HuggingFace hub cache entry against its own manifest.

These are the tests that fail on the old code: it asked whether a snapshot
directory existed, so a truncated blob, a snapshot whose symlinks were dropped
in transit, and a snapshot whose blobs never arrived all answered "present".

Every fixture is a temporary directory shaped like a real hub cache. The
developer's own cache is never touched.
"""

from __future__ import annotations

import ast
import importlib
import json
import sys
from pathlib import Path

import pytest
from hub_cache_fixtures import (
    SAMPLE_COMMIT,
    SAMPLE_FILES,
    SAMPLE_MODEL,
    blob_for,
    build_cache_entry,
    corrupt_in_place,
    git_sha1,
    sample_entry,
    sha256,
    truncate,
)

hub_cache = importlib.import_module("spark_pulse.tools.hub_cache")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A complete, correct cache entry in a temp directory."""
    return sample_entry(tmp_path / "hub")


# ── Layout ───────────────────────────────────────────────────────────────────


class TestLayout:
    def test_repo_dir_name(self):
        assert hub_cache.repo_dir_name("acme/plain-7b") == "models--acme--plain-7b"

    def test_snapshot_entries_are_relative_symlinks_into_blobs(self, repo):
        for name in SAMPLE_FILES:
            link = repo / "snapshots" / SAMPLE_COMMIT / name
            assert link.is_symlink(), f"{name} should be a symlink, not a copy"
            target = Path(link.readlink())
            assert not target.is_absolute(), "the link must be relative to be movable"
            assert link.resolve().parent == repo / "blobs"

    def test_resolve_commit_follows_refs_main(self, repo):
        assert hub_cache.resolve_commit(str(repo)) == SAMPLE_COMMIT

    def test_resolve_commit_accepts_a_hash_directly(self, repo):
        assert hub_cache.resolve_commit(str(repo), SAMPLE_COMMIT) == SAMPLE_COMMIT

    def test_resolve_commit_accepts_a_ref_name(self, repo):
        assert hub_cache.resolve_commit(str(repo), "main") == SAMPLE_COMMIT

    def test_resolve_commit_refuses_to_guess_between_snapshots(self, tmp_path):
        hub = tmp_path / "hub"
        repo = build_cache_entry(hub, SAMPLE_MODEL, "a" * 40, dict(SAMPLE_FILES))
        build_cache_entry(hub, SAMPLE_MODEL, "b" * 40, dict(SAMPLE_FILES), ref="other")
        (repo / "refs" / "main").unlink()
        assert hub_cache.resolve_commit(str(repo)) is None

    def test_resolve_commit_rejects_an_unknown_ref(self, repo):
        assert hub_cache.resolve_commit(str(repo), "nope") is None

    def test_manifest_lists_every_file_with_both_hash_kinds(self, repo):
        manifest = hub_cache.read_manifest(str(repo), SAMPLE_COMMIT)
        assert set(manifest) == set(SAMPLE_FILES)
        assert hub_cache.expected_hash(manifest["config.json"])[0] == "git-sha1"
        weights = manifest["model-00001-of-00002.safetensors"]
        assert hub_cache.expected_hash(weights)[0] == "sha256"

    def test_manifest_of_an_unknown_format_version_is_ignored(self, repo):
        path = Path(hub_cache.manifest_path(str(repo), SAMPLE_COMMIT))
        path.write_text(json.dumps({"format_version": 99, "files": {}}))
        assert hub_cache.read_manifest(str(repo), SAMPLE_COMMIT) is None


# ── Hashing ──────────────────────────────────────────────────────────────────


class TestHashing:
    def test_git_sha1_matches_gits_own_object_id(self, tmp_path):
        path = tmp_path / "f"
        path.write_bytes(b"hello world")
        assert hub_cache.git_sha1_of(str(path)) == git_sha1(b"hello world")

    def test_sha256_matches(self, tmp_path):
        path = tmp_path / "f"
        path.write_bytes(b"hello world")
        assert hub_cache.sha256_of(str(path)) == sha256(b"hello world")


# ── The three states ─────────────────────────────────────────────────────────


class TestVerification:
    def test_a_complete_entry_verifies_against_its_manifest(self, repo):
        report = hub_cache.verify_snapshot(str(repo))
        assert report["state"] == hub_cache.STATE_VERIFIED
        assert report["evidence"] == hub_cache.EVIDENCE_MANIFEST
        assert report["revision"] == SAMPLE_COMMIT
        assert report["files_expected"] == len(SAMPLE_FILES)
        assert report["files_present"] == len(SAMPLE_FILES)
        assert report["bytes_expected"] == sum(len(v) for v in SAMPLE_FILES.values())
        assert report["bytes_present"] == report["bytes_expected"]

    def test_deep_verification_hashes_every_file(self, repo):
        report = hub_cache.verify_snapshot(str(repo), deep=True)
        assert report["state"] == hub_cache.STATE_VERIFIED
        assert report["evidence"] == hub_cache.EVIDENCE_HASHES

    def test_no_entry_at_all_is_absent(self, tmp_path):
        report = hub_cache.verify_snapshot(str(tmp_path / "nothing"))
        assert report["state"] == hub_cache.STATE_ABSENT
        assert report["revision"] is None

    def test_an_unknown_revision_is_absent(self, repo):
        report = hub_cache.verify_snapshot(str(repo), "f" * 40)
        assert report["state"] == hub_cache.STATE_ABSENT

    def test_a_truncated_blob_is_partial(self, repo):
        """The case the old existence check reported as present."""
        blob = blob_for(repo, SAMPLE_COMMIT, "model-00002-of-00002.safetensors")
        truncate(blob, 1024)

        report = hub_cache.verify_snapshot(str(repo))

        assert report["state"] == hub_cache.STATE_PARTIAL
        assert report["mismatched_count"] == 1
        offender = report["mismatched"][0]
        assert offender["path"] == "model-00002-of-00002.safetensors"
        assert offender["kind"] == "size"
        assert offender["expected"] == 8192
        assert offender["actual"] == 1024

    def test_a_snapshot_directory_still_exists_when_the_copy_is_truncated(self, repo):
        """Documents precisely why the old check could not see the defect."""
        truncate(blob_for(repo, SAMPLE_COMMIT, "model-00001-of-00002.safetensors"), 0)
        assert (repo / "snapshots").is_dir()
        assert (repo / "snapshots" / SAMPLE_COMMIT / "config.json").exists()
        assert hub_cache.verify_snapshot(str(repo))["state"] == hub_cache.STATE_PARTIAL

    def test_bytes_that_changed_without_changing_size_need_deep(self, repo):
        corrupt_in_place(blob_for(repo, SAMPLE_COMMIT, "config.json"))

        assert hub_cache.verify_snapshot(str(repo))["state"] == (
            hub_cache.STATE_VERIFIED
        )
        deep = hub_cache.verify_snapshot(str(repo), deep=True)
        assert deep["state"] == hub_cache.STATE_PARTIAL
        assert deep["mismatched"][0]["kind"] == "git-sha1"

    def test_a_corrupted_weight_file_is_caught_by_its_sha256(self, repo):
        corrupt_in_place(
            blob_for(repo, SAMPLE_COMMIT, "model-00001-of-00002.safetensors")
        )
        deep = hub_cache.verify_snapshot(str(repo), deep=True)
        assert deep["state"] == hub_cache.STATE_PARTIAL
        assert deep["mismatched"][0]["kind"] == "sha256"

    def test_snapshots_without_blobs_is_partial_not_present(self, repo):
        """What copying `snapshots` on its own leaves behind: dangling links."""
        for blob in (repo / "blobs").iterdir():
            blob.unlink()

        report = hub_cache.verify_snapshot(str(repo))

        assert report["state"] == hub_cache.STATE_PARTIAL
        assert report["dangling_count"] == len(SAMPLE_FILES)
        assert "no blob" in report["reason"]

    def test_a_snapshot_that_lost_its_symlinks_is_partial(self, repo):
        """What `rsync -r` produces: the links are skipped, the tree is empty."""
        for link in (repo / "snapshots" / SAMPLE_COMMIT).iterdir():
            link.unlink()

        report = hub_cache.verify_snapshot(str(repo))

        assert report["state"] == hub_cache.STATE_PARTIAL
        assert report["missing_count"] == len(SAMPLE_FILES)
        assert sorted(report["missing"]) == sorted(SAMPLE_FILES)

    def test_one_missing_shard_names_the_shard(self, repo):
        (
            repo / "snapshots" / SAMPLE_COMMIT / "model-00002-of-00002.safetensors"
        ).unlink()
        report = hub_cache.verify_snapshot(str(repo))
        assert report["state"] == hub_cache.STATE_PARTIAL
        assert report["missing"] == ["model-00002-of-00002.safetensors"]
        assert report["files_present"] == len(SAMPLE_FILES) - 1


class TestWithoutAManifest:
    """A cache written by an older hub client carries no ``trees/``."""

    @pytest.fixture
    def repo(self, tmp_path):
        return sample_entry(tmp_path / "hub", with_manifest=False)

    def test_structure_alone_is_weaker_evidence(self, repo):
        report = hub_cache.verify_snapshot(str(repo))
        assert report["state"] == hub_cache.STATE_VERIFIED
        assert report["evidence"] == hub_cache.EVIDENCE_STRUCTURE

    def test_replication_refuses_structural_evidence(self, repo):
        report = hub_cache.verify_snapshot(str(repo), require_manifest=True)
        assert report["state"] == hub_cache.STATE_PARTIAL
        assert "no manifest" in report["reason"]

    def test_dangling_links_are_still_caught(self, repo):
        for blob in (repo / "blobs").iterdir():
            blob.unlink()
        report = hub_cache.verify_snapshot(str(repo))
        assert report["state"] == hub_cache.STATE_PARTIAL

    def test_an_empty_snapshot_is_not_verified(self, repo):
        for link in (repo / "snapshots" / SAMPLE_COMMIT).iterdir():
            link.unlink()
        report = hub_cache.verify_snapshot(str(repo))
        assert report["state"] == hub_cache.STATE_PARTIAL
        assert report["reason"] == "snapshot is empty"

    def test_an_unfinished_download_is_not_verified(self, repo):
        (repo / "blobs" / "abc123.incomplete").write_bytes(b"half")
        report = hub_cache.verify_snapshot(str(repo))
        assert report["state"] == hub_cache.STATE_PARTIAL
        assert "unfinished" in report["reason"]


# ── The completion marker ────────────────────────────────────────────────────


class TestMarker:
    def test_round_trips_and_leaves_no_temporary_behind(self, repo):
        payload = hub_cache.marker_payload(
            SAMPLE_MODEL,
            SAMPLE_COMMIT,
            hub_cache.verify_snapshot(str(repo)),
            source="control-1",
        )
        hub_cache.write_marker(str(repo), payload)

        stored = hub_cache.read_marker(str(repo))
        assert stored["revision"] == SAMPLE_COMMIT
        assert stored["model"] == SAMPLE_MODEL
        assert stored["bytes"] == sum(len(v) for v in SAMPLE_FILES.values())
        assert stored["files"] == len(SAMPLE_FILES)
        assert stored["source"] == "control-1"
        assert stored["verified_at"]
        assert stored["marker_version"] == hub_cache.MARKER_VERSION
        leftovers = list(Path(hub_cache.marker_dir(str(repo))).glob("*.tmp"))
        assert leftovers == []

    def test_absent_marker_reads_as_none(self, repo):
        assert hub_cache.read_marker(str(repo)) is None

    def test_a_corrupt_marker_reads_as_none(self, repo):
        path = Path(hub_cache.marker_path(str(repo)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json")
        assert hub_cache.read_marker(str(repo)) is None

    def test_verification_reports_when_the_marker_was_written(self, repo):
        report = hub_cache.verify_snapshot(str(repo))
        hub_cache.write_marker(
            str(repo), hub_cache.marker_payload(SAMPLE_MODEL, SAMPLE_COMMIT, report)
        )
        again = hub_cache.verify_snapshot(str(repo))
        assert again["verified_at"] == hub_cache.read_marker(str(repo))["verified_at"]


# ── The script the node runs ─────────────────────────────────────────────────


class TestCommandLine:
    def _run(self, capsys, *argv):
        code = hub_cache._main(list(argv))
        return code, json.loads(capsys.readouterr().out.strip())

    def test_verify_exits_zero_and_prints_the_report(self, repo, capsys):
        code, report = self._run(capsys, "verify", "--repo", str(repo))
        assert code == 0
        assert report["state"] == hub_cache.STATE_VERIFIED

    def test_verify_exits_nonzero_when_partial(self, repo, capsys):
        truncate(blob_for(repo, SAMPLE_COMMIT, "config.json"), 1)
        code, report = self._run(capsys, "verify", "--repo", str(repo))
        assert code == 1
        assert report["state"] == hub_cache.STATE_PARTIAL

    def test_write_marker_only_fires_on_success(self, repo, capsys):
        truncate(blob_for(repo, SAMPLE_COMMIT, "config.json"), 1)
        payload = json.dumps({"model": SAMPLE_MODEL, "source": "control-1"})
        self._run(capsys, "verify", "--repo", str(repo), "--write-marker", payload)
        assert hub_cache.read_marker(str(repo)) is None

    def test_write_marker_records_the_verified_counts(self, repo, capsys):
        payload = json.dumps({"model": SAMPLE_MODEL, "source": "control-1"})
        _code, report = self._run(
            capsys,
            "verify",
            "--repo",
            str(repo),
            "--revision",
            SAMPLE_COMMIT,
            "--write-marker",
            payload,
        )
        marker = hub_cache.read_marker(str(repo))
        assert marker["revision"] == SAMPLE_COMMIT
        assert marker["bytes"] == sum(len(v) for v in SAMPLE_FILES.values())
        assert marker["files"] == len(SAMPLE_FILES)
        assert report["marker"] == marker

    def test_require_manifest_is_honoured_from_the_command_line(self, tmp_path, capsys):
        repo = sample_entry(tmp_path / "hub", with_manifest=False)
        code, report = self._run(
            capsys, "verify", "--repo", str(repo), "--require-manifest"
        )
        assert code == 1
        assert report["state"] == hub_cache.STATE_PARTIAL

    def test_du_reports_apparent_bytes_counting_each_blob_once(self, repo, capsys):
        code, usage = self._run(capsys, "du", "--path", str(repo))
        assert code == 0
        # Blobs plus refs, manifest and snapshot symlinks; the blobs are
        # counted once even though the snapshot links to them.
        assert usage["bytes"] >= sum(len(v) for v in SAMPLE_FILES.values())
        assert usage["files"] >= len(SAMPLE_FILES)

    def test_du_of_a_missing_path_is_zero(self, tmp_path, capsys):
        _code, usage = self._run(capsys, "du", "--path", str(tmp_path / "nope"))
        assert usage == {"bytes": 0, "files": 0}


def test_the_verifier_imports_only_the_standard_library():
    """It runs on a worker node, by that node's python, with nothing installed.

    Any import outside the stdlib — spark_pulse itself, or huggingface_hub —
    would make the shipped file unrunnable on a bare node, and the whole point
    of shipping it is that the replica is checked where it lives.
    """
    tree = ast.parse(Path(hub_cache.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= set(sys.stdlib_module_names), sorted(
        imported - set(sys.stdlib_module_names)
    )
