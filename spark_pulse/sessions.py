"""Browser sessions, in the database rather than in a dictionary.

What this replaces was one line — ``_active_tokens: dict[str, dict] = {}`` —
with a comment saying to replace it with Redis in production. Three things
followed from it being a dictionary in one process's memory, and the first two
are the reason this exists:

* **A restart logged everyone out.** Not dangerous, but for a service that
  restarts on upgrade it meant re-authenticating every time.
* **It ruled out more than one worker.** ``--workers 4`` gives each process its
  own dictionary, so a cookie minted by one is unknown to the other three and
  roughly three requests in four get a 401 at random. The shipped systemd unit
  pins ``--workers 1``, so this was latent — and latent is exactly how it would
  be found, by whoever first tried to scale under load.
* The refresh token was stored and never used, which is a separate gap and
  still is.

Redis was the obvious answer and is the wrong one here: it is a daemon to
install, supervise, secure and back up, and it puts a network service in front
of *logging in* — on a control plane whose job is to work when other things
are broken. SQLite costs no service at all, and §3.3 had already chosen it for
the rest of the state.

**The row holds OIDC access and refresh tokens**, so the database file is
0600 from the moment it exists (see :func:`spark_pulse.db._create_engine`). On
PostgreSQL that guarantee becomes the database's own, which is a thing to
decide deliberately rather than inherit.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from sqlalchemy import JSON, Float, String, Text, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from spark_pulse.db import Base, session_scope

__all__ = ["Session", "create", "get", "remove", "sweep", "count", "clear"]

#: How many bytes of entropy a session id carries. The cookie is a bearer of
#: this and nothing else — the provider's tokens never leave the server — so
#: it has to be unguessable and need not be anything else.
_TOKEN_BYTES = 32


class Session(Base):
    """One logged-in browser.

    Column types are the ones SQLite and PostgreSQL both have, because the
    point of the seam is that the same declaration emits DDL for either.
    """

    __tablename__ = "sessions"

    #: The opaque id in the cookie. The primary key, so a lookup on every
    #: authenticated request is an index hit rather than a scan.
    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    #: The provider's own tokens, which never reach the browser.
    #:
    #: ``Text``, not ``String(n)``. SQLite ignores a declared length and
    #: PostgreSQL enforces it, so any cap here is a cap that only exists on the
    #: backend this whole seam was built to enable. Azure AD and Okta routinely
    #: issue access tokens past 4 KB once group claims are included: with a
    #: length, login succeeds on SQLite and returns 500 from ``/auth/callback``
    #: on PostgreSQL, which is the worst possible place to discover it.
    access_token: Mapped[str] = mapped_column(Text, default="")
    refresh_token: Mapped[str] = mapped_column(Text, default="")
    #: Unix seconds. Indexed because the sweep orders by it.
    expires_at: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    created_at: Mapped[str] = mapped_column(String(64), default="")
    #: The claims the provider published. JSON rather than columns because the
    #: shape is the provider's, not ours.
    user: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    def to_dict(self) -> dict[str, Any]:
        """The shape the rest of ``auth`` already expects.

        Deliberately identical to what the dictionary held, so moving the
        store did not become a rewrite of everything that read it.
        """
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "user": self.user or {},
            "created_at": self.created_at,
        }


def create(
    *,
    access_token: str = "",
    refresh_token: str = "",
    expires_at: float = 0.0,
    user: dict[str, Any] | None = None,
    created_at: str = "",
    token: str | None = None,
) -> str:
    """Store a session and return the id the cookie will carry.

    ``token`` exists for tests that need a known id; nothing in the product
    passes it, because a session id a caller can choose is a session id an
    attacker can choose.
    """
    session_id = token or secrets.token_urlsafe(_TOKEN_BYTES)
    with session_scope() as db:
        # Not ``merge`` — see the house rule in ``db``: it chooses UPDATE from
        # its own load and then matches nothing on PostgreSQL.
        row = db.get(Session, session_id)
        if row is None:
            db.add(
                Session(
                    token=session_id,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_at=float(expires_at or 0.0),
                    created_at=created_at,
                    user=user or {},
                )
            )
        else:
            row.access_token = access_token
            row.refresh_token = refresh_token
            row.expires_at = float(expires_at or 0.0)
            row.created_at = created_at
            row.user = user or {}
    return session_id


def get(token: str, *, now: float | None = None) -> dict[str, Any] | None:
    """The session, or None if there is none or it has expired.

    An expired row is deleted on the way past. That is what keeps the table
    bounded without a scheduled job: the busiest thing in the system reads it,
    so the busiest thing in the system cleans it.
    """
    if not token:
        return None
    moment = time.time() if now is None else now
    with session_scope() as db:
        row = db.get(Session, token)
        if row is None:
            return None
        if row.expires_at and moment >= row.expires_at:
            db.delete(row)
            return None
        return row.to_dict()


def remove(token: str) -> bool:
    """Drop a session. ``False`` when there was nothing to drop."""
    if not token:
        return False
    with session_scope() as db:
        row = db.get(Session, token)
        if row is None:
            return False
        db.delete(row)
        return True


def sweep(*, now: float | None = None) -> int:
    """Delete every expired session, and say how many. Returns 0 on an empty table."""
    moment = time.time() if now is None else now
    with session_scope() as db:
        result = db.execute(
            delete(Session).where(Session.expires_at != 0, Session.expires_at <= moment)
        )
        return int(result.rowcount or 0)


def count() -> int:
    """How many sessions are stored, expired or not. For tests and diagnostics."""
    with session_scope() as db:
        return len(list(db.execute(select(Session.token)).scalars()))


def clear() -> None:
    """Drop every session. Logout-everywhere, and test teardown."""
    with session_scope() as db:
        db.execute(delete(Session))
