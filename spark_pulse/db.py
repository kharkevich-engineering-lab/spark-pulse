"""The one database, and the one place that decides which database it is.

``docs/cluster-agent-plan.md`` §3.3 chose this: *"SQLite in WAL mode replaces
the JSON file as the source of truth for desired state, with a single writer
thread and all access off the event loop. k3s backs a Kubernetes API server
with SQLite, so it is ample here."* This module is that, plus the seam that
makes the second half of the sentence — swapping SQLite for PostgreSQL when
one appliance becomes several — a change to a URL rather than a rewrite.

**Why SQLAlchemy rather than the ``sqlite3`` driver.** Not for the ORM. For the
dialect boundary: the schema is declared once and emitted as SQLite DDL or
PostgreSQL DDL by the same code, and every query is written against the same
API. A direct driver would mean hand-written SQL that is portable only by
inspection, and the day the URL changed would be the day it was discovered not
to be. ``SPARK_PULSE_DATABASE_URL=postgresql+psycopg://...`` is the whole
migration path, and ``pip install spark-pulse[postgres]`` the whole
dependency.

**Why synchronous.** Everything in ``tools/`` is synchronous and runs on the
AnyIO worker threads FastAPI hands to sync endpoints; the async half of the
program is the agent transport and the auth routes. A synchronous engine
therefore matches the code that has the most state to store, and the async
callers reach it through ``run_in_threadpool`` — which is §3.3's "all access
off the event loop" stated as a rule rather than hoped for.

**Portability is a constraint on the schema, not a promise about it.** Column
types here are the ones both backends have: ``String``, ``Integer``,
``Float``, ``Boolean``, ``Text``, and JSON via SQLAlchemy's dialect-neutral
``JSON``. Nothing here may use ``AUTOINCREMENT``, ``ON CONFLICT`` spelled for
one backend, or a SQLite-only pragma outside :func:`_configure_sqlite`.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

__all__ = [
    "Base",
    "configure",
    "database_url",
    "default_database_url",
    "dispose",
    "engine",
    "session_scope",
]


class Base(DeclarativeBase):
    """The declarative base every table in this program inherits from."""


def default_database_url() -> str:
    """Where the database lives when nothing says otherwise.

    Beside the JSON files it replaces, so an operator looking for state finds
    it where they already look. Simulation gets its own file inside the
    package's gitignored ``data/`` directory, for the same reason the
    deployment records do: an e2e run must not be able to write over the
    deployments of the machine it runs on.
    """
    if os.environ.get("SIMULATION_MODE", "0") == "1":
        path = Path(__file__).resolve().parent / "data" / "spark-pulse.db"
    else:
        path = Path.home() / ".config" / "spark-pulse" / "spark-pulse.db"
    return f"sqlite:///{path}"


def database_url() -> str:
    """The configured URL, or the default."""
    from spark_pulse.config import config

    return (
        os.environ.get("SPARK_PULSE_DATABASE_URL")
        or config.database_url
        or (default_database_url())
    )


_lock = threading.Lock()
_engine: Engine | None = None
_sessionmaker: sessionmaker[Session] | None = None
_configured_url: str | None = None


def _is_sqlite(url: str) -> bool:
    return make_url(url).get_backend_name() == "sqlite"


def _is_memory(url: str) -> bool:
    database = make_url(url).database
    return _is_sqlite(url) and (not database or database == ":memory:")


def _configure_sqlite(engine: Engine) -> None:
    """The pragmas that make SQLite safe for more than one reader.

    WAL is the one that matters: without it a writer blocks every reader for
    the length of its transaction, and with it readers never block at all.
    ``busy_timeout`` covers the one case WAL does not — two writers — by
    waiting rather than failing, which at this scale means waiting for a
    login to finish.
    """

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


def _create_engine(url: str) -> Engine:
    if _is_memory(url):
        # One shared connection, or every session gets its own empty database.
        return create_engine(
            url, poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
    if _is_sqlite(url):
        path = Path(make_url(url).database or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        created = create_engine(url, connect_args={"check_same_thread": False})
        _configure_sqlite(created)
        # 0600 the moment it exists: this file holds OIDC access and refresh
        # tokens, and on a shared machine the default 0644 hands them to
        # every local user.
        with created.connect():
            pass
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover — a filesystem without modes
            pass
        return created
    return create_engine(url, pool_pre_ping=True)


def engine() -> Engine:
    """The process-wide engine, built on first use and reused after."""
    global _engine, _sessionmaker, _configured_url
    url = database_url()
    with _lock:
        if _engine is None or _configured_url != url:
            if _engine is not None:
                _engine.dispose()
            _engine = _create_engine(url)
            _sessionmaker = sessionmaker(bind=_engine, expire_on_commit=False)
            _configured_url = url
            Base.metadata.create_all(_engine)
        return _engine


def configure(url: str | None) -> None:
    """Point the process at ``url``, disposing whatever it held before.

    Tests use this to get a database per test; nothing in the product calls
    it, because the product reads the URL from configuration.
    """
    global _engine, _sessionmaker, _configured_url
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _sessionmaker = None
        _configured_url = None
    if url is not None:
        os.environ["SPARK_PULSE_DATABASE_URL"] = url
    else:
        os.environ.pop("SPARK_PULSE_DATABASE_URL", None)


def dispose() -> None:
    """Close every pooled connection. Shutdown, and tests."""
    configure(os.environ.get("SPARK_PULSE_DATABASE_URL"))


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transaction: commits on success, rolls back on anything else.

    Every write goes through here rather than through a bare session, so
    "half the change landed" is not a state this program can reach — which is
    the whole reason the JSON files were written atomically, expressed once
    instead of at each call site.
    """
    engine()
    assert _sessionmaker is not None  # engine() builds it
    session = _sessionmaker()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
