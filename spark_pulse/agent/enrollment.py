"""Enrollment tokens and the ledger the servicer checks on every connection.

Two things live here, and they are separate on purpose.

**Tokens** are the bootstrap secret. One token is scoped to one node, lives ten
minutes, and can be redeemed once — Nomad scopes tokens to a node name this
way and k0sctl uses exactly a ten minute TTL (§3.1). The token itself is never
stored: the ledger holds a SHA-256 of it, so a stolen state file yields no
usable token. Redemption is what mints the node's uuid, which is why the uuid
can be server-minted without the node ever proposing one.

**The ledger** is the passive half of revocation. Most TLS stacks outside
browsers check neither CRL nor OCSP, and ours is no exception, so the servicer
consults this ledger on every connection alongside chain validation (§3.2).
Revoking a node is adding a row here, which takes effect on its next
connection and on every command in flight, with no CRL and no OCSP.

The ledger is also where a reimage is *detected rather than inferred*. Each
node's public key fingerprint, machine-id, boot_id and hardware fingerprint are
recorded at enrollment and compared on every heartbeat. A new key for an
already-accepted uuid, or a hardware fingerprint that no longer matches, marks
the node ``denied`` and surfaces it for a human decision — which is what
``salt-key`` does, and is the opposite of quietly trusting whatever answered.

Storage is the state database (:mod:`spark_pulse.db`), which the ledger shares
with deployments and sessions: one transaction per mutation, and a one-time
import of the ``enrollment.json`` this used to be — keyed on the ``meta`` table,
never on "are the tables empty", because removing the last node empties them
and an import that re-ran then would readmit it. An unreadable ledger still
refuses to start rather than reporting a cheerful empty cluster (§3.3).

Writes are per-row: admitting one node writes one row, and the whole-ledger
write survives only as the housekeeping sweep, which is the pass whose job is
to reconcile the tables with this ledger's working copy.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from spark_pulse.agent.errors import EnrollmentRejected
from spark_pulse.agent.identity import mint_node_id
from sqlalchemy import JSON, String, select
from sqlalchemy.orm import Mapped, mapped_column

from sqlalchemy.exc import IntegrityError

from spark_pulse.db import Base, is_done, mark_done_within, session_scope
from spark_pulse.tools.atomic_json import read_state_file

logger = logging.getLogger(__name__)

__all__ = [
    "TOKEN_TTL_SECONDS",
    "NodeState",
    "IdentityVerdict",
    "LedgerEntry",
    "TokenGrant",
    "EnrollmentLedger",
]

#: Ten minutes, matching k0sctl. Long enough for an SSH install over a slow
#: uplink, short enough that a token left in a log is worthless by morning.
TOKEN_TTL_SECONDS = 600


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class NodeState(str, Enum):
    """Three states, because two is never enough (§3.3).

    ``pending`` is a node whose token was minted but not yet redeemed;
    ``accepted`` is enrolled and allowed to connect; ``denied`` is a node whose
    identity stopped adding up and needs a human. Removal is a deletion, and
    is a different operator action from denial.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class IdentityVerdict:
    """What the ledger makes of the identity a connecting node presents."""

    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class LedgerEntry:
    """One node's membership record."""

    node_id: str
    name: str = ""
    state: str = NodeState.PENDING.value
    enrolled_at: float = 0.0
    last_seen: float = 0.0
    #: sha256 of the SPKI in the certificate we issued. A different key for
    #: this uuid is a reimage or a theft, never a routine event.
    public_key_fingerprint: str = ""
    #: Diagnostic only — Sparks ship duplicate machine-ids, so this may never
    #: be identity. It exists so duplicates can be *reported*.
    machine_id: str = ""
    #: Changes on every reboot, so it is compared for change, not for match.
    boot_id: str = ""
    hardware_fingerprint: str = ""
    cert_not_after: float = 0.0
    denied_reason: str = ""
    #: Certificates issued so far. A renewal bumps it; a jump means something
    #: is re-enrolling in a loop.
    issued: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEntry:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class TokenGrant:
    """A minted, not yet redeemed enrollment token."""

    token_hash: str
    name: str
    created_at: float
    expires_at: float
    used_at: float = 0.0
    node_id: str = ""
    #: The identity this token will mint, chosen by the *control plane* at
    #: mint time rather than at redemption. Empty means "mint a fresh one".
    #: The rule it protects is unchanged — the node still never proposes an
    #: identity — and what it buys is one id per machine instead of two: the
    #: node registry has already minted one by the time an install starts, and
    #: a second uuid for the same machine is a join waiting to go wrong.
    assign_node_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenGrant:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


# ── The tables ───────────────────────────────────────────────────────────────


class _LedgerNodeRow(Base):
    """One node's membership record."""

    __tablename__ = "enrollment_nodes"

    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), default="", index=True)
    entry: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class _LedgerTokenRow(Base):
    """One minted enrollment token, by hash. The secret is never stored."""

    __tablename__ = "enrollment_tokens"

    token_hash: Mapped[str] = mapped_column(String(128), primary_key=True)
    grant: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


def _node_row(entry: LedgerEntry) -> _LedgerNodeRow:
    return _LedgerNodeRow(
        node_id=entry.node_id, state=entry.state, entry=entry.to_dict()
    )


def _token_row(grant: TokenGrant) -> _LedgerTokenRow:
    return _LedgerTokenRow(token_hash=grant.token_hash, grant=grant.to_dict())


def _entry_of(row: _LedgerNodeRow) -> LedgerEntry:
    """The stored membership record, addressed by the row's own id.

    The id comes from the row rather than from the payload because every write
    addresses a row by ``entry.node_id``: a row whose two disagreed — an
    imported file, a hand-edited one — would be a row no later write could
    ever reach again.
    """
    entry = LedgerEntry.from_dict(dict(row.entry))
    entry.node_id = row.node_id
    return entry


def _grant_of(row: _LedgerTokenRow) -> TokenGrant:
    grant = TokenGrant.from_dict(dict(row.grant))
    grant.token_hash = row.token_hash
    return grant


@dataclass
class _State:
    nodes: dict[str, LedgerEntry] = field(default_factory=dict)
    tokens: dict[str, TokenGrant] = field(default_factory=dict)


def _put_node(db, entry: LedgerEntry) -> None:
    """Write one ledger row inside the caller's transaction, without ``merge``.

    See the house rule in :mod:`spark_pulse.db`: merge picks UPDATE from its
    own load and then matches nothing on PostgreSQL.
    """
    row = db.get(_LedgerNodeRow, entry.node_id)
    if row is None:
        db.add(_node_row(entry))
    else:
        row.state = entry.state
        row.entry = entry.to_dict()


def _put_token(db, grant: TokenGrant) -> None:
    row = db.get(_LedgerTokenRow, grant.token_hash)
    if row is None:
        db.add(_token_row(grant))
    else:
        row.grant = grant.to_dict()


class EnrollmentLedger:
    """Who may connect, and with which key.

    Thread-safe: the control plane's event loop and its request threads both
    reach it. ``_state`` is the working copy, and it is what keeps
    :meth:`authorize` — called on every connection — off the database entirely;
    the lock is what makes each *load, change, store* sequence indivisible, so
    two threads accepting and denying the same node cannot interleave into one
    of the two decisions being lost.

    Mutations write the rows they changed and nothing else. A whole-ledger
    write is kept for :meth:`sweep`, which is housekeeping and is the one pass
    that is supposed to reconcile the tables with the working copy.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state = self._load()

    # ── Persistence ──────────────────────────────────────────────────────

    def _import_key(self) -> str:
        """The ``meta`` key recording that this ledger's file was imported.

        Per path, because a process can hold more than one ledger and an import
        marked done for one file must not skip another. The path is *hashed*
        rather than spelled out because the key is a 128-character column:
        SQLite ignores that length, PostgreSQL enforces it, so a config
        directory nested deeper than about ninety characters was a control
        plane that started on one backend and failed its first INSERT on the
        other. The readable path is kept as the row's value.
        """
        return f"enrollment.imported_from_json:{_hash(str(self.path))}"

    def _migrate_from_json(self) -> None:
        """Import ``enrollment.json`` once, if there is one.

        Recorded in ``meta`` rather than inferred from empty tables: removing
        the last node empties them, and an import that re-ran then would
        readmit a node the operator had deliberately removed — which for a
        membership list is the worst of the three stores to get wrong.
        """
        if is_done(self._import_key()):
            return
        # read_state_file raises rather than returning empty when the file is
        # there but unreadable: an unreadable ledger must stop a start, not
        # report a cluster with nobody in it.
        data = read_state_file(self.path, expect=dict)
        if data is None:
            return
        # Marker and rows in one transaction. Marking first leaves a window in
        # which the marker says the ledger was imported while none of it was —
        # and for a *membership list* that is the worst of the stores to lose:
        # every enrolled node becomes unknown and is refused on its next
        # connection.
        try:
            with session_scope() as db:
                if not mark_done_within(db, self._import_key(), str(self.path)):
                    return
                for node_id, raw in (data.get("nodes") or {}).items():
                    entry = LedgerEntry.from_dict(raw)
                    # The key wins over the payload: every later write
                    # addresses a row by ``entry.node_id``, so a file whose two
                    # disagreed would leave a row nothing could update again.
                    entry.node_id = node_id
                    _put_node(db, entry)
                for token_hash, raw in (data.get("tokens") or {}).items():
                    grant = TokenGrant.from_dict(raw)
                    grant.token_hash = token_hash
                    _put_token(db, grant)
        except IntegrityError:
            # Another control plane claimed the import between our read and
            # our insert; our whole transaction rolled back and theirs stands.
            return

    def _load(self) -> _State:
        self._migrate_from_json()
        with session_scope() as db:
            nodes = {
                row.node_id: _entry_of(row)
                for row in db.execute(select(_LedgerNodeRow)).scalars()
            }
            tokens = {
                row.token_hash: _grant_of(row)
                for row in db.execute(select(_LedgerTokenRow)).scalars()
            }
        return _State(nodes=nodes, tokens=tokens)

    def _write(
        self,
        *,
        nodes: Iterable[LedgerEntry] = (),
        tokens: Iterable[TokenGrant] = (),
        drop_nodes: Iterable[str] = (),
        drop_tokens: Iterable[str] = (),
    ) -> None:
        """Write exactly the rows named here, in one transaction.

        Every mutation used to go through :meth:`_save`, so admitting one node
        rewrote the row of every node on the desk. The cost is the smaller
        half of the problem: the whole-ledger write also writes back rows this
        instance's working copy is merely *holding*, which is how a field
        another writer has already changed gets quietly replaced by the value
        we read before they changed it. A write that names its rows cannot
        reach a row the call never touched.

        One transaction rather than one per row, because the calls that change
        two rows change them together: redemption marks a token used *and*
        admits the node it minted, and a failure between the two leaves a node
        nobody has a token for. Callers hold ``self._lock`` across the
        read-modify-write that produced these rows — narrowing a write is not
        a substitute for serialising the decision behind it.
        """
        with session_scope() as db:
            for node_id in drop_nodes:
                node_row = db.get(_LedgerNodeRow, node_id)
                if node_row is not None:
                    db.delete(node_row)
            for token_hash in drop_tokens:
                token_row = db.get(_LedgerTokenRow, token_hash)
                if token_row is not None:
                    db.delete(token_row)
            for entry in nodes:
                _put_node(db, entry)
            for grant in tokens:
                _put_token(db, grant)

    def _stored_node(self, node_id: str) -> LedgerEntry | None:
        """One node's row as the database has it, not as this copy has it.

        The working copy answers every read on the connection path; this is
        for the rare write that must not be based on a copy which may have
        been read before somebody else's change landed.
        """
        with session_scope() as db:
            row = db.get(_LedgerNodeRow, node_id)
            return _entry_of(row) if row is not None else None

    def _save(self) -> None:  # noqa: D401 — see the warning in the docstring
        """Write the whole ledger, in one transaction.

        Both tables together, because a node accepted without the token that
        admitted it — or the reverse — is a half-written membership list, and
        that is the state this store exists to make unreachable.

        This is the *reconciling* write, and the reason it survives the move to
        per-row writes: rows absent from the working copy are deleted, so a row
        left behind by a write that failed halfway does not live forever, and
        the ``last_seen`` hints that :meth:`note_seen` deliberately keeps in
        memory reach the database. :meth:`sweep` — housekeeping, off every
        connection path — is what calls it.
        """
        with session_scope() as db:
            for row in list(db.execute(select(_LedgerNodeRow)).scalars()):
                if row.node_id not in self._state.nodes:
                    db.delete(row)
            for entry in self._state.nodes.values():
                _put_node(db, entry)
            for row in list(db.execute(select(_LedgerTokenRow)).scalars()):
                if row.token_hash not in self._state.tokens:
                    db.delete(row)
            for grant in self._state.tokens.values():
                _put_token(db, grant)

    # ── Tokens ───────────────────────────────────────────────────────────

    def mint_token(
        self, name: str, *, ttl: int = TOKEN_TTL_SECONDS, node_id: str = ""
    ) -> str:
        """Mint a single-use token scoped to one node, and return the secret.

        The secret is returned once and never stored; only its hash is kept.

        ``node_id`` pins the identity this token will mint. A caller that has
        already registered the machine passes the registry's id so the two
        stores agree; a caller that has not omits it and redemption mints.
        """
        secret = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            stale = self._sweep_locked(now)
            grant = TokenGrant(
                token_hash=_hash(secret),
                name=name,
                created_at=now,
                expires_at=now + ttl,
                assign_node_id=node_id,
            )
            self._state.tokens[grant.token_hash] = grant
            self._write(tokens=(grant,), drop_tokens=stale)
        return secret

    def redeem_token(self, secret: str, *, now: float | None = None) -> str:
        """Redeem a token and return the node id it minted.

        Raises :class:`EnrollmentRejected` for a token that is unknown,
        expired, or already used — with a message that names which, because an
        operator debugging a failed install needs to know whether to wait or
        to mint another.

        The uuid is minted *here*, on redemption — unless the control plane
        already chose one when it minted the token. Either way the node never
        proposes an identity and cannot choose one, which is the property that
        matters.
        """
        now = now if now is not None else time.time()
        with self._lock:
            grant = self._state.tokens.get(_hash(secret))
            if grant is None:
                raise EnrollmentRejected("enrollment token is not recognised")
            if grant.used_at:
                # Kept, not deleted, precisely so reuse is *refused* with a
                # true statement rather than dropping to "not recognised".
                raise EnrollmentRejected(
                    f"enrollment token for {grant.name!r} was already used"
                )
            if now >= grant.expires_at:
                raise EnrollmentRejected(
                    f"enrollment token for {grant.name!r} expired "
                    f"{int(now - grant.expires_at)}s ago"
                )
            node_id = grant.assign_node_id or mint_node_id()
            grant.used_at = now
            grant.node_id = node_id
            entry = LedgerEntry(
                node_id=node_id,
                name=grant.name,
                state=NodeState.PENDING.value,
                enrolled_at=now,
            )
            self._state.nodes[node_id] = entry
            # Both rows or neither: a node admitted without the token that
            # spent itself admitting it is a token that can be redeemed twice.
            self._write(nodes=(entry,), tokens=(grant,))
        return node_id

    def revoke_token(self, secret: str) -> bool:
        """Invalidate a token whether or not it was ever redeemed.

        §3.1 step 8 is "the control plane invalidates the token", and that has
        to hold on the failure path too: an install that died between minting
        and enrolling would otherwise leave a live ten-minute credential on a
        node it never finished configuring. Redemption already marks a token
        used; this marks an unredeemed one used as well, so the same call ends
        a token's life either way.

        Returns whether a token was found to invalidate.
        """
        with self._lock:
            grant = self._state.tokens.get(_hash(secret))
            if grant is None:
                return False
            if not grant.used_at:
                grant.used_at = time.time()
                self._write(tokens=(grant,))
            return True

    def token_for(self, secret: str) -> TokenGrant | None:
        """The grant behind a secret, for diagnostics only."""
        with self._lock:
            return self._state.tokens.get(_hash(secret))

    # ── Membership ───────────────────────────────────────────────────────

    def record_issue(
        self,
        node_id: str,
        *,
        public_key_fingerprint: str,
        not_after: float,
        facts: dict[str, str] | None = None,
    ) -> LedgerEntry:
        """Record a certificate issued to a node and accept it."""
        facts = facts or {}
        with self._lock:
            # The stored row, not only the working copy: enrolment is rare
            # enough to afford one read, and it is the call that must not
            # start from a copy read before another writer's change landed.
            stored = self._stored_node(node_id)
            entry = (
                self._state.nodes.get(node_id) or stored or LedgerEntry(node_id=node_id)
            )
            entry.state = NodeState.ACCEPTED.value
            entry.public_key_fingerprint = public_key_fingerprint
            entry.cert_not_after = not_after
            # A counter is the one field a narrow write can silently roll
            # back. ``issued`` exists so that a node re-enrolling in a loop
            # shows up as a jump; writing our count over a higher stored one
            # would turn that jump back into a 1, which is the reading that
            # says nothing is wrong.
            entry.issued = max(entry.issued, stored.issued if stored else 0) + 1
            entry.denied_reason = ""
            if not entry.enrolled_at:
                entry.enrolled_at = time.time()
            for key in ("machine_id", "boot_id", "hardware_fingerprint"):
                if facts.get(key):
                    setattr(entry, key, facts[key])
            self._state.nodes[node_id] = entry
            self._write(nodes=(entry,))
            return entry

    def get(self, node_id: str) -> LedgerEntry | None:
        with self._lock:
            return self._state.nodes.get(node_id)

    def nodes(self) -> list[LedgerEntry]:
        with self._lock:
            return list(self._state.nodes.values())

    def deny(self, node_id: str, reason: str) -> None:
        """Mark a node denied. Takes effect on its next connection."""
        with self._lock:
            entry = self._state.nodes.get(node_id)
            if entry is None:
                return
            entry.state = NodeState.DENIED.value
            entry.denied_reason = reason
            self._write(nodes=(entry,))
        logger.warning("node %s denied: %s", node_id, reason)

    def remove(self, node_id: str) -> None:
        """Forget a node entirely; it must re-enroll to return.

        This is the *Remove* action of §3.1, and is deliberately distinct from
        "uninstall, keep identity" — that one leaves this row alone so a
        reinstall rejoins. k3s's uninstall removes config but not identity,
        and that asymmetry is why reinstall works there and reimaging does not.
        """
        with self._lock:
            self._state.nodes.pop(node_id, None)
            # By id, not by "everything except this one": a node this ledger
            # never knew about — enrolled against the same database by someone
            # else — is not something a removal was asked to delete.
            self._write(drop_nodes=(node_id,))

    def note_seen(self, node_id: str) -> None:
        with self._lock:
            entry = self._state.nodes.get(node_id)
            if entry is None:
                return
            entry.last_seen = time.time()
            # Deliberately not written: a heartbeat every ten seconds must not
            # be a database write every ten seconds. Liveness lives in the hub,
            # in memory, where it belongs; this field is a coarse hint that
            # reaches storage with this node's next real mutation — a renewal,
            # a denial — or with the next sweep, which writes the ledger whole.

    # ── Authorisation ────────────────────────────────────────────────────

    def authorize(
        self,
        node_id: str,
        *,
        public_key_fingerprint: str = "",
        facts: dict[str, str] | None = None,
    ) -> IdentityVerdict:
        """Whether a connecting node may hold the identity it presents.

        Called on every connection, not only the first, because that is what
        makes revocation work without a CRL.
        """
        facts = facts or {}
        with self._lock:
            entry = self._state.nodes.get(node_id)
            if entry is None:
                return IdentityVerdict(False, "node is not enrolled")
            if entry.state == NodeState.DENIED.value:
                return IdentityVerdict(False, entry.denied_reason or "node is denied")
            if entry.state != NodeState.ACCEPTED.value:
                return IdentityVerdict(False, f"node is {entry.state}")
            if (
                public_key_fingerprint
                and entry.public_key_fingerprint
                and public_key_fingerprint != entry.public_key_fingerprint
            ):
                reason = "a different key was presented for this node id"
                entry.state = NodeState.DENIED.value
                entry.denied_reason = reason
                self._write(nodes=(entry,))
                return IdentityVerdict(False, reason)
            fingerprint = facts.get("hardware_fingerprint") or ""
            if (
                fingerprint
                and entry.hardware_fingerprint
                and fingerprint != entry.hardware_fingerprint
            ):
                reason = "hardware fingerprint changed; the node may be reimaged"
                entry.state = NodeState.DENIED.value
                entry.denied_reason = reason
                self._write(nodes=(entry,))
                return IdentityVerdict(False, reason)
            return IdentityVerdict(True)

    # ── Housekeeping ─────────────────────────────────────────────────────

    def sweep(self, *, now: float | None = None) -> int:
        """Drop tokens that are both expired and long past use.

        Expiry-based sweeping is what Swarm does, and it is what keeps a
        blacklist from growing without bound. A *used* token is kept for a
        grace period so that a retried install is told "already used" rather
        than the less useful "not recognised".

        The one caller of the whole-ledger write, and the only one that should
        be: housekeeping is off every connection path, it is where the cost of
        rewriting a list bounded by the machines on a desk is affordable, and
        reconciling the tables with the working copy is exactly the job it was
        given. Everything on the enrollment and connection paths writes the
        rows it changed.
        """
        with self._lock:
            removed = len(self._sweep_locked(now if now is not None else time.time()))
            if removed:
                self._save()
            return removed

    def _sweep_locked(self, now: float) -> list[str]:
        """Drop stale tokens from the working copy; return what was dropped.

        The hashes rather than a count, because the caller has to write the
        deletions too — a token dropped from memory and left in the table is a
        token the next reload readmits.
        """
        grace = TOKEN_TTL_SECONDS
        stale = [
            token_hash
            for token_hash, grant in self._state.tokens.items()
            if now > grant.expires_at + grace
            and (not grant.used_at or now > grant.used_at + grace)
        ]
        for token_hash in stale:
            del self._state.tokens[token_hash]
        return stale
