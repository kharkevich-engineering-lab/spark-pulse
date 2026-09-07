"""State-file durability and failure semantics.

Two properties are under test, both from ``docs/cluster-agent-plan.md`` 2.2
and 3.3:

* a write is atomic and durable — a crash never leaves a truncated target and
  never leaves a temp file behind;
* an unreadable state file is not an empty cluster — only a genuinely missing
  file reads as ``[]``, and a corrupt one is quarantined and raised.

``tools.deployment_records`` owns the file in both modes — it is real-only,
picking a different path under ``SIMULATION_MODE`` and running the same code —
so there is one implementation to hold to these properties, not two.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import stat
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spark_pulse import tools
from spark_pulse.tools.atomic_json import (
    StateFileError,
    quarantine_corrupt,
    read_state_file,
    write_json_atomic,
)

records = tools.deployment_records

SAMPLE = [{"id": "dep-1", "status": "running", "port": 8000}]


def _temp_leftovers(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if p.name.endswith(".tmp")]


@contextlib.contextmanager
def crashing_commit():
    """Fail every database commit for the duration of the block.

    The successor to :func:`crashing_replace`: what used to be a torn rename
    is now a transaction that does not commit, and the property under test —
    the previous state survives — is the same one.
    """
    from sqlalchemy.orm import Session as SaSession

    original = SaSession.commit

    def boom(self):
        raise OSError("simulated crash before the transaction committed")

    SaSession.commit = boom
    try:
        yield
    finally:
        SaSession.commit = original


@contextlib.contextmanager
def crashing_replace():
    """Fail every ``os.replace`` for the duration of the block.

    Scoped by hand rather than through ``monkeypatch.undo()``, which would also
    revert the fixture that keeps these tests off the developer's real
    ``~/.config/spark-pulse``.
    """
    original = os.replace

    def boom(src, dst):
        raise OSError("simulated crash between temp-write and replace")

    os.replace = boom
    try:
        yield
    finally:
        os.replace = original


# ── The helper: atomic, durable writes ───────────────────────────────────────


class TestWriteJsonAtomic:
    def test_write_leaves_no_temp_file_behind(self, tmp_path):
        target = tmp_path / "state.json"
        write_json_atomic(target, SAMPLE)
        write_json_atomic(target, SAMPLE + [{"id": "dep-2"}])

        assert json.loads(target.read_text()) == SAMPLE + [{"id": "dep-2"}]
        assert _temp_leftovers(tmp_path) == []
        assert list(tmp_path.iterdir()) == [target]

    def test_durability_sequence_is_fsync_replace_fsync_dir(
        self, tmp_path, monkeypatch
    ):
        """The file is fsynced before the replace, the directory after it."""
        calls: list[str] = []

        real_fsync, real_replace = os.fsync, os.replace

        def spy_fsync(fd):
            # A directory fd has no size we can write to; tell the two apart by
            # asking the kernel what kind of object the descriptor points at.
            kind = "fsync_dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync_file"
            calls.append(kind)
            return real_fsync(fd)

        def spy_replace(src, dst):
            calls.append("replace")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "fsync", spy_fsync)
        monkeypatch.setattr(os, "replace", spy_replace)

        write_json_atomic(tmp_path / "state.json", SAMPLE)

        assert calls == ["fsync_file", "replace", "fsync_dir"]

    def test_crash_before_replace_leaves_the_original_intact(self, tmp_path):
        target = tmp_path / "state.json"
        write_json_atomic(target, SAMPLE)
        original = target.read_bytes()

        with crashing_replace():
            with pytest.raises(OSError):
                write_json_atomic(target, [{"id": "dep-2", "status": "running"}])

        assert target.read_bytes() == original
        assert json.loads(target.read_text()) == SAMPLE
        assert _temp_leftovers(tmp_path) == []

    def test_a_truncated_temp_file_does_not_affect_the_target(self, tmp_path):
        """A crash mid-``json.dump`` leaves a partial temp file, never a partial target."""
        target = tmp_path / "state.json"
        write_json_atomic(target, SAMPLE)
        original = target.read_bytes()

        # Exactly what a crash halfway through the dump would leave behind.
        stray = tmp_path / ".state.json.abc123.tmp"
        stray.write_text('[{"id": "dep-1", "sta')

        assert target.read_bytes() == original
        assert read_state_file(target, expect=list) == SAMPLE

        # And the next successful write replaces the target regardless.
        write_json_atomic(target, [{"id": "dep-2"}])
        assert read_state_file(target, expect=list) == [{"id": "dep-2"}]

    def test_mode_is_applied_to_the_replaced_file(self, tmp_path):
        target = tmp_path / "state.json"
        write_json_atomic(target, {"a": 1}, mode=0o600)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

        write_json_atomic(target, {"a": 2}, mode=0o600)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_creates_missing_parent_directories(self, tmp_path):
        target = tmp_path / "nested" / "deep" / "state.json"
        write_json_atomic(target, SAMPLE)
        assert json.loads(target.read_text()) == SAMPLE


# ── The helper: missing versus unreadable ────────────────────────────────────


class TestReadStateFile:
    def test_missing_file_reads_as_no_state(self, tmp_path):
        assert read_state_file(tmp_path / "absent.json") is None

    def test_corrupt_file_raises_and_is_moved_aside(self, tmp_path):
        target = tmp_path / "state.json"
        target.write_text('[{"id": "dep-1", "sta')

        with pytest.raises(StateFileError) as exc:
            read_state_file(target)

        assert exc.value.path == target
        assert not target.exists()
        moved = list(tmp_path.glob("state.json.corrupt.*"))
        assert len(moved) == 1
        assert moved[0].read_text() == '[{"id": "dep-1", "sta'
        assert exc.value.quarantine_path == moved[0]
        assert str(moved[0]) in str(exc.value)

    def test_empty_file_is_corrupt_not_empty_state(self, tmp_path):
        target = tmp_path / "state.json"
        target.write_text("")

        with pytest.raises(StateFileError):
            read_state_file(target)

    def test_wrong_top_level_type_raises_and_is_moved_aside(self, tmp_path):
        target = tmp_path / "state.json"
        target.write_text('{"id": "dep-1"}')

        with pytest.raises(StateFileError) as exc:
            read_state_file(target, expect=list)

        assert "expected a JSON list" in str(exc.value)
        assert len(list(tmp_path.glob("state.json.corrupt.*"))) == 1

    def test_unreadable_file_raises_without_quarantine(self, tmp_path):
        """An IO error is not corruption, so the file stays where it is."""
        target = tmp_path / "state.json"
        target.write_text(json.dumps(SAMPLE))
        target.chmod(0o000)
        if os.access(target, os.R_OK):  # running as root
            pytest.skip("cannot make a file unreadable as this user")
        try:
            with pytest.raises(StateFileError) as exc:
                read_state_file(target)
        finally:
            target.chmod(0o600)

        assert exc.value.quarantine_path is None
        assert target.exists()

    def test_quarantine_never_overwrites_a_previous_artifact(self, tmp_path):
        target = tmp_path / "state.json"
        for body in ("first", "second"):
            target.write_text(body)
            assert quarantine_corrupt(target) is not None
        assert len(list(tmp_path.glob("state.json.corrupt.*"))) == 2


# ── The deployment state file ────────────────────────────────────────────────


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """Point the record store at tmp_path."""
    path = tmp_path / "deployments.json"
    monkeypatch.setattr(records, "RECORDS_FILE", path)
    return path


class TestDeploymentStateFile:
    def test_missing_file_loads_as_empty_list(self, state_file):
        assert not state_file.exists()
        assert records.load() == []

    def test_round_trip_leaves_no_temp_file(self, state_file):
        records.save(SAMPLE)
        assert records.load() == SAMPLE
        assert _temp_leftovers(state_file.parent) == []

    def test_corrupt_file_raises_instead_of_reading_as_empty(self, state_file):
        state_file.write_text("[{not json")

        with pytest.raises(StateFileError) as exc:
            records.load()

        assert exc.value.path == state_file
        assert not state_file.exists()
        assert len(list(state_file.parent.glob("deployments.json.corrupt.*"))) == 1
        assert str(exc.value.quarantine_path) in str(exc.value)

    def test_a_crash_mid_write_keeps_the_previous_deployments(self, state_file):
        """The property survives the move from a JSON file to the database.

        It used to be the atomic rename that gave it: a torn write left the
        old file in place. Now it is the transaction — ``session_scope``
        rolls back — and the guarantee an operator depends on is unchanged:
        a write that fails leaves the previous set of deployments intact,
        never half of it.
        """
        records.save(SAMPLE)

        with crashing_commit():
            with pytest.raises(OSError):
                records.save([])

        assert records.load() == SAMPLE
        assert _temp_leftovers(state_file.parent) == []

    def test_check_state_file_is_silent_when_readable(self, state_file):
        records.check_state_file()  # missing file
        records.save(SAMPLE)
        records.check_state_file()  # present and valid

    def test_check_state_file_raises_on_corruption(self, state_file):
        state_file.write_text("}{")
        with pytest.raises(StateFileError):
            records.check_state_file()

    def test_native_runtime_records_share_the_durable_path(self, state_file):
        """One store, reached the same way from both modules."""
        nr = importlib.import_module("spark_pulse.tools.native_runtime")
        nr._save_records(SAMPLE)

        assert nr._load_records() == SAMPLE
        assert records.load() == SAMPLE

        # And an unreadable *legacy* file is still refused rather than read as
        # an empty cluster, because that file is the migration source.
        state_file.write_text("[[[")
        with pytest.raises(StateFileError):
            nr._load_records()


# ── Per-row writes ───────────────────────────────────────────────────────────


@contextlib.contextmanager
def sql_recorded():
    """Every statement the shared engine executes inside the block."""
    from sqlalchemy import event

    from spark_pulse import db

    executed: list[tuple[str, object]] = []

    def observe(_conn, _cursor, statement, parameters, _context, _many):
        executed.append((statement, parameters))

    active = db.engine()
    event.listen(active, "before_cursor_execute", observe)
    try:
        yield executed
    finally:
        event.remove(active, "before_cursor_execute", observe)


def _writes(executed: list[tuple[str, object]], verb: str) -> list[tuple[str, object]]:
    return [(s, p) for s, p in executed if s.lstrip().upper().startswith(verb)]


THREE = [
    {"id": "dep-1", "status": "running", "port": 8000},
    {"id": "dep-2", "status": "running", "port": 8001},
    {"id": "dep-3", "status": "stopped", "port": 8002},
]


class TestPerRowWrites:
    """One deployment can change without the rest being rewritten.

    The whole-set :func:`save` is still what ``native_runtime`` calls and still
    means "the set is now exactly this", deletions included. What it may not go
    on doing is *writing* the whole set: on PostgreSQL that is a dead tuple and
    a WAL record for every deployment on the machine each time one of them
    changes status, and the cost grows with the size of the cluster rather than
    with the size of the change.
    """

    def test_the_whole_set_save_still_removes_what_is_absent(self, state_file):
        records.save(THREE)

        records.save(THREE[:1])

        assert [r["id"] for r in records.load()] == ["dep-1"]

    def test_saving_the_whole_set_writes_only_the_row_that_changed(self, state_file):
        records.save(THREE)
        changed = [dict(r) for r in THREE]
        changed[1]["status"] = "stopped"

        with sql_recorded() as executed:
            records.save(changed)

        updates = _writes(executed, "UPDATE")
        assert len(updates) == 1, executed
        assert "dep-2" in repr(updates[0][1])
        assert "dep-1" not in repr(updates[0][1])

    def test_saving_a_set_nothing_changed_in_writes_nothing_at_all(self, state_file):
        records.save(THREE)

        with sql_recorded() as executed:
            records.save(THREE)

        assert _writes(executed, "UPDATE") == []
        assert _writes(executed, "INSERT") == []
        assert _writes(executed, "DELETE") == []

    def test_upsert_stores_one_record_and_leaves_the_others_alone(self, state_file):
        """The difference from ``save``, which would have deleted the others."""
        records.save(THREE)

        records.upsert({"id": "dep-2", "status": "stopped", "port": 8001})

        assert {r["id"]: r["status"] for r in records.load()} == {
            "dep-1": "running",
            "dep-2": "stopped",
            "dep-3": "stopped",
        }

    def test_upsert_inserts_a_record_that_was_not_stored_yet(self, state_file):
        records.save(THREE)

        records.upsert({"id": "dep-4", "status": "pending"})

        assert records.get("dep-4") == {"id": "dep-4", "status": "pending"}
        assert len(records.load()) == 4

    def test_a_record_with_no_id_is_refused_rather_than_silently_dropped(
        self, state_file
    ):
        with pytest.raises(ValueError):
            records.upsert({"status": "running"})

    def test_update_merges_onto_what_is_stored_not_onto_the_callers_copy(
        self, state_file
    ):
        """The lost update the mutex exists to prevent, in per-row form."""
        records.save(THREE)
        stale = records.get("dep-1")  # a caller's copy, taken early
        records.update("dep-1", port=9999)  # somebody else changes a field

        stale["status"] = "stopped"
        records.update("dep-1", status=stale["status"])

        stored = records.get("dep-1")
        assert stored["status"] == "stopped"
        assert stored["port"] == 9999, "the stale copy's port was written back"

    def test_update_answers_none_for_a_record_that_is_gone(self, state_file):
        records.save(THREE)
        assert records.update("dep-9", status="stopped") is None

    def test_delete_many_removes_exactly_the_named_rows(self, state_file):
        records.save(THREE)

        assert records.delete_many(["dep-1", "dep-3", "dep-9"]) == 2

        assert [r["id"] for r in records.load()] == ["dep-2"]

    def test_a_per_row_write_waits_for_a_whole_set_write_to_finish(self, state_file):
        """Both kinds of write take the same mutex, or neither of them is safe."""
        records.save(THREE)
        done = threading.Event()

        def stop_it():
            records.update("dep-1", status="stopped")
            done.set()

        worker = threading.Thread(target=stop_it)
        with records.transaction():
            worker.start()
            assert not done.wait(0.2), "the per-row write did not take the mutex"

        assert done.wait(5)
        worker.join()
        assert records.get("dep-1")["status"] == "stopped"

    def test_a_redaction_on_load_does_not_delete_a_record_created_since(
        self, state_file, monkeypatch
    ):
        """``load`` repairs the records it read, and touches nothing else.

        The redaction used to write the whole set back, which on a *read* path
        means deleting whatever another thread created while the read was in
        flight — and ``load`` holds no mutex, so that window is real.
        ``_redact`` runs inside it, which makes it the seam that can stand in
        for the other thread.
        """
        records.save([{"id": "dep-1", "launch_command": "run -e HF_TOKEN=hunter2"}])
        redact = records._redact

        def redact_and_race(command):
            records.upsert({"id": "dep-2", "status": "running"})
            return redact(command)

        monkeypatch.setattr(records, "_redact", redact_and_race)

        loaded = records.load()

        assert loaded[0]["launch_command"] == "run -e HF_TOKEN=[REDACTED]"
        assert records.get("dep-1")["launch_command"].endswith("[REDACTED]")
        assert records.get("dep-2") is not None, "the concurrent record was deleted"

    def test_reconciling_legacy_records_writes_back_only_those_it_decided(
        self, state_file, monkeypatch
    ):
        """A PID sweep is about legacy records, and must not touch the rest.

        ``_pid_is_alive`` is the seam for the same reason ``_redact`` is: it
        runs between the load and the write-back, so a record that appears
        there is one the sweep never read.
        """
        records.save([{"id": "old", "status": "running", "pid": 4242}])

        def probe_and_race(_pid):
            records.upsert({"id": "new", "status": "running", "runtime": "native"})
            return False

        monkeypatch.setattr(records, "_pid_is_alive", probe_and_race)

        assert [r["id"] for r in records.list_legacy()] == ["old"]

        assert records.get("old")["status"] == "stopped"
        assert records.get("new") is not None, "a native deployment was purged"


# ── Startup refuses on an unreadable state file ──────────────────────────────


class TestStartupRefusal:
    def test_startup_refuses_and_names_the_path(self, state_file, capfd):
        from spark_pulse.app import create_app

        state_file.write_text('[{"id": "dep-1"')

        with pytest.raises(StateFileError):
            with TestClient(create_app()):
                pass

        out = capfd.readouterr().out
        assert str(state_file) in out
        assert "FATAL" in out
        assert "refusing to start" in out
        # The operator is told where the artifact went.
        moved = list(state_file.parent.glob("deployments.json.corrupt.*"))
        assert len(moved) == 1
        assert str(moved[0]) in out

    def test_startup_proceeds_on_a_readable_state_file(self, state_file):
        from spark_pulse.app import create_app

        records.save([])
        with TestClient(create_app()) as client:
            assert client.get("/health").status_code == 200


# ── Secrets and user settings ────────────────────────────────────────────────


class TestConfigFiles:
    def test_secrets_keep_mode_0600_through_a_rewrite(self, tmp_path, monkeypatch):
        import spark_pulse.config as cfg

        secrets = tmp_path / "secrets.json"
        monkeypatch.setattr(cfg, "_SECRETS_PATH", secrets)

        cfg._save_secrets({"hf_token": "one"})
        assert stat.S_IMODE(secrets.stat().st_mode) == 0o600

        cfg._save_secrets({"hf_token": "two"})
        assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
        assert cfg._load_secrets() == {"hf_token": "two"}
        assert _temp_leftovers(tmp_path) == []

    def test_secrets_survive_a_crash_before_replace(self, tmp_path, monkeypatch):
        import spark_pulse.config as cfg

        secrets = tmp_path / "secrets.json"
        monkeypatch.setattr(cfg, "_SECRETS_PATH", secrets)
        cfg._save_secrets({"hf_token": "one"})

        with crashing_replace():
            with pytest.raises(OSError):
                cfg._save_secrets({"hf_token": "two"})

        assert cfg._load_secrets() == {"hf_token": "one"}
        assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
        assert _temp_leftovers(tmp_path) == []

    def test_user_settings_round_trip_leaves_no_temp_file(self, tmp_path, monkeypatch):
        import spark_pulse.config as cfg

        settings = tmp_path / "settings.json"
        monkeypatch.setattr(cfg, "_SETTINGS_PATH", settings)

        cfg._save_user_settings({"webui_port": 8100})
        cfg._save_user_settings({"webui_port": 8200})

        assert cfg._load_user_settings() == {"webui_port": 8200}
        assert _temp_leftovers(tmp_path) == []
