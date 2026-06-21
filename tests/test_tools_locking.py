"""Tests for the lock manager."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from spark_pulse.tools.locking import (
    LockInfo,
    LockManager,
    LockResult,
    LockType,
)


class TestLockInfo:
    def test_to_dict(self):
        now = datetime.now(timezone.utc)
        lock = LockInfo(
            lock_type=LockType.CLUSTER_START,
            resource="test-cluster",
            owner="127.0.0.1",
            acquired_at=now,
            expires_at=now + timedelta(seconds=30),
        )
        d = lock.to_dict()
        assert d["lock_type"] == "cluster_start"
        assert d["resource"] == "test-cluster"
        assert d["owner"] == "127.0.0.1"


class TestLockManager:
    @pytest.fixture
    def manager(self, tmp_path):
        return LockManager(lock_dir=str(tmp_path / "locks"))

    def test_acquire_release(self, manager):
        result = manager.acquire(LockType.CLUSTER_START, "test-cluster")
        assert result.success is True
        assert result.lock is not None
        assert result.lock.resource == "test-cluster"

        released = manager.release(LockType.CLUSTER_START, "test-cluster")
        assert released is True

    def test_acquire_twice_contention(self, manager):
        result1 = manager.acquire(LockType.CLUSTER_START, "test-cluster")
        assert result1.success is True

        result2 = manager.acquire(LockType.CLUSTER_START, "test-cluster")
        assert result2.success is False
        assert "locked" in result2.error.lower()

    def test_is_locked(self, manager):
        manager.acquire(LockType.CLUSTER_START, "test-cluster")
        assert manager.is_locked(LockType.CLUSTER_START, "test-cluster") is True
        assert manager.is_locked(LockType.CLUSTER_STOP, "test-cluster") is False

    def test_release_nonexistent(self, manager):
        released = manager.release(LockType.CLUSTER_START, "nonexistent")
        assert released is False

    def test_cleanup_expired(self, manager):
        # Acquire with 0-second timeout (already expired)
        result = manager.acquire(LockType.CLUSTER_START, "test-cluster", timeout=0)
        assert result.success is True

        # Should be expired immediately
        cleaned = manager.cleanup_expired()
        assert cleaned >= 1

    def test_get_active_locks(self, manager):
        manager.acquire(LockType.CLUSTER_START, "cluster1")
        manager.acquire(LockType.CLUSTER_STOP, "cluster2")
        locks = manager.get_active_locks()
        assert len(locks) == 2

    def test_different_resources_no_contention(self, manager):
        result1 = manager.acquire(LockType.CLUSTER_START, "cluster1")
        result2 = manager.acquire(LockType.CLUSTER_START, "cluster2")
        assert result1.success is True
        assert result2.success is True

    def test_different_lock_types_no_contention(self, manager):
        result1 = manager.acquire(LockType.CLUSTER_START, "test-cluster")
        result2 = manager.acquire(LockType.CLUSTER_STOP, "test-cluster")
        assert result1.success is True
        assert result2.success is True
