"""In-memory lock manager for cluster/deployment operations.

Prevents concurrent operations on the same resource, avoiding race conditions
that can corrupt state, create duplicate containers, or leave clusters
in inconsistent states.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from spark_pulse.tools.atomic_json import write_json_atomic

logger = logging.getLogger(__name__)


class LockType(str, Enum):
    """Types of locks for cluster/deployment operations."""

    CLUSTER_START = "cluster_start"
    CLUSTER_STOP = "cluster_stop"
    CLUSTER_ROLLBACK = "cluster_rollback"
    DEPLOYMENT_START = "deployment_start"
    DEPLOYMENT_STOP = "deployment_stop"
    MOD_APPLY = "mod_apply"


@dataclass(frozen=True, slots=True)
class LockResult:
    """Result of a lock acquisition attempt."""

    success: bool
    lock: LockInfo | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LockInfo:
    """Information about an active lock."""

    lock_type: LockType
    resource: str
    owner: str
    acquired_at: datetime
    expires_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for API responses."""
        return {
            "lock_type": self.lock_type.value,
            "resource": self.resource,
            "owner": self.owner,
            "acquired_at": self.acquired_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


def default_lock_dir() -> str:
    """Where the lock file lives.

    Under the user's own data directory, with everything else this program
    persists — not ``/tmp/spark-pulse/locks``, which it used to be. A fixed
    path under a world-writable directory is one any local user can create
    first and then own: they choose the permissions, and they can replace
    ``locks.json`` with a symlink to somewhere this process can write.
    """
    return str(Path.home() / ".local" / "share" / "spark-pulse" / "locks")


class LockManager:
    """In-memory lock manager for cluster/deployment operations.

    Prevents concurrent operations on the same resource **within one
    process**. The lock file beside it is a crash-recovery record, not a
    cross-process mutex: it is read once at construction and written on every
    change, so a second process picks up what the first left behind but the
    two do not exclude each other while both are running. The docstring here
    used to claim cross-process safety, which is not what this does — two
    processes each hold their own dictionary and never re-read the file.

    Nothing in the control plane constructs one today; the orchestrator that
    did was removed. It is kept because the deployment paths that would need
    it are still being built, and because a lock manager that quietly does
    something other than what it says is worse than no lock manager at all.
    """

    DEFAULT_TIMEOUT_SECONDS = 30

    def __init__(self, lock_dir: str | None = None):
        """Initialize lock manager.

        Args:
            lock_dir: Directory for file-based lock persistence.
        """
        lock_dir = lock_dir if lock_dir is not None else default_lock_dir()
        self._locks: dict[str, LockInfo] = {}
        self._lock_dir = Path(lock_dir)
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = self._lock_dir / "locks.json"
        self._lock = threading.Lock()
        # Load any existing file-based locks
        self._load_file_locks()

    def acquire(
        self,
        lock_type: LockType,
        resource: str,
        owner: str = "unknown",
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> LockResult:
        """Acquire a lock on a resource.

        Args:
            lock_type: Type of operation being locked.
            resource: Cluster name or deployment id.
            owner: Identifier for the lock owner (e.g., client IP).
            timeout: Seconds before lock expires.

        Returns:
            LockResult with success/failure and reason.
        """
        key = self._make_key(lock_type, resource)

        with self._lock:
            # Check if already locked
            existing = self._locks.get(key)
            if existing is not None:
                # Check if expired
                if datetime.now(timezone.utc) < existing.expires_at:
                    return LockResult(
                        success=False,
                        error=(
                            f"Resource '{resource}' is locked by {existing.owner} "
                            f"(type: {lock_type.value}, expires: {existing.expires_at.isoformat()})"
                        ),
                    )
                else:
                    # Lock expired, remove it
                    logger.info("Removing expired lock for %s", key)
                    del self._locks[key]

            # Acquire new lock
            now = datetime.now(timezone.utc)
            lock_info = LockInfo(
                lock_type=lock_type,
                resource=resource,
                owner=owner,
                acquired_at=now,
                expires_at=now + timedelta(seconds=timeout),
            )
            self._locks[key] = lock_info
            self._save_file_locks()

            logger.info(
                "Acquired %s lock on '%s' (owner: %s, timeout: %ds)",
                lock_type.value,
                resource,
                owner,
                timeout,
            )
            return LockResult(success=True, lock=lock_info)

    def release(self, lock_type: LockType, resource: str) -> bool:
        """Release a lock on a resource.

        Args:
            lock_type: Type of lock to release.
            resource: Cluster name or deployment id.

        Returns:
            True if lock was released, False if not found.
        """
        key = self._make_key(lock_type, resource)

        with self._lock:
            if key in self._locks:
                del self._locks[key]
                self._save_file_locks()
                logger.info("Released %s lock on '%s'", lock_type.value, resource)
                return True
            logger.warning("Attempted to release non-existent lock: %s", key)
            return False

    def is_locked(self, lock_type: LockType, resource: str) -> bool:
        """Check if a resource is currently locked.

        Args:
            lock_type: Type of lock to check.
            resource: Cluster name or deployment id.

        Returns:
            True if locked and not expired.
        """
        key = self._make_key(lock_type, resource)

        with self._lock:
            lock_info = self._locks.get(key)
            if lock_info is None:
                return False
            # Check if expired
            if datetime.now(timezone.utc) >= lock_info.expires_at:
                del self._locks[key]
                self._save_file_locks()
                return False
            return True

    def cleanup_expired(self) -> int:
        """Remove expired locks.

        The scan happens **inside** the mutex, and each candidate is checked
        again at the moment it is deleted. Both matter, and the second is the
        one that bites: the scan used to run outside the lock, so a resource
        whose lock had expired could be legitimately re-acquired by another
        caller before the delete ran — and the delete then removed that
        caller's *live* lock by key, leaving it believing it held exclusion it
        no longer had. Iterating a dict another thread is mutating is the
        other half, and raises outright.

        Returns:
            Number of locks cleaned up.
        """
        with self._lock:
            now = datetime.now(timezone.utc)
            expired_keys = [
                key for key, info in self._locks.items() if now >= info.expires_at
            ]
            for key in expired_keys:
                info = self._locks.get(key)
                # Re-checked, not assumed: between building the list and this
                # line nothing can have changed *now* — but keeping the two
                # together is what makes that true rather than incidental.
                if info is not None and now >= info.expires_at:
                    del self._locks[key]
            if expired_keys:
                self._save_file_locks()

            logger.info("Cleaned up %d expired locks", len(expired_keys))
            return len(expired_keys)

    def get_active_locks(self) -> list[LockInfo]:
        """Get all active (non-expired) locks.

        Returns:
            List of active LockInfo objects.
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            return [info for info in self._locks.values() if now < info.expires_at]

    @staticmethod
    def _make_key(lock_type: LockType, resource: str) -> str:
        """Create a unique key for a lock."""
        return f"{lock_type.value}:{resource}"

    def _save_file_locks(self) -> None:
        """Save current locks to file for cross-process safety."""
        try:
            locks_data = {
                key: {
                    "lock_type": info.lock_type.value,
                    "resource": info.resource,
                    "owner": info.owner,
                    "acquired_at": info.acquired_at.isoformat(),
                    "expires_at": info.expires_at.isoformat(),
                }
                for key, info in self._locks.items()
            }
            # The same durable write every other persisted file here uses:
            # fsync the content, rename, fsync the directory. The hand-rolled
            # version fsynced nothing, so the rename could land before the
            # bytes did and a power loss left an empty file.
            write_json_atomic(self._lock_file, locks_data)
        except OSError as e:
            logger.warning(f"Failed to save locks to file: {e}")

    def _load_file_locks(self) -> None:
        """Load locks from file for cross-process recovery."""
        if not self._lock_file.exists():
            return
        try:
            with open(self._lock_file) as f:
                locks_data = json.load(f)
            now = datetime.now(timezone.utc)
            for key, data in locks_data.items():
                expires_at = datetime.fromisoformat(data["expires_at"])
                # Only load non-expired locks
                if now < expires_at:
                    lock_info = LockInfo(
                        lock_type=LockType(data["lock_type"]),
                        resource=data["resource"],
                        owner=data["owner"],
                        acquired_at=datetime.fromisoformat(data["acquired_at"]),
                        expires_at=expires_at,
                    )
                    self._locks[key] = lock_info
            logger.info("Loaded %d locks from file", len(self._locks))
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning(f"Failed to load locks from file: {e}")
