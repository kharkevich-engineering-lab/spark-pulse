"""HuggingFace hub-cache layout: manifests, verification and completion markers.

A hub cache entry is four directories that only mean something together::

    models--org--name/
      blobs/<hash>              content-addressed file, the actual bytes
      snapshots/<commit>/<rel>  a *relative* symlink into ../../blobs/<hash>
      refs/<branch>             a file holding a commit hash
      trees/<commit>.json       the manifest: every file, its size and hashes

The symlinks are what make the tree movable, and the manifest is what makes it
checkable.  Copy the snapshot without the blobs and every link dangles; copy it
with a tool that does not preserve symlinks and the snapshot arrives empty;
copy the blobs without the manifest and nothing can ever prove the copy is
complete.  All four travel together or none of them do.

Verification exists because nothing else in the stack does it.  ``hf`` checks a
file's *size* on download and never a hash, and its "already cached" test is a
path-existence check that follows symlinks, so a truncated blob reads as a
finished download.  :func:`verify_snapshot` compares the tree on disk against
the manifest the hub published for that commit — every file present, every size
exact, every symlink resolving — and, with ``deep``, the hashes too: SHA-256
for LFS/Xet files, git SHA-1 for the rest, which are the two algorithms the
hub itself publishes.

**This module imports nothing but the standard library, and nothing from
spark_pulse.**  That is a hard constraint, not an accident: the same file is
copied to a worker node and run there by the node's system python, so a replica
is verified *on the machine that holds it* rather than by trusting a transfer
that reported success.  It is therefore also runnable as a script::

    python3 hub_cache.py verify --repo <repo dir> [--revision <commit>] \\
        [--deep] [--require-manifest] [--write-marker <json>]

which prints one JSON object on stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any

# ── Layout ───────────────────────────────────────────────────────────────────

#: The four directories that make up a cache entry.  A replication that copies
#: any subset of these is broken in one of the ways described above.
CACHE_SUBDIRS = ("blobs", "snapshots", "refs", "trees")

#: Manifest format version written by ``huggingface_hub``'s tree cache.
TREE_CACHE_FORMAT_VERSION = 1

#: Where our own completion marker lives inside a cache entry.  Dotted so it
#: never collides with a repo-relative filename, and inside the entry so it
#: travels (and is deleted) with it.
MARKER_DIRNAME = ".spark-pulse"
MARKER_FILENAME = "replica.json"
MARKER_VERSION = 1

#: Per-node replication state.  ``partial`` is the state the old
#: directory-exists check could not express and the one that matters.
STATE_ABSENT = "absent"
STATE_PARTIAL = "partial"
STATE_VERIFIED = "verified"

#: How the verdict was reached, weakest first.  ``structure`` means the layout
#: is self-consistent but no manifest was available to check it against, and is
#: never good enough to publish a replica.
EVIDENCE_NONE = "none"
EVIDENCE_STRUCTURE = "structure"
EVIDENCE_MANIFEST = "manifest"
EVIDENCE_HASHES = "hashes"

#: Cap on how many offending paths a report carries, so a wholly-missing tree
#: does not return fifty thousand strings over SSH.
MAX_REPORTED_PATHS = 20

_HEX = set("0123456789abcdef")
_CHUNK = 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def repo_dir_name(model_id: str) -> str:
    """``org/name`` -> ``models--org--name``."""
    return "models--" + model_id.replace("/", "--")


def is_commit_hash(value: str) -> bool:
    """Whether ``value`` looks like a 40-character git commit hash."""
    return len(value) == 40 and all(c in _HEX for c in value.lower())


# ── Manifest ─────────────────────────────────────────────────────────────────


def resolve_commit(repo: str, revision: str | None = None) -> str | None:
    """Resolve ``revision`` to a commit hash inside a cache entry.

    Accepts a commit hash directly, a ref name, or nothing at all — in which
    case ``refs/main`` wins, and failing that a single snapshot directory.  An
    ambiguous entry (several snapshots, no ref) returns ``None`` rather than
    guessing, because guessing is how the wrong revision gets published.
    """
    if revision and is_commit_hash(revision):
        return revision.lower()
    refs = os.path.join(repo, "refs")
    if revision:
        ref_path = os.path.join(refs, revision)
        if os.path.isfile(ref_path):
            return _read_ref(ref_path)
        return None
    main = os.path.join(refs, "main")
    if os.path.isfile(main):
        resolved = _read_ref(main)
        if resolved:
            return resolved
    snapshots = os.path.join(repo, "snapshots")
    try:
        candidates = [
            name
            for name in sorted(os.listdir(snapshots))
            if os.path.isdir(os.path.join(snapshots, name))
        ]
    except OSError:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return None


def _read_ref(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def manifest_path(repo: str, commit: str) -> str:
    """Path of the local manifest for ``commit``."""
    return os.path.join(repo, "trees", f"{commit}.json")


def manifest_unreadable_reason(repo: str, commit: str) -> str | None:
    """Why the manifest could not be read, when it is there but unusable.

    ``None`` means either it was read fine or it genuinely does not exist.
    This distinction matters on a real host: engine containers run as root and
    write into the bind-mounted hub cache, so the manifest routinely lands as
    root-owned mode 600 and the control-plane user cannot read it. Reporting
    that as "no manifest" hides a fixable permissions problem behind what looks
    like a missing feature, and silently drops verification to its weakest
    evidence.
    """
    path = manifest_path(repo, commit)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            handle.read(1)
    except PermissionError:
        try:
            owner = os.stat(path).st_uid
        except OSError:  # pragma: no cover - defensive
            owner = -1
        return (
            f"manifest trees/{commit}.json is not readable by this user "
            f"(owned by uid {owner}); engine containers write the cache as root"
        )
    except OSError as exc:
        return f"manifest trees/{commit}.json could not be read: {exc}"
    return None


def read_manifest(repo: str, commit: str) -> dict[str, dict[str, Any]] | None:
    """Return ``{repo-relative path: entry}`` from ``trees/<commit>.json``.

    ``None`` when the manifest is absent, unreadable, or written in a format
    version we do not know — all three mean "cannot prove anything", which the
    caller must not confuse with "nothing is wrong".
    """
    try:
        with open(manifest_path(repo, commit), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("format_version") != TREE_CACHE_FORMAT_VERSION:
        return None
    files = data.get("files")
    if not isinstance(files, dict):
        return None
    return {
        str(path): entry for path, entry in files.items() if isinstance(entry, dict)
    }


def manifest_bytes(manifest: dict[str, dict[str, Any]]) -> int:
    """Total size the manifest says the revision should occupy."""
    return sum(int(entry.get("size") or 0) for entry in manifest.values())


# ── Hashing ──────────────────────────────────────────────────────────────────


def sha256_of(path: str) -> str:
    """Streaming SHA-256, the algorithm the hub publishes for LFS/Xet files."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_sha1_of(path: str) -> str:
    """Streaming git blob SHA-1, the algorithm the hub publishes for the rest.

    git hashes ``blob <size>\\0<content>``, so the size prefix has to be known
    up front; that is a ``stat`` rather than a second pass over the file.
    """
    size = os.path.getsize(path)
    digest = hashlib.sha1(f"blob {size}\0".encode())  # noqa: S324 — git's format
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_hash(entry: dict[str, Any]) -> tuple[str, str] | None:
    """``(algorithm, digest)`` the hub published for a manifest entry."""
    lfs = entry.get("lfs_sha256")
    if lfs:
        return ("sha256", str(lfs).lower())
    blob_id = entry.get("blob_id")
    if blob_id:
        return ("git-sha1", str(blob_id).lower())
    return None


# ── Verification ─────────────────────────────────────────────────────────────


def _blank_report(state: str, reason: str, revision: str | None) -> dict[str, Any]:
    return {
        "state": state,
        "reason": reason,
        "revision": revision,
        "evidence": EVIDENCE_NONE,
        "files_expected": 0,
        "files_present": 0,
        "bytes_expected": 0,
        "bytes_present": 0,
        "missing": [],
        "missing_count": 0,
        "mismatched": [],
        "mismatched_count": 0,
        "dangling": [],
        "dangling_count": 0,
        "marker": None,
        "verified_at": None,
        "checked_at": _now(),
    }


def verify_snapshot(
    repo: str,
    revision: str | None = None,
    *,
    deep: bool = False,
    require_manifest: bool = False,
) -> dict[str, Any]:
    """Decide whether a cache entry holds a complete, correct revision.

    Three outcomes, and the middle one is the point of the whole exercise:

    * ``absent`` — no entry, or no snapshot for the revision at all.
    * ``partial`` — something is there but it is not the revision: files
      missing, symlinks dangling because the blobs never arrived, a blob
      truncated so its size no longer matches the manifest, or (with ``deep``)
      a blob whose bytes hash to something else.  The report names what is
      wrong.
    * ``verified`` — every file the manifest lists is present at the size it
      lists, reachable through the snapshot's symlinks.

    Args:
        repo: The cache entry directory (``…/hub/models--org--name``).
        revision: Commit hash or ref name; ``None`` resolves ``refs/main``.
        deep: Also hash every file.  Correct but expensive — hundreds of
            gigabytes of SHA-256 — so it is opt-in, and sizes alone already
            catch every truncation, which is the failure this stack produces.
        require_manifest: Refuse to return ``verified`` on structural evidence
            alone.  Replication sets this: it ships ``trees/`` itself, so a
            replica without a manifest means the transfer lost files.

    Returns:
        A report dict; see the module docstring for the CLI that prints it.
    """
    if not os.path.isdir(repo):
        return _blank_report(STATE_ABSENT, "no cache entry", None)

    commit = resolve_commit(repo, revision)
    if commit is None:
        return _blank_report(
            STATE_ABSENT,
            f"no snapshot for revision {revision!r}" if revision else "no snapshot",
            None,
        )

    snapshot = os.path.join(repo, "snapshots", commit)
    report = _blank_report(STATE_PARTIAL, "", commit)
    report["marker"] = read_marker(repo)
    if not os.path.isdir(snapshot):
        report["state"] = STATE_ABSENT
        report["reason"] = f"no snapshot directory for {commit}"
        return report

    manifest = read_manifest(repo, commit)
    if manifest is None:
        return _verify_structurally(repo, commit, snapshot, report, require_manifest)
    return _verify_against_manifest(manifest, snapshot, commit, report, deep)


def _verify_against_manifest(
    manifest: dict[str, dict[str, Any]],
    snapshot: str,
    commit: str,
    report: dict[str, Any],
    deep: bool,
) -> dict[str, Any]:
    """Compare the snapshot against the hub's own manifest for the commit."""
    missing: list[str] = []
    dangling: list[str] = []
    mismatched: list[dict[str, Any]] = []
    present = 0
    bytes_present = 0

    for rel_path in sorted(manifest):
        entry = manifest[rel_path]
        expected_size = int(entry.get("size") or 0)
        local = os.path.join(snapshot, rel_path)
        if not os.path.lexists(local):
            missing.append(rel_path)
            continue
        # A snapshot entry is normally a relative symlink into ../../blobs.
        # The link existing while its target does not is exactly what copying
        # snapshots without blobs produces, and it is not "present".
        if not os.path.exists(local):
            dangling.append(rel_path)
            continue
        try:
            actual_size = os.path.getsize(local)
        except OSError as exc:  # pragma: no cover — races and permissions
            mismatched.append(
                {"path": rel_path, "kind": "unreadable", "detail": str(exc)}
            )
            continue
        present += 1
        bytes_present += actual_size
        if actual_size != expected_size:
            mismatched.append(
                {
                    "path": rel_path,
                    "kind": "size",
                    "expected": expected_size,
                    "actual": actual_size,
                }
            )
            continue
        if deep:
            algorithm_digest = expected_hash(entry)
            if algorithm_digest is None:
                continue
            algorithm, expected_digest = algorithm_digest
            actual_digest = (
                sha256_of(local) if algorithm == "sha256" else git_sha1_of(local)
            )
            if actual_digest != expected_digest:
                mismatched.append(
                    {
                        "path": rel_path,
                        "kind": algorithm,
                        "expected": expected_digest,
                        "actual": actual_digest,
                    }
                )

    report.update(
        {
            "evidence": EVIDENCE_HASHES if deep else EVIDENCE_MANIFEST,
            "files_expected": len(manifest),
            "files_present": present,
            "bytes_expected": manifest_bytes(manifest),
            "bytes_present": bytes_present,
            "missing": missing[:MAX_REPORTED_PATHS],
            "missing_count": len(missing),
            "dangling": dangling[:MAX_REPORTED_PATHS],
            "dangling_count": len(dangling),
            "mismatched": mismatched[:MAX_REPORTED_PATHS],
            "mismatched_count": len(mismatched),
        }
    )
    if missing or dangling or mismatched:
        report["state"] = STATE_PARTIAL
        report["reason"] = _describe(len(missing), len(dangling), len(mismatched))
        return report
    report["state"] = STATE_VERIFIED
    report["reason"] = f"{len(manifest)} files match the manifest for {commit}"
    marker = report.get("marker")
    if isinstance(marker, dict) and marker.get("revision") == commit:
        report["verified_at"] = marker.get("verified_at")
    return report


def _describe(missing: int, dangling: int, mismatched: int) -> str:
    parts = []
    if missing:
        parts.append(f"{missing} file(s) missing")
    if dangling:
        parts.append(f"{dangling} symlink(s) with no blob")
    if mismatched:
        parts.append(f"{mismatched} file(s) do not match")
    return ", ".join(parts)


def _verify_structurally(
    repo: str,
    commit: str,
    snapshot: str,
    report: dict[str, Any],
    require_manifest: bool,
) -> dict[str, Any]:
    """Fall back to checking the layout when no manifest is available.

    This can prove a tree is broken — an empty snapshot, a dangling link, a
    download left ``.incomplete`` — but it cannot prove one is complete, so it
    never satisfies ``require_manifest``.
    """
    dangling: list[str] = []
    present = 0
    bytes_present = 0
    # A broken symlink is not a directory, so os.walk always reports it among
    # ``files`` — which is where the dangling links left by a blob-less copy
    # show up.
    for root, _dirs, files in os.walk(snapshot):
        for name in files:
            path = os.path.join(root, name)
            if not os.path.exists(path):
                dangling.append(os.path.relpath(path, snapshot))
                continue
            present += 1
            try:
                bytes_present += os.path.getsize(path)
            except OSError:  # pragma: no cover — races and permissions
                pass

    incomplete = _incomplete_blobs(repo)
    report.update(
        {
            "evidence": EVIDENCE_STRUCTURE,
            "files_expected": present + len(dangling),
            "files_present": present,
            "bytes_expected": bytes_present,
            "bytes_present": bytes_present,
            "dangling": sorted(set(dangling))[:MAX_REPORTED_PATHS],
            "dangling_count": len(set(dangling)),
        }
    )
    if require_manifest:
        report["state"] = STATE_PARTIAL
        report["reason"] = f"no manifest (trees/{commit}.json) to verify against"
        return report
    if dangling:
        report["state"] = STATE_PARTIAL
        report["reason"] = _describe(0, len(set(dangling)), 0)
        return report
    if incomplete:
        report["state"] = STATE_PARTIAL
        report["reason"] = f"{len(incomplete)} unfinished blob download(s)"
        report["missing"] = incomplete[:MAX_REPORTED_PATHS]
        report["missing_count"] = len(incomplete)
        return report
    if present == 0:
        report["state"] = STATE_PARTIAL
        report["reason"] = "snapshot is empty"
        return report
    report["state"] = STATE_VERIFIED
    unreadable = manifest_unreadable_reason(repo, commit)
    if unreadable:
        report["manifest_unreadable"] = unreadable
        report["reason"] = f"{present} file(s) resolve, but the {unreadable}"
    else:
        report["reason"] = f"{present} file(s) resolve, but no manifest to check them"
    return report


def tree_bytes(path: str) -> dict[str, int]:
    """Apparent bytes and file count under ``path``, each inode counted once.

    ``du`` would do this, except that ``-b`` is GNU-only and the block-based
    answer every ``du`` agrees on is not the number a progress bar wants. The
    verifier is already on the node, so it answers this too and the control
    plane gets the same arithmetic on every platform.
    """
    total = 0
    files = 0
    seen: set[tuple[int, int]] = set()
    for root, _dirs, names in os.walk(path):
        for name in names:
            full = os.path.join(root, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            key = (stat.st_dev, stat.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += stat.st_size
            files += 1
    return {"bytes": total, "files": files}


def _incomplete_blobs(repo: str) -> list[str]:
    """Blob downloads ``huggingface_hub`` left half-written."""
    blobs = os.path.join(repo, "blobs")
    try:
        return sorted(
            name for name in os.listdir(blobs) if name.endswith(".incomplete")
        )
    except OSError:
        return []


# ── Completion marker ────────────────────────────────────────────────────────


def marker_dir(repo: str) -> str:
    return os.path.join(repo, MARKER_DIRNAME)


def marker_path(repo: str) -> str:
    """Where the completion marker for a cache entry lives."""
    return os.path.join(marker_dir(repo), MARKER_FILENAME)


def read_marker(repo: str) -> dict[str, Any] | None:
    """Return the completion marker, or ``None`` when there is none."""
    try:
        with open(marker_path(repo), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_marker(repo: str, payload: dict[str, Any]) -> str:
    """Write the completion marker atomically and return its path.

    The marker is written *into the staging directory*, before the rename that
    publishes it, so that finding a marker in the published entry means the
    rename happened after a verification passed.  A control plane that restarts
    mid-replication can then tell a finished replica from an interrupted one
    without re-reading a hundred gigabytes.
    """
    directory = marker_dir(repo)
    os.makedirs(directory, exist_ok=True)
    body = dict(payload)
    body.setdefault("marker_version", MARKER_VERSION)
    body.setdefault("verified_at", _now())
    handle_fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, indent=1, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, marker_path(repo))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return marker_path(repo)


def marker_payload(
    model_id: str,
    commit: str,
    report: dict[str, Any],
    source: str = "",
) -> dict[str, Any]:
    """The completion marker for a verified replica of ``commit``."""
    return {
        "marker_version": MARKER_VERSION,
        "model": model_id,
        "revision": commit,
        "bytes": int(report.get("bytes_present") or 0),
        "files": int(report.get("files_present") or 0),
        "evidence": report.get("evidence"),
        "verified_at": _now(),
        "source": source,
    }


# ── Script entry point (this file also runs on a worker node) ────────────────


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="verify a hub cache entry")
    verify.add_argument("--repo", required=True, help="the models--org--name directory")
    verify.add_argument("--revision", default=None, help="commit hash or ref name")
    verify.add_argument("--deep", action="store_true", help="hash every file too")
    verify.add_argument(
        "--require-manifest",
        action="store_true",
        help="refuse to pass on structural evidence alone",
    )
    verify.add_argument(
        "--write-marker",
        default=None,
        metavar="JSON",
        help="on success, record this JSON object as the completion marker",
    )
    usage = sub.add_parser("du", help="apparent bytes under a path")
    usage.add_argument("--path", required=True)
    args = parser.parse_args(argv)

    if args.command == "du":
        json.dump(tree_bytes(args.path), sys.stdout)
        sys.stdout.write("\n")
        return 0

    report = verify_snapshot(
        args.repo,
        args.revision,
        deep=args.deep,
        require_manifest=args.require_manifest,
    )
    if args.write_marker and report["state"] == STATE_VERIFIED:
        payload = dict(json.loads(args.write_marker))
        payload.setdefault("revision", report["revision"])
        payload["bytes"] = int(report.get("bytes_present") or 0)
        payload["files"] = int(report.get("files_present") or 0)
        payload["evidence"] = report.get("evidence")
        write_marker(args.repo, payload)
        report["marker"] = read_marker(args.repo)
        report["verified_at"] = (report["marker"] or {}).get("verified_at")
    json.dump(report, sys.stdout)
    sys.stdout.write("\n")
    return 0 if report["state"] == STATE_VERIFIED else 1


if __name__ == "__main__":  # pragma: no cover — exercised over SSH, not in-process
    raise SystemExit(_main())
