"""File content in the database, materialised to disk on demand.

Recipes and mods are *files*: a recipe is a YAML document an operator edits, a
mod is a directory copied wholesale into a container by
``native_runtime._apply_mods``. Everything that consumes them takes a
``Path`` — ``mod_dir.iterdir()``, ``docker.copy_to_container(str(path), …)`` —
and that is not an accident worth undoing, because the thing on the far end is
a container filesystem.

So they cannot simply move into the database the way a deployment record did.
What they can do is change which copy is *authoritative*. Here the database
holds the content and the filesystem holds a cache of it, rebuilt when the
digest says it is stale. One control plane behaves exactly as it does today,
because its cache is always warm. Several control planes finally agree with
each other, because an operator who uploads a mod to one of them is not
uploading it to only one of them any more — which is the whole reason this
moves at all.

**Not encrypted.** Recipes and mods are configuration an operator wrote and
can read back through the UI; they are not secrets, and encrypting them would
cost the ability to inspect the store without the key while protecting
something that is not sensitive. Secrets go through
:mod:`spark_pulse.crypto` instead, and the two must not be confused: if a
recipe ever carries a token, that token belongs in the secret store and the
recipe should reference it by name.

**Digest-addressed, not timestamp-addressed.** A cache keyed on mtime is a
cache that is wrong whenever two instances write the same second, or a clock
moves. The digest is of the content, so "is this stale" has an answer that
does not depend on anybody's clock.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable

from sqlalchemy import Integer, LargeBinary, String, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from spark_pulse.db import Base, session_scope

__all__ = [
    "Blob",
    "put",
    "get",
    "listing",
    "remove",
    "remove_scope",
    "digest_of",
    "materialize",
    "import_tree",
]


def digest_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class Blob(Base):
    """One file, addressed by the scope it belongs to and its relative path.

    ``LargeBinary`` rather than ``Text``: a mod directory can hold a chat
    template, a shell script and a wheel, and deciding which of those is text
    is a decision that only has to be wrong once.
    """

    __tablename__ = "blobs"

    #: What this file belongs to — ``mod:my-mod``, ``recipe:custom/foo``.
    #: Scoped rather than one flat namespace so a whole mod is one delete.
    scope: Mapped[str] = mapped_column(String(255), primary_key=True)
    #: Path relative to the scope's root. Never absolute, never containing
    #: ``..`` — checked on the way in, because the far end of this is a
    #: filesystem and eventually a container.
    #: ``String(1024)`` would be a cap only PostgreSQL enforces; a deep mod
    #: tree is a real path. It stays a ``String`` because it is a primary key
    #: and PostgreSQL will not index an unbounded ``Text`` beyond ~2704 bytes
    #: — 2048 is well inside that and well past any real relative path.
    path: Mapped[str] = mapped_column(String(2048), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
    #: The executable bit matters: a mod's ``run.sh`` is executed.
    mode: Mapped[int] = mapped_column(Integer, default=0o644)
    digest: Mapped[str] = mapped_column(String(64), default="", index=True)


class UnsafeBlobPath(ValueError):
    """A relative path that would escape the scope it is written into."""


def _checked(path: str) -> str:
    """``path`` as a safe relative path, or a refusal.

    The same rule the agent applies to an incoming tar, for the same reason:
    what is on the other end of this is a directory on disk and then a
    container, and an absolute or climbing path reaches outside both.
    """
    candidate = Path(path)
    if not path or candidate.is_absolute():
        raise UnsafeBlobPath(f"{path!r} is not a relative path")
    if any(part in ("..", "") for part in candidate.parts):
        raise UnsafeBlobPath(f"{path!r} escapes its scope")
    # ``Path(".").parts`` is empty, so the check above passes it. It names the
    # scope root, and materialising it would call ``write_bytes`` on a
    # directory.
    if candidate.as_posix() in (".", ""):
        raise UnsafeBlobPath(f"{path!r} names the scope root, not a file in it")
    return candidate.as_posix()


def put(scope: str, path: str, content: bytes, *, mode: int = 0o644) -> str:
    """Store one file and return its digest."""
    safe = _checked(path)
    fingerprint = digest_of(content)
    with session_scope() as db:
        # An explicit get-then-write rather than ``Session.merge``. merge
        # decides between UPDATE and INSERT from a load it performs itself,
        # and on PostgreSQL that produced
        # ``StaleDataError: UPDATE expected to update 1 row(s); 0 were
        # matched`` — it had decided to update a row its own load had seen and
        # the statement then matched nothing. Doing the decision here makes it
        # the transaction's, which is where it can actually be held.
        row = db.get(Blob, (scope, safe))
        if row is None:
            db.add(
                Blob(
                    scope=scope,
                    path=safe,
                    content=content,
                    mode=mode,
                    digest=fingerprint,
                )
            )
        else:
            row.content = content
            row.mode = mode
            row.digest = fingerprint
    return fingerprint


def get(scope: str, path: str) -> bytes | None:
    with session_scope() as db:
        row = db.get(Blob, (scope, _checked(path)))
        return bytes(row.content) if row is not None else None


def listing(scope: str) -> list[tuple[str, str, int]]:
    """``(path, digest, mode)`` for every file in ``scope``, sorted."""
    with session_scope() as db:
        rows = db.execute(select(Blob).where(Blob.scope == scope)).scalars()
        return sorted((row.path, row.digest, row.mode) for row in rows)


def remove(scope: str, path: str) -> bool:
    with session_scope() as db:
        row = db.get(Blob, (scope, _checked(path)))
        if row is None:
            return False
        db.delete(row)
        return True


def remove_scope(scope: str) -> int:
    """Drop every file in a scope — deleting a mod, in one statement."""
    with session_scope() as db:
        result = db.execute(delete(Blob).where(Blob.scope == scope))
        return int(result.rowcount or 0)


def materialize(scope: str, destination: Path) -> Path:
    """Write ``scope`` into ``destination`` and return it.

    Only what changed is written: a file whose on-disk content already hashes
    to the stored digest is left alone, so a warm cache costs one read per
    file and no writes. Files present on disk but no longer in the scope are
    removed, because a mod that dropped a script must not keep running it.
    """
    destination.mkdir(parents=True, exist_ok=True)
    wanted: dict[str, tuple[str, int]] = {}
    with session_scope() as db:
        rows = list(db.execute(select(Blob).where(Blob.scope == scope)).scalars())
        for row in rows:
            wanted[row.path] = (row.digest, row.mode)
            target = destination / row.path
            if target.is_file() and digest_of(target.read_bytes()) == row.digest:
                # Content is current; the mode may not be. A ``put`` that only
                # changes permissions leaves the digest alone, and skipping
                # outright meant a mod's ``run.sh`` promoted to 0755 stayed
                # non-executable in the cache — the one case the column's own
                # docstring calls out.
                if target.stat().st_mode & 0o777 != row.mode:
                    os.chmod(target, row.mode)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes(row.content))
            os.chmod(target, row.mode)

    if not wanted:
        # Nothing is stored under this scope, so there is nothing to reconcile
        # the directory against. Sweeping here would delete every file in
        # ``destination`` — and the caller chose that path, so a typo or a
        # scope that does not exist yet would erase an operator's mod
        # directory rather than produce an empty cache.
        return destination

    for existing in sorted(destination.rglob("*"), reverse=True):
        if existing.is_file():
            relative = existing.relative_to(destination).as_posix()
            if relative not in wanted:
                existing.unlink()
        elif existing.is_dir() and not any(existing.iterdir()):
            existing.rmdir()
    return destination


def import_tree(
    scope: str, source: Path, *, paths: Iterable[Path] | None = None
) -> int:
    """Load a directory into ``scope``. The migration path for what is on disk.

    Returns how many files were stored. Symlinks are read through rather than
    recorded: what a container needs is the bytes, and a link into the
    operator's home directory would not resolve there anyway.
    """
    source = Path(source)
    if not source.is_dir():
        return 0
    stored = 0
    for item in sorted(paths if paths is not None else source.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(source).as_posix()
        put(scope, relative, item.read_bytes(), mode=item.stat().st_mode & 0o777)
        stored += 1
    return stored
