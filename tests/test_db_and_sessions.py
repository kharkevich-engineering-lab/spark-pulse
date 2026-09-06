"""The state store, and the seam that makes the backend replaceable.

``docs/cluster-agent-plan.md`` §3.3 chose SQLite in WAL mode. The reason this
goes through SQLAlchemy rather than the ``sqlite3`` driver is the second half
of that decision — that one appliance becoming several is a change to a URL —
and a claim like that is worth a test rather than a comment.

:func:`test_every_table_emits_postgresql_ddl` is that test. It compiles the
schema for the PostgreSQL dialect without a PostgreSQL server, so a column
type that only SQLite has fails here rather than on the day somebody sets
``SPARK_PULSE_DATABASE_URL``. It cannot prove the queries behave identically —
only a real server does that — and it does prove the schema is portable, which
is the half that is cheap to get wrong and expensive to discover.
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from spark_pulse import db, sessions


# ── The engine ──────────────────────────────────────────────────────────────


def test_the_sqlite_file_is_not_world_readable(tmp_path):
    """The rows hold OIDC access and refresh tokens.

    In memory those were unreachable by other users on the machine; in a file
    at the default 0644 they are readable by every one of them.
    """
    path = tmp_path / "state.db"
    db.configure(f"sqlite:///{path}")
    sessions.create(user={"sub": "alice"}, expires_at=time.time() + 60)

    assert path.exists()
    assert path.stat().st_mode & 0o077 == 0


def test_sqlite_runs_in_wal_mode(tmp_path):
    """§3.3 says WAL, and the reason is readers.

    Without it a writer blocks every reader for the length of its
    transaction; with it readers never block at all — which is what makes one
    file safe for the several worker processes this exists to allow.
    """
    path = tmp_path / "state.db"
    db.configure(f"sqlite:///{path}")
    sessions.create(user={"sub": "alice"}, expires_at=time.time() + 60)

    with sqlite3.connect(path) as raw:
        mode = raw.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_the_engine_is_rebuilt_when_the_url_changes(tmp_path):
    """Two databases, and nothing leaks between them."""
    first, second = tmp_path / "one.db", tmp_path / "two.db"

    db.configure(f"sqlite:///{first}")
    token = sessions.create(user={"sub": "alice"}, expires_at=time.time() + 60)
    assert sessions.get(token) is not None

    db.configure(f"sqlite:///{second}")
    assert sessions.get(token) is None, "a different database answered for the first"

    db.configure(f"sqlite:///{first}")
    assert sessions.get(token) is not None, "the first database lost its row"


def test_a_failed_write_leaves_nothing_behind(tmp_path):
    """``session_scope`` rolls back, so half a change is not a state to reach."""
    db.configure(f"sqlite:///{tmp_path / 'state.db'}")
    before = sessions.count()

    with pytest.raises(RuntimeError):
        with db.session_scope() as active:
            active.add(sessions.Session(token="doomed", expires_at=0.0, user={}))
            raise RuntimeError("something went wrong mid-transaction")

    assert sessions.count() == before
    assert sessions.get("doomed") is None


# ── Portability ─────────────────────────────────────────────────────────────


def test_every_table_emits_postgresql_ddl():
    """The scale case, checked without a PostgreSQL server.

    Compiling the schema for the PostgreSQL dialect catches the thing that
    actually breaks a backend swap: a column type, default or constraint that
    only one backend has. It is not a promise that every query behaves
    identically — that needs a real server — but it is the half that would
    otherwise be discovered by an operator rather than by us.
    """
    dialect = postgresql.dialect()

    for table in db.Base.metadata.sorted_tables:
        statement = str(CreateTable(table).compile(dialect=dialect))
        assert f"CREATE TABLE {table.name}" in statement.replace('"', "")
        # The spellings SQLite tolerates and PostgreSQL does not.
        assert "AUTOINCREMENT" not in statement.upper()
        assert "DATETIME" not in statement.upper()


def test_the_schema_is_created_on_first_use(tmp_path):
    """No migration step to forget, and no empty-database failure mode."""
    path = tmp_path / "fresh.db"
    db.configure(f"sqlite:///{path}")

    assert sessions.count() == 0  # touching it is enough

    with sqlite3.connect(path) as raw:
        tables = {
            row[0]
            for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "sessions" in tables


# ── Sessions ────────────────────────────────────────────────────────────────


def test_a_session_round_trips(tmp_path):
    db.configure(f"sqlite:///{tmp_path / 'state.db'}")
    token = sessions.create(
        access_token="at",
        refresh_token="rt",
        expires_at=time.time() + 3600,
        user={"sub": "alice", "email": "alice@example.com"},
        created_at="2026-09-06T00:00:00+00:00",
    )

    stored = sessions.get(token)

    assert stored is not None
    assert stored["access_token"] == "at"
    assert stored["refresh_token"] == "rt"
    assert stored["user"]["sub"] == "alice"
    assert stored["created_at"] == "2026-09-06T00:00:00+00:00"


def test_a_minted_id_is_not_guessable(tmp_path):
    db.configure(f"sqlite:///{tmp_path / 'state.db'}")

    ids = {sessions.create(expires_at=time.time() + 60) for _ in range(50)}

    assert len(ids) == 50
    assert all(len(i) >= 32 for i in ids)


def test_an_expired_session_is_dropped_as_it_is_read(tmp_path):
    """What keeps the table bounded without a scheduled job.

    The busiest thing in the system reads sessions, so the busiest thing in
    the system is what cleans them.
    """
    db.configure(f"sqlite:///{tmp_path / 'state.db'}")
    token = sessions.create(expires_at=time.time() - 1)

    assert sessions.get(token) is None
    assert sessions.count() == 0


def test_a_session_with_no_expiry_recorded_survives(tmp_path):
    """Rows written before expiry was tracked must not all die at once."""
    db.configure(f"sqlite:///{tmp_path / 'state.db'}")
    token = sessions.create(expires_at=0.0, user={"sub": "alice"})

    assert sessions.get(token) is not None


def test_removing_a_session_is_idempotent(tmp_path):
    db.configure(f"sqlite:///{tmp_path / 'state.db'}")
    token = sessions.create(expires_at=time.time() + 60)

    assert sessions.remove(token) is True
    assert sessions.remove(token) is False
    assert sessions.remove("") is False


def test_sweeping_takes_the_expired_and_leaves_the_rest(tmp_path):
    db.configure(f"sqlite:///{tmp_path / 'state.db'}")
    live = sessions.create(expires_at=time.time() + 3600)
    sessions.create(expires_at=time.time() - 1)
    sessions.create(expires_at=time.time() - 500)
    forever = sessions.create(expires_at=0.0)

    assert sessions.sweep() == 2

    assert sessions.get(live) is not None
    assert sessions.get(forever) is not None, "no expiry recorded is not expired"
    assert sessions.count() == 2


def test_a_session_survives_a_restart(tmp_path):
    """The whole point of leaving the dictionary behind.

    Disposing the engine and building a new one against the same file is what
    a process restart is, from the database's side.
    """
    path = tmp_path / "state.db"
    db.configure(f"sqlite:///{path}")
    token = sessions.create(user={"sub": "alice"}, expires_at=time.time() + 3600)

    db.dispose()
    db.configure(f"sqlite:///{path}")

    assert sessions.get(token) is not None, "the session did not survive a restart"


def test_two_engines_on_one_file_see_each_others_writes(tmp_path):
    """The multi-worker case, which is why a shared store exists at all.

    Two engines against one file stand in for two uvicorn workers: a session
    minted by one has to be readable by the other, which is exactly what the
    in-memory dictionary could not do.
    """
    path = tmp_path / "state.db"
    db.configure(f"sqlite:///{path}")
    token = sessions.create(user={"sub": "alice"}, expires_at=time.time() + 3600)

    db.dispose()  # the second "worker" builds its own engine
    db.configure(f"sqlite:///{path}")

    assert sessions.get(token) is not None


# ── The one-time import ─────────────────────────────────────────────────────


class TestTheLegacyImportRunsOnce:
    """A JSON file is imported once, and "once" cannot mean "whenever empty".

    Keying the import off an empty table is the obvious implementation and is
    wrong in a way that only shows up later: deleting the last deployment
    empties the table, so the next read re-imports the file and resurrects
    every deployment the operator had removed. It is the same shape as the
    zombie record fixed in #40, arriving by a different route.
    """

    def _seed(self, path, records):
        import json

        path.write_text(json.dumps(records))

    def test_records_are_imported_from_the_legacy_file(self, tmp_path, monkeypatch):
        from spark_pulse.tools import deployment_records as store

        db.configure(f"sqlite:///{tmp_path / 'state.db'}")
        legacy = tmp_path / "deployments.json"
        self._seed(legacy, [{"id": "dep-1", "status": "running", "runtime": "native"}])
        monkeypatch.setattr(store, "RECORDS_FILE", legacy)

        assert [r["id"] for r in store.load()] == ["dep-1"]

    def test_deleting_the_last_record_does_not_resurrect_the_file(
        self, tmp_path, monkeypatch
    ):
        from spark_pulse.tools import deployment_records as store

        db.configure(f"sqlite:///{tmp_path / 'state.db'}")
        legacy = tmp_path / "deployments.json"
        self._seed(legacy, [{"id": "dep-1", "status": "running", "runtime": "native"}])
        monkeypatch.setattr(store, "RECORDS_FILE", legacy)
        assert len(store.load()) == 1

        assert store.delete("dep-1") is True

        assert store.load() == [], "the legacy file was imported a second time"
        assert legacy.exists(), "the operator's file is left where it was"

    def test_saving_an_empty_set_stays_empty(self, tmp_path, monkeypatch):
        from spark_pulse.tools import deployment_records as store

        db.configure(f"sqlite:///{tmp_path / 'state.db'}")
        legacy = tmp_path / "deployments.json"
        self._seed(legacy, [{"id": "dep-1", "status": "running", "runtime": "native"}])
        monkeypatch.setattr(store, "RECORDS_FILE", legacy)
        store.load()

        store.save([])

        assert store.load() == []

    def test_a_file_restored_after_a_fresh_start_is_still_imported(
        self, tmp_path, monkeypatch
    ):
        """A fresh install must not record an import it never performed."""
        from spark_pulse.tools import deployment_records as store

        db.configure(f"sqlite:///{tmp_path / 'state.db'}")
        legacy = tmp_path / "deployments.json"
        monkeypatch.setattr(store, "RECORDS_FILE", legacy)
        assert store.load() == []  # nothing to import yet

        self._seed(legacy, [{"id": "dep-1", "status": "running", "runtime": "native"}])

        assert [r["id"] for r in store.load()] == ["dep-1"]
