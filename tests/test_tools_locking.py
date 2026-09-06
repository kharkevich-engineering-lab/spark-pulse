"""Tests for the lock manager."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from spark_pulse.tools.locking import (
    LockInfo,
    LockManager,
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


class TestCleanupDoesNotStealALiveLock:
    """``cleanup_expired`` scanned outside the mutex and deleted by key.

    A resource whose lock had expired could be legitimately re-acquired
    between the scan and the delete — and the delete then removed the new
    holder's *live* lock, leaving it believing it held exclusion it did not.
    Iterating a dict another thread is mutating is the other half, and raises
    outright.
    """

    def _interleaving_lock(self, manager, action):
        """A mutex that runs ``action`` once, at the moment it is first taken.

        Deterministic rather than timing-based: the window is a few
        microseconds wide, so racing threads reproduce it only by luck.
        """
        real = manager._lock

        class Interleave:
            done = False

            def __enter__(self):
                real.acquire()
                if not Interleave.done:
                    Interleave.done = True
                    real.release()
                    action()
                    real.acquire()
                return real

            def __exit__(self, *_exc):
                real.release()

        return Interleave(), real

    def test_a_lock_reacquired_mid_cleanup_is_not_deleted(self, tmp_path):
        from spark_pulse.tools.locking import LockManager, LockType

        manager = LockManager(lock_dir=str(tmp_path))
        manager.acquire(LockType.DEPLOYMENT_START, "dep-1", owner="first", timeout=0)

        def reacquire():
            granted = manager.acquire(
                LockType.DEPLOYMENT_START, "dep-1", owner="second", timeout=600
            )
            assert granted.success, "the expired lock should be re-acquirable"

        shim, real = self._interleaving_lock(manager, reacquire)
        manager._lock = shim
        manager.cleanup_expired()
        manager._lock = real

        assert manager.is_locked(
            LockType.DEPLOYMENT_START, "dep-1"
        ), "cleanup deleted a live lock it had only seen as expired"

    def test_an_actually_expired_lock_is_still_cleaned(self, tmp_path):
        """And the fix must not stop cleanup doing its job."""
        from spark_pulse.tools.locking import LockManager, LockType

        manager = LockManager(lock_dir=str(tmp_path))
        manager.acquire(LockType.DEPLOYMENT_START, "gone", owner="first", timeout=0)

        assert manager.cleanup_expired() == 1
        assert not manager.is_locked(LockType.DEPLOYMENT_START, "gone")

    def test_the_lock_file_is_not_under_a_world_writable_directory(self):
        """A fixed path under /tmp is one any local user can create first."""
        from spark_pulse.tools.locking import default_lock_dir

        assert not default_lock_dir().startswith("/tmp/")
