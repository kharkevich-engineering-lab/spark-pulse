"""The deployment record store, plus the tombstone for pre-native records.

Every deployment — running, stopped or half-finished — is a row in one JSON
file. :mod:`spark_pulse.tools.native_runtime` owns what those rows *mean*; this
module owns the file: where it lives, that it is written atomically, and that an
unreadable one is an error rather than an empty world.

Real-only on purpose, like ``atomic_json`` and ``labels``: there is no
behaviour here to simulate, only a file. Simulation mode changes *where* the
file is (the gitignored ``spark_pulse/data/`` copy, so an e2e run never touches
an operator's real state) and nothing else, so both modes run this code.

**Legacy records.** Before the native runtime, a deployment was
``run-recipe.sh`` forked out of a spark-vllm-docker checkout, tracked by PID.
That path is gone and cannot be recreated. Its *records* can still be on disk
after an upgrade, with a process still serving, so the minimum that keeps an
operator honest is kept here and nowhere else: see such a deployment, read the
log it already wrote, stop it by the PID it recorded, and delete it. Nothing
can create one.
"""

from __future__ import annotations

import logging
import os
import re
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy import JSON, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from spark_pulse.config import RUNTIME_NATIVE, config
from spark_pulse.db import Base, engine, is_done, mark_done_within, session_scope
from spark_pulse.tools.atomic_json import (
    StateFileError as StateFileError,
    read_state_file,
)

logger = logging.getLogger(__name__)

#: Where the records live. Simulation writes to the gitignored package copy so
#: an e2e run cannot overwrite the deployments of the machine it runs on.
RECORDS_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "deployments.json"
    if os.environ.get("SIMULATION_MODE", "0") == "1"
    else Path.home() / ".config" / "spark-pulse" / "deployments.json"
)

# Matches -e KEY=VALUE and redacts the value. Legacy records stored the whole
# forked command line, tokens included; sanitising on read is what gets those
# out of a file written by an older version.
_SENSITIVE_ENV_RE = re.compile(r"(-e\s+\w+=)\S+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(cmd: str) -> str:
    return _SENSITIVE_ENV_RE.sub(r"\1[REDACTED]", cmd)


# ── The table ────────────────────────────────────────────────────────────────


class DeploymentRow(Base):
    """One deployment, as a row.

    The identifying and queryable fields are columns; the rest of the record
    is a JSON document. That split is deliberate rather than lazy: the record
    shape belongs to :mod:`spark_pulse.tools.native_runtime` and is still
    moving — ranks, orphans, warnings, engine details — while ``id``,
    ``status`` and ``runtime`` are what everything else filters on and what a
    future index would be built over. Normalising a shape that is still
    changing would make every change to it a migration.
    """

    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="", index=True)
    runtime: Mapped[str] = mapped_column(String(32), default="", index=True)
    created_at: Mapped[str] = mapped_column(String(64), default="")
    record: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


def _fields_of(record: dict[str, Any]) -> dict[str, Any]:
    """The columns a record projects onto, denormalised out of the document."""
    return {
        "id": str(record.get("id") or ""),
        "status": str(record.get("status") or ""),
        "runtime": str(record.get("runtime") or ""),
        "created_at": str(record.get("created_at") or ""),
        "record": record,
    }


def _row_of(record: dict[str, Any]) -> DeploymentRow:
    return DeploymentRow(**_fields_of(record))


def _apply(row: DeploymentRow, record: dict[str, Any]) -> bool:
    """Copy ``record`` onto an existing row; ``False`` if nothing differed.

    Assigning an equal-but-distinct dict to a JSON column still marks the
    attribute dirty, so without this comparison a whole-set save that changed
    one deployment emits an UPDATE for every other one — on PostgreSQL a dead
    tuple and a WAL record per untouched deployment, on every backend a write
    amplification that grows with the size of the cluster rather than with the
    size of the change.
    """
    changed = False
    for column, value in _fields_of(record).items():
        if getattr(row, column) != value:
            setattr(row, column, value)
            changed = True
    return changed


#: Recorded once the legacy file has been imported. Keyed in ``meta`` rather
#: than inferred from an empty table: deleting the last deployment empties the
#: table, and an import that re-ran then would resurrect everything.
_IMPORT_KEY = "deployments.imported_from_json"

#: Held for the whole check-claim-import, which every read and every write
#: begins with. ``mark_done`` is a read followed by an insert, so two threads
#: arriving together both see the key missing and both try to write it: one
#: gets the row, the other gets a primary-key violation out of what was
#: supposed to be a question. The startup reconcile and the first API request
#: are exactly that pair.
_IMPORT_LOCK = threading.Lock()


def _write_row(db, record: dict[str, Any]) -> None:
    """Store one record inside the caller's transaction.

    Get-then-write, not ``merge``: see the house rule in :mod:`spark_pulse.db`
    — merge chooses UPDATE from a load it performs itself and then matches
    nothing on PostgreSQL.
    """
    row = db.get(DeploymentRow, str(record.get("id") or ""))
    if row is None:
        db.add(_row_of(record))
    else:
        _apply(row, record)


def _migrate_from_json() -> None:
    """Import ``deployments.json`` once, if there is one and the table is empty.

    An upgrade must not look like an empty cluster. The file is left where it
    is rather than deleted — a downgrade should find it, and an operator
    should be able to see what was imported.
    """
    if is_done(_IMPORT_KEY):
        return
    with _IMPORT_LOCK:
        if is_done(_IMPORT_KEY):
            return  # another thread imported it while this one waited
        data = read_state_file(RECORDS_FILE, expect=list)
        if data is None:
            # No file to import, and nothing to remember: a fresh install must
            # not record an import it never did, or a file restored later is
            # ignored.
            return
        # Marker and rows in one transaction. Claiming first and writing
        # afterwards leaves a window in which the marker says the file was
        # taken while none of it was — and the marker is what stops it being
        # retried, so the records would be gone for good.
        try:
            with session_scope() as db:
                if not mark_done_within(db, _IMPORT_KEY, RECORDS_FILE.name):
                    return
                for record in data:
                    if isinstance(record, dict) and record.get("id"):
                        _write_row(db, record)
        except IntegrityError:
            # A second control plane against the same PostgreSQL claimed it
            # between the read and the insert. Our whole transaction rolled
            # back — marker and rows together — and theirs stands.
            return
    logger.info(
        "imported %d deployment record(s) from %s into the state database",
        len(data),
        RECORDS_FILE,
    )


# ── The file ─────────────────────────────────────────────────────────────────


def load() -> list[dict[str, Any]]:
    """Every persisted record.

    A missing file means "nothing deployed yet" and yields ``[]``. A file that
    exists but cannot be read or parsed raises :class:`StateFileError` — an
    unreadable state file is not an empty cluster, and swallowing the error
    here is what lets a control plane tear down running work.
    """
    _migrate_from_json()
    with session_scope() as db:
        rows = list(db.execute(select(DeploymentRow)).scalars())
    data = [dict(row.record) for row in rows]
    for record in data:
        command = record.get("launch_command") if isinstance(record, dict) else None
        if command and _SENSITIVE_ENV_RE.search(command):
            record["launch_command"] = _redact(command)
            # Per row, not ``save(data)``. A redaction is a repair to the
            # records that need it, and a whole-set save here would delete
            # every deployment another thread created since this load began —
            # on the read path, which callers reasonably assume writes nothing
            # they did not ask for.
            if record.get("id"):
                update(str(record["id"]), launch_command=record["launch_command"])
    return data


#: Held across every read-modify-write of the record file.
#:
#: Each individual write is already atomic, which is a different guarantee and
#: not the one that was missing: a reader never sees a torn file, but two
#: threads that each *load, change, save* will still lose one of the changes,
#: because the second writes back a list it read before the first landed.
#:
#: That is not theoretical here. A deploy runs its pull on a background thread
#: which updates the record as it goes, while the API thread can delete the
#: same deployment at any moment. The interleaving is:
#:
#:     pull thread   records = load()          # the deployment is in this list
#:     API thread    delete_deployment(...)    # filters it out, saves. Gone.
#:     pull thread   save(records)             # writes the deleted one back
#:
#: and it leaves a record with no container behind it — holding its port,
#: listed in the UI, and deletable only by someone who notices. Reentrant
#: because the mutating helpers call one another.
_MUTATION_LOCK = threading.RLock()


def transaction() -> AbstractContextManager[None]:
    """Serialise a read-modify-write of the record file.

    Every caller that loads records, changes them and saves them back must
    hold this for the whole sequence — not merely around the save, which is
    where the atomicity already is.
    """
    return _MUTATION_LOCK


def save(records: list[dict[str, Any]]) -> None:
    """Persist the whole record set, replacing what is stored.

    Kept because callers still hold the whole list — ``native_runtime`` loads
    it, changes it and saves it back — and because "the set is now exactly
    this" is a guarantee :func:`upsert` deliberately does not make: records
    absent from ``records`` are removed. The replacement happens in one
    transaction, so a reader sees the previous set or the new one.

    What it no longer does is *write* the whole set. Only rows that actually
    differ are updated, so the common shape — load the list, change one
    deployment, save it back — costs one row rather than the table.

    Under the mutex, because a save is one half of the read-modify-write the
    mutex exists to serialise, and a caller that holds it already re-enters
    harmlessly.
    """
    wanted = {
        str(r.get("id")): r for r in records if isinstance(r, dict) and r.get("id")
    }
    with transaction():
        with session_scope() as db:
            stored = {
                row.id: row for row in db.execute(select(DeploymentRow)).scalars()
            }
            for row_id, row in stored.items():
                if row_id not in wanted:
                    db.delete(row)
            for record_id, record in wanted.items():
                row = stored.get(record_id)
                if row is None:
                    db.add(_row_of(record))
                else:
                    _apply(row, record)


# ── One row at a time ────────────────────────────────────────────────────────
#
# Everything above treats the record set as one value. That is the right shape
# for a caller that genuinely rewrote the set, and the wrong one for the case
# that actually dominates: a deploy changing the status of the single
# deployment it is running. These are that case, and they are the operations
# to reach for when the caller knows which record it touched.


def upsert(record: dict[str, Any]) -> None:
    """Store one record, leaving every other row exactly as it was.

    Unlike :func:`save`, this never deletes: a record that is not in the
    argument is one this call has no opinion about, not one to remove. That
    difference is the point — it is what makes a per-row write safe to issue
    from a caller holding a partial view of the set.
    """
    deployment_id = str(record.get("id") or "")
    if not deployment_id:
        # ``save`` skips these silently because they are noise in a whole set.
        # A per-row write of one is a caller bug that would otherwise look
        # like a successful write and lose the record.
        raise ValueError("a deployment record needs an id to be stored")
    with transaction():
        _migrate_from_json()
        with session_scope() as db:
            row = db.get(DeploymentRow, deployment_id)
            if row is None:
                db.add(_row_of(record))
            else:
                _apply(row, record)


def update(deployment_id: str, **fields: Any) -> dict[str, Any] | None:
    """Merge ``fields`` into one stored record. ``None`` if it is not there.

    This is a read-modify-write, which is precisely what the mutex exists for,
    so it happens inside the mutex and against what is *stored now* rather
    than against a copy the caller loaded earlier — a caller cannot use this
    to write a stale field back over somebody else's change, only the fields
    it named.

    The row is also selected ``FOR UPDATE``, which SQLAlchemy renders for
    PostgreSQL and omits for SQLite. The mutex serialises one process; the
    reason PostgreSQL is a supported backend at all is the day there is more
    than one, and a lock held in Python says nothing to the other process.
    """
    with transaction():
        _migrate_from_json()
        with session_scope() as db:
            row = db.get(DeploymentRow, deployment_id, with_for_update=True)
            if row is None:
                return None
            record = {**row.record, **fields}
            _apply(row, record)
            return record


def check_state_file() -> None:
    """Raise :class:`StateFileError` if the record file is unreadable.

    Startup calls this so the control plane refuses to come up with an empty
    view of the world while containers are still running.
    """
    # The JSON file is only consulted while it still exists, to import it.
    # Once state lives in the database, an unreadable *database* is what
    # engine() raises on, and it raises for the same reason: an unreadable
    # state store is not an empty cluster.
    read_state_file(RECORDS_FILE, expect=list)
    engine()


def get(deployment_id: str) -> dict[str, Any] | None:
    """One record by id, exactly as stored."""
    _migrate_from_json()
    with session_scope() as db:
        row = db.get(DeploymentRow, deployment_id)
        return dict(row.record) if row is not None else None


def delete(deployment_id: str) -> bool:
    """Drop a record. ``False`` when there was nothing to drop."""
    return delete_many([deployment_id]) == 1


def delete_many(deployment_ids: Iterable[str]) -> int:
    """Drop several records in one transaction; answers how many existed.

    Retention purging is the caller: naming the rows to remove keeps it from
    having to express "delete these three" as "the set is now these forty",
    which would also delete anything created while it was deciding.
    """
    with transaction():
        _migrate_from_json()
        removed = 0
        with session_scope() as db:
            for deployment_id in deployment_ids:
                row = db.get(DeploymentRow, deployment_id)
                if row is not None:
                    db.delete(row)
                    removed += 1
        return removed


def purge_expired(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop terminal records older than ``job_retention_days``."""
    retention = config.job_retention_days
    if retention <= 0:
        return records
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
    kept: list[dict[str, Any]] = []
    expired: list[str] = []
    for record in records:
        if record.get("status") in ("stopped", "error"):
            stopped_at = record.get("stopped_at")
            if stopped_at:
                try:
                    ts = datetime.fromisoformat(stopped_at)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        if record.get("id"):
                            expired.append(str(record["id"]))
                        continue
                except ValueError:
                    pass
        kept.append(record)
    if expired:
        # The rows this decided are expired, not "the set is now ``kept``":
        # purging runs on the list path, so the set it would write back was
        # read before the purge started and would take a deployment created
        # since with it.
        delete_many(expired)
    return kept


# ── Legacy (pre-native) records ──────────────────────────────────────────────


def is_legacy(record: dict[str, Any] | None) -> bool:
    """Whether this record was made by the removed ``run-recipe.sh`` runner.

    Those records predate the ``runtime`` field, so "not native" is the test.
    """
    return bool(record) and record.get("runtime") != RUNTIME_NATIVE  # type: ignore[union-attr]


def _pid_is_alive(pid: int) -> bool:
    """Whether the PID is still there.

    ``PermissionError`` means the process exists and belongs to somebody else,
    which is *alive*: reporting it dead would tell an operator a GPU is free
    while something is still holding it.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def list_legacy() -> list[dict[str, Any]]:
    """Legacy records, reconciled against the PIDs they recorded.

    A record whose process is gone is marked stopped; one that says ``pending``
    with no PID never got as far as a process and is marked errored. This is
    the only reconciliation these records will ever get.
    """
    with transaction():
        records = load()
        reconciled: list[dict[str, Any]] = []
        for record in records:
            if not is_legacy(record):
                continue
            status = record.get("status")
            if status not in ("running", "pending"):
                continue
            pid = record.get("pid")
            if not pid:
                if status == "pending":
                    record["status"] = "error"
                    record["error_message"] = (
                        "Interrupted: the deployment runtime that started this "
                        "was removed"
                    )
                    record["stopped_at"] = _now()
                    reconciled.append(record)
                continue
            if not _pid_is_alive(int(pid)):
                record["status"] = "stopped"
                record.setdefault("stopped_at", _now())
                reconciled.append(record)
            elif status == "pending":
                record["status"] = "running"
                reconciled.append(record)
        for record in reconciled:
            # Only the records this sweep decided something about. A native
            # deployment created while the PIDs were being probed is none of
            # this function's business, and a whole-set save would delete it.
            upsert(record)
        return [r for r in purge_expired(records) if is_legacy(r)]


def stop_legacy(deployment_id: str) -> dict[str, Any] | None:
    """Signal a legacy deployment's process group and mark it stopped.

    SIGTERM to the process group, because that is how the runner started it —
    ``start_new_session=True``, so the whole tree goes. A PID that is already
    gone is success: the point is that the record and the machine agree.
    """
    with transaction():
        record = get(deployment_id)
        if record is None:
            return None
        pid = record.get("pid")
        if pid:
            try:
                os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        # The signal and the record change are one step, so a concurrent
        # delete cannot land between them and leave a signalled process with
        # no record; and it is a two-field update of one row, not a rewrite of
        # every deployment on the machine.
        return update(deployment_id, status="stopped", stopped_at=_now())


#: How much of a log file the tail may read. An engine log runs to gigabytes;
#: reading it whole to show 200 lines would be the last thing this process did.
_TAIL_BYTES = 1024 * 1024


def logs_legacy(deployment_id: str, lines: int = 200) -> str:
    """The tail of the log file the removed runner wrote for this deployment."""
    record = get(deployment_id)
    if not record:
        return "Deployment not found"
    log_path = record.get("log_path")
    if not log_path or not Path(log_path).is_file():
        return f"No log file for deployment {deployment_id}"
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return "Failed to read log file"
    return "\n".join(text.splitlines()[-lines:]) or "(empty log)"


def live_legacy_ids() -> list[str]:
    """Ids of legacy deployments whose process is still running.

    Startup says these out loud. They cannot be recreated, so an operator who
    does not know they are there is an operator with a GPU held by something
    invisible.
    """
    try:
        return [
            str(r.get("id"))
            for r in list_legacy()
            if r.get("status") in ("running", "pending")
        ]
    except StateFileError:
        raise
    except Exception:  # pragma: no cover - defensive
        return []
