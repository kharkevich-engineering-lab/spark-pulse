"""In-memory lock manager for cluster/deployment operations.

Prevents concurrent operations on the same resource, avoiding race conditions
that can corrupt state, create duplicate containers, or leave clusters
in inconsistent states.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

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


class LockManager:
    """In-memory lock manager for cluster/deployment operations.

    Prevents concurrent operations on the same resource.
    Uses file-based locks for cross-process safety (systemd service).
    """

    DEFAULT_TIMEOUT_SECONDS = 30

    def __init__(self, lock_dir: str = "/tmp/spark-pulse/locks"):
        """Initialize lock manager.

        Args:
            lock_dir: Directory for file-based lock persistence.
        """
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

        Returns:
            Number of locks cleaned up.
        """
        now = datetime.now(timezone.utc)
        expired_keys = [
            key for key, info in self._locks.items()
            if now >= info.expires_at
        ]

        with self._lock:
            for key in expired_keys:
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
            return [
                info for info in self._locks.values()
                if now < info.expires_at
            ]

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
            tmp = self._lock_file.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(locks_data, f, indent=2)
            tmp.rename(self._lock_file)
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
