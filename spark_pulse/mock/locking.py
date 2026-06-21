"""Mock lock manager for simulation mode."""

from __future__ import annotations


from spark_pulse.tools.locking import (
    LockInfo,
    LockResult,
    LockType,
)


class MockLockManager:
    """Mock lock manager for simulation mode.

    Scenarios:
    - "default": always succeeds
    - "contention": returns 409 conflict
    - "expired": auto-cleanup after first acquire
    """

    def __init__(self, scenario: str = "default"):
        self.scenario = scenario
        self._locked = False
        self._cleanup_called = False

    def acquire(
        self,
        lock_type: LockType,
        resource: str,
        owner: str = "test",
        timeout: int = 30,
    ) -> LockResult:
        """Mock lock acquisition."""
        if self.scenario == "contention":
            return LockResult(
                success=False,
                error=f"Resource '{resource}' is locked by mock (contention scenario)",
            )
        if self.scenario == "expired":
            # Simulate expired lock
            from datetime import datetime, timezone, timedelta

            expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            lock_info = LockInfo(
                lock_type=lock_type,
                resource=resource,
                owner=owner,
                acquired_at=expired_at - timedelta(seconds=60),
                expires_at=expired_at,
            )
            return LockResult(success=True, lock=lock_info)
        # Default: always succeeds
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        lock_info = LockInfo(
            lock_type=lock_type,
            resource=resource,
            owner=owner,
            acquired_at=now,
            expires_at=now + __import__("datetime").timedelta(seconds=timeout),
        )
        self._locked = True
        return LockResult(success=True, lock=lock_info)

    def release(self, lock_type: LockType, resource: str) -> bool:
        """Mock lock release."""
        self._locked = False
        return True

    def is_locked(self, lock_type: LockType, resource: str) -> bool:
        """Mock lock check."""
        return self._locked

    def cleanup_expired(self) -> int:
        """Mock expired lock cleanup."""
        if self.scenario == "expired":
            self._cleanup_called = True
            return 1
        return 0

    def get_active_locks(self) -> list[LockInfo]:
        """Mock get active locks."""
        return []
