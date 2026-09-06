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

import os
import re
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from contextlib import AbstractContextManager
from typing import Any

from spark_pulse.config import RUNTIME_NATIVE, config
from spark_pulse.tools.atomic_json import (
    StateFileError as StateFileError,
    read_state_file,
    write_json_atomic,
)

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


# ── The file ─────────────────────────────────────────────────────────────────


def load() -> list[dict[str, Any]]:
    """Every persisted record.

    A missing file means "nothing deployed yet" and yields ``[]``. A file that
    exists but cannot be read or parsed raises :class:`StateFileError` — an
    unreadable state file is not an empty cluster, and swallowing the error
    here is what lets a control plane tear down running work.
    """
    data = read_state_file(RECORDS_FILE, expect=list)
    if data is None:
        return []
    changed = False
    for record in data:
        command = record.get("launch_command") if isinstance(record, dict) else None
        if command and _SENSITIVE_ENV_RE.search(command):
            record["launch_command"] = _redact(command)
            changed = True
    if changed:
        save(data)
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
    """Persist the records: temp file, fsync, replace, directory fsync."""
    write_json_atomic(RECORDS_FILE, records, indent=2, default=str)


def check_state_file() -> None:
    """Raise :class:`StateFileError` if the record file is unreadable.

    Startup calls this so the control plane refuses to come up with an empty
    view of the world while containers are still running.
    """
    read_state_file(RECORDS_FILE, expect=list)


def get(deployment_id: str) -> dict[str, Any] | None:
    """One record by id, exactly as stored."""
    return next((r for r in load() if r.get("id") == deployment_id), None)


def delete(deployment_id: str) -> bool:
    """Drop a record. ``False`` when there was nothing to drop."""
    with transaction():
        records = load()
        remaining = [r for r in records if r.get("id") != deployment_id]
        if len(remaining) == len(records):
            return False
        save(remaining)
        return True


def purge_expired(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop terminal records older than ``job_retention_days``."""
    retention = config.job_retention_days
    if retention <= 0:
        return records
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
    kept: list[dict[str, Any]] = []
    changed = False
    for record in records:
        if record.get("status") in ("stopped", "error"):
            stopped_at = record.get("stopped_at")
            if stopped_at:
                try:
                    ts = datetime.fromisoformat(stopped_at)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        changed = True
                        continue
                except ValueError:
                    pass
        kept.append(record)
    if changed:
        save(kept)
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
    records = load()
    legacy = [r for r in records if is_legacy(r)]
    changed = False
    for record in legacy:
        status = record.get("status")
        if status not in ("running", "pending"):
            continue
        pid = record.get("pid")
        if not pid:
            if status == "pending":
                record["status"] = "error"
                record["error_message"] = (
                    "Interrupted: the deployment runtime that started this was removed"
                )
                record["stopped_at"] = _now()
                changed = True
            continue
        if not _pid_is_alive(int(pid)):
            record["status"] = "stopped"
            record.setdefault("stopped_at", _now())
            changed = True
        elif status == "pending":
            record["status"] = "running"
            changed = True
    if changed:
        save(records)
    return [r for r in purge_expired(records) if is_legacy(r)]


def stop_legacy(deployment_id: str) -> dict[str, Any] | None:
    """Signal a legacy deployment's process group and mark it stopped.

    SIGTERM to the process group, because that is how the runner started it —
    ``start_new_session=True``, so the whole tree goes. A PID that is already
    gone is success: the point is that the record and the machine agree.
    """
    records = load()
    for record in records:
        if record.get("id") != deployment_id:
            continue
        pid = record.get("pid")
        if pid:
            try:
                os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        record["status"] = "stopped"
        record["stopped_at"] = _now()
        save(records)
        return record
    return None


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
