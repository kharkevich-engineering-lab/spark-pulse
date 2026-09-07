"""File content in the database, and the cache it is materialised into.

The interesting cases are all about the cache being a cache: it must be
rebuilt when the content changed, left alone when it did not, and pruned when
a file was removed from the scope. A cache that is merely *usually* right is
worse than no cache, because the thing on the far end of this is a mod that
gets executed inside a container.
"""

from __future__ import annotations

import pytest

from spark_pulse import blobs


def test_a_file_round_trips(tmp_path):
    blobs.put("mod:demo", "run.sh", b"#!/bin/sh\necho hi\n", mode=0o755)

    assert blobs.get("mod:demo", "run.sh") == b"#!/bin/sh\necho hi\n"


def test_listing_reports_paths_digests_and_modes(tmp_path):
    blobs.put("mod:demo", "run.sh", b"a", mode=0o755)
    blobs.put("mod:demo", "template.jinja", b"b")

    listed = blobs.listing("mod:demo")

    assert [p for p, _d, _m in listed] == ["run.sh", "template.jinja"]
    assert dict((p, m) for p, _d, m in listed)["run.sh"] == 0o755


def test_scopes_do_not_see_each_other(tmp_path):
    blobs.put("mod:one", "run.sh", b"one")
    blobs.put("mod:two", "run.sh", b"two")

    assert blobs.get("mod:one", "run.sh") == b"one"
    assert blobs.get("mod:two", "run.sh") == b"two"


def test_removing_a_scope_takes_all_of_it(tmp_path):
    blobs.put("mod:demo", "run.sh", b"a")
    blobs.put("mod:demo", "nested/thing.txt", b"b")

    assert blobs.remove_scope("mod:demo") == 2

    assert blobs.listing("mod:demo") == []


# ── Paths that would escape ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hostile",
    ["/etc/passwd", "../outside", "nested/../../outside", "", "./../x"],
)
def test_a_path_that_escapes_its_scope_is_refused(hostile, tmp_path):
    """The same rule the agent applies to an incoming tar.

    What is on the other end of this is a directory on disk and then a
    container, and an absolute or climbing path reaches outside both.
    """
    with pytest.raises(blobs.UnsafeBlobPath):
        blobs.put("mod:demo", hostile, b"x")


def test_an_ordinary_nested_path_is_allowed(tmp_path):
    blobs.put("mod:demo", "patches/vllm/fix.diff", b"x")

    assert blobs.get("mod:demo", "patches/vllm/fix.diff") == b"x"


# ── The cache ───────────────────────────────────────────────────────────────


def test_materialize_writes_the_scope_to_disk(tmp_path):
    blobs.put("mod:demo", "run.sh", b"#!/bin/sh\n", mode=0o755)
    blobs.put("mod:demo", "nested/template.jinja", b"tmpl")

    root = blobs.materialize("mod:demo", tmp_path / "cache")

    assert (root / "run.sh").read_bytes() == b"#!/bin/sh\n"
    assert (root / "nested" / "template.jinja").read_bytes() == b"tmpl"
    assert (root / "run.sh").stat().st_mode & 0o777 == 0o755


def test_a_warm_cache_is_not_rewritten(tmp_path):
    """Unchanged content must not be rewritten.

    Asserted through mtime rather than by counting writes, because what would
    break is exactly what an observer sees: a mod directory that looks
    modified on every deploy invalidates anything downstream that watches it.
    """
    blobs.put("mod:demo", "run.sh", b"same")
    root = blobs.materialize("mod:demo", tmp_path / "cache")
    stamp = (root / "run.sh").stat().st_mtime_ns

    blobs.materialize("mod:demo", root)

    assert (root / "run.sh").stat().st_mtime_ns == stamp


def test_changed_content_replaces_the_cached_copy(tmp_path):
    blobs.put("mod:demo", "run.sh", b"old")
    root = blobs.materialize("mod:demo", tmp_path / "cache")

    blobs.put("mod:demo", "run.sh", b"new")
    blobs.materialize("mod:demo", root)

    assert (root / "run.sh").read_bytes() == b"new"


def test_a_file_removed_from_the_scope_is_removed_from_the_cache(tmp_path):
    """A mod that dropped a script must not keep running it."""
    blobs.put("mod:demo", "run.sh", b"keep")
    blobs.put("mod:demo", "old.sh", b"gone")
    root = blobs.materialize("mod:demo", tmp_path / "cache")
    assert (root / "old.sh").exists()

    blobs.remove("mod:demo", "old.sh")
    blobs.materialize("mod:demo", root)

    assert not (root / "old.sh").exists()
    assert (root / "run.sh").read_bytes() == b"keep"


def test_a_stale_file_a_local_edit_left_behind_is_corrected(tmp_path):
    """The database is the primary source; the disk is a cache of it.

    Someone editing the cached copy directly must not change what a deploy
    uses, or the two control planes stop agreeing — which is the thing this
    move exists to fix.
    """
    blobs.put("mod:demo", "run.sh", b"authoritative")
    root = blobs.materialize("mod:demo", tmp_path / "cache")
    (root / "run.sh").write_bytes(b"tampered locally")

    blobs.materialize("mod:demo", root)

    assert (root / "run.sh").read_bytes() == b"authoritative"


# ── Importing what is already on disk ───────────────────────────────────────


def test_import_tree_loads_a_directory(tmp_path):
    source = tmp_path / "mod"
    (source / "nested").mkdir(parents=True)
    (source / "run.sh").write_bytes(b"#!/bin/sh\n")
    (source / "run.sh").chmod(0o755)
    (source / "nested" / "t.jinja").write_bytes(b"tmpl")

    assert blobs.import_tree("mod:demo", source) == 2

    assert blobs.get("mod:demo", "run.sh") == b"#!/bin/sh\n"
    assert dict((p, m) for p, _d, m in blobs.listing("mod:demo"))["run.sh"] == 0o755


def test_importing_a_missing_directory_is_not_an_error(tmp_path):
    assert blobs.import_tree("mod:demo", tmp_path / "nope") == 0


def test_a_directory_survives_the_round_trip(tmp_path):
    """Import, materialise elsewhere, and get the same tree back."""
    source = tmp_path / "mod"
    (source / "nested").mkdir(parents=True)
    (source / "run.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    (source / "run.sh").chmod(0o755)
    (source / "nested" / "t.jinja").write_bytes(b"tmpl")

    blobs.import_tree("mod:demo", source)
    root = blobs.materialize("mod:demo", tmp_path / "elsewhere")

    assert (root / "run.sh").read_bytes() == b"#!/bin/sh\necho hi\n"
    assert (root / "run.sh").stat().st_mode & 0o777 == 0o755
    assert (root / "nested" / "t.jinja").read_bytes() == b"tmpl"


def test_a_mode_only_change_reaches_the_cache(tmp_path):
    """Content unchanged, permissions changed — the cache must follow.

    ``materialize`` skips a file whose digest already matches, and the chmod
    used to live only on the write branch. A mod's ``run.sh`` promoted to
    0755 therefore stayed non-executable in the cache: the exact case the
    column's docstring says the mode is stored for.
    """
    blobs.put("mod:demo", "run.sh", b"#!/bin/sh\n", mode=0o644)
    root = blobs.materialize("mod:demo", tmp_path / "cache")
    assert (root / "run.sh").stat().st_mode & 0o777 == 0o644

    blobs.put("mod:demo", "run.sh", b"#!/bin/sh\n", mode=0o755)
    blobs.materialize("mod:demo", root)

    assert (root / "run.sh").stat().st_mode & 0o777 == 0o755


def test_materializing_an_unknown_scope_does_not_erase_the_destination(tmp_path):
    """The sweep reconciles a directory against a scope. With no scope there
    is nothing to reconcile against, and deleting everything is not the
    answer — the caller chose that path, so a typo would take an operator's
    mod directory with it."""
    destination = tmp_path / "not-a-cache"
    destination.mkdir()
    (destination / "precious.txt").write_bytes(b"do not delete me")

    blobs.materialize("mod:never-stored", destination)

    assert (destination / "precious.txt").read_bytes() == b"do not delete me"


def test_the_scope_root_is_not_a_file_in_it(tmp_path):
    """``Path(".").parts`` is empty, so the traversal guard passed it through
    and ``materialize`` then called ``write_bytes`` on a directory."""
    for root_ish in (".", "./"):
        with pytest.raises(blobs.UnsafeBlobPath):
            blobs.put("mod:demo", root_ish, b"x")
