"""Crash-safe JSON state files.

Every persisted state file in Spark Pulse goes through here. Two rules, both
learned the expensive way (see ``docs/cluster-agent-plan.md`` sections 2.2 and
3.3):

1. **Writes are atomic and durable.** A write that truncates the target in
   place loses the whole file to a crash or a full disk mid-write. We write a
   sibling temp file, fsync it, then ``os.replace`` and fsync the *directory* —
   the directory fsync is what makes the rename itself survive power loss, not
   merely a process crash.

2. **An unreadable state file is not an empty cluster.** A loader that turns
   every error into ``[]`` is the exact input condition behind Nomad issue
   18267, where servers told clients to stop every allocation they were
   running. A missing file is empty; a file that exists and cannot be read or
   parsed is a hard error, raised as :class:`StateFileError`.

This module holds pure filesystem behaviour with nothing to simulate, so — like
``labels`` — it is real-only and intentionally has no mock twin. The mock state
modules import it directly.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

__all__ = [
    "StateFileError",
    "write_json_atomic",
    "read_state_file",
    "quarantine_corrupt",
]


class StateFileError(RuntimeError):
    """A state file exists but could not be read or parsed.

    Callers must not treat this as "no state". ``path`` names the file and
    ``quarantine_path`` — when set — names where the unparseable content was
    moved so an operator still has the artifact.
    """

    def __init__(
        self,
        path: Path | str,
        reason: str,
        quarantine_path: Path | str | None = None,
    ) -> None:
        self.path = Path(path)
        self.reason = reason
        self.quarantine_path = Path(quarantine_path) if quarantine_path else None
        message = f"State file {self.path} exists but could not be read: {reason}"
        if self.quarantine_path is not None:
            message += (
                f". The unreadable file was moved aside to {self.quarantine_path}; "
                f"restore or remove it, then restart."
            )
        super().__init__(message)


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a rename into it is durable across power loss."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return  # platforms that cannot open a directory cannot fsync one
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def write_json_atomic(
    path: Path | str,
    data: Any,
    *,
    mode: int = 0o644,
    indent: int | None = 2,
    default: Any = None,
) -> None:
    """Write ``data`` as JSON to ``path`` atomically and durably.

    The sequence is: temp file in the same directory, ``json.dump``, ``flush``,
    ``os.fsync`` on the file, ``chmod`` to ``mode``, ``os.replace``, then
    ``os.fsync`` on the directory. A reader either sees the previous complete
    file or the new complete file, never a truncated one, and no temp file is
    left behind on failure.
    """
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(directory)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=indent, default=default)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    _fsync_dir(directory)


def quarantine_corrupt(path: Path | str) -> Path | None:
    """Move an unparseable state file aside to ``<name>.corrupt.<timestamp>``.

    Returns the new path, or ``None`` if the file could not be moved. Keeping
    the artifact lets an operator inspect what happened and makes a restart
    possible once they have intervened.
    """
    path = Path(path)
    target = path.with_name(f"{path.name}.corrupt.{int(time.time())}")
    suffix = 1
    while target.exists():
        target = target.with_name(f"{target.name}.{suffix}")
        suffix += 1
    try:
        os.replace(path, target)
    except OSError:
        return None
    _fsync_dir(path.parent)
    return target


def read_state_file(
    path: Path | str, expect: type | tuple[type, ...] | None = None
) -> Any | None:
    """Read the JSON state at ``path``.

    Returns ``None`` when the file genuinely does not exist — that is the only
    case a caller may read as "no state yet". Raises :class:`StateFileError`
    when the file exists but cannot be read, cannot be parsed, or does not hold
    the ``expect``ed top-level type; unparseable content is moved aside by
    :func:`quarantine_corrupt` first.
    """
    path = Path(path)
    try:
        with open(path) as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise StateFileError(path, f"invalid JSON ({exc})", quarantine_corrupt(path))
    except OSError as exc:
        raise StateFileError(path, f"{type(exc).__name__}: {exc}")

    if expect is not None and not isinstance(data, expect):
        names = (
            expect.__name__
            if isinstance(expect, type)
            else "/".join(t.__name__ for t in expect)
        )
        raise StateFileError(
            path,
            f"expected a JSON {names}, found {type(data).__name__}",
            quarantine_corrupt(path),
        )
    return data
