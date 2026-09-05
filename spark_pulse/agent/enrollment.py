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

Storage is the repository's own atomic JSON writer, so the ledger inherits the
same durability the deployment records have: fsync, atomic rename, and a
refusal to start on an unreadable file rather than a cheerful empty cluster
(§3.3). Desired *state* moves to SQLite in a later step; the ledger is a small
append-mostly membership list and does not need it.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from spark_pulse.agent.errors import EnrollmentRejected
from spark_pulse.agent.identity import mint_node_id
from spark_pulse.tools.atomic_json import read_state_file, write_json_atomic

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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenGrant:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class _State:
    nodes: dict[str, LedgerEntry] = field(default_factory=dict)
    tokens: dict[str, TokenGrant] = field(default_factory=dict)


class EnrollmentLedger:
    """Who may connect, and with which key.

    Thread-safe: the control plane's event loop and its request threads both
    reach it. Every mutation writes the whole file atomically, which is ample
    for a list bounded by the number of machines on a desk.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state = self._load()

    # ── Persistence ──────────────────────────────────────────────────────

    def _load(self) -> _State:
        # read_state_file raises rather than returning empty when the file is
        # there but unreadable: an unreadable ledger must stop a start, not
        # report a cluster with nobody in it.
        data = read_state_file(self.path, expect=dict)
        if not data:
            return _State()
        nodes = {
            node_id: LedgerEntry.from_dict(raw)
            for node_id, raw in (data.get("nodes") or {}).items()
        }
        tokens = {
            token_hash: TokenGrant.from_dict(raw)
            for token_hash, raw in (data.get("tokens") or {}).items()
        }
        return _State(nodes=nodes, tokens=tokens)

    def _save(self) -> None:
        write_json_atomic(
            self.path,
            {
                "version": 1,
                "nodes": {k: v.to_dict() for k, v in self._state.nodes.items()},
                "tokens": {k: v.to_dict() for k, v in self._state.tokens.items()},
            },
            mode=0o600,
        )

    # ── Tokens ───────────────────────────────────────────────────────────

    def mint_token(self, name: str, *, ttl: int = TOKEN_TTL_SECONDS) -> str:
        """Mint a single-use token scoped to one node, and return the secret.

        The secret is returned once and never stored; only its hash is kept.
        """
        secret = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            self._state.tokens[_hash(secret)] = TokenGrant(
                token_hash=_hash(secret),
                name=name,
                created_at=now,
                expires_at=now + ttl,
            )
            self._save()
        return secret

    def redeem_token(self, secret: str, *, now: float | None = None) -> str:
        """Redeem a token and return the node id it minted.

        Raises :class:`EnrollmentRejected` for a token that is unknown,
        expired, or already used — with a message that names which, because an
        operator debugging a failed install needs to know whether to wait or
        to mint another.

        The uuid is minted *here*, on redemption. The node never proposes an
        identity and cannot choose one.
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
            node_id = mint_node_id()
            grant.used_at = now
            grant.node_id = node_id
            self._state.nodes[node_id] = LedgerEntry(
                node_id=node_id,
                name=grant.name,
                state=NodeState.PENDING.value,
                enrolled_at=now,
            )
            self._save()
        return node_id

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
            entry = self._state.nodes.get(node_id) or LedgerEntry(node_id=node_id)
            entry.state = NodeState.ACCEPTED.value
            entry.public_key_fingerprint = public_key_fingerprint
            entry.cert_not_after = not_after
            entry.issued += 1
            entry.denied_reason = ""
            if not entry.enrolled_at:
                entry.enrolled_at = time.time()
            for key in ("machine_id", "boot_id", "hardware_fingerprint"):
                if facts.get(key):
                    setattr(entry, key, facts[key])
            self._state.nodes[node_id] = entry
            self._save()
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
            self._save()
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
            self._save()

    def note_seen(self, node_id: str) -> None:
        with self._lock:
            entry = self._state.nodes.get(node_id)
            if entry is None:
                return
            entry.last_seen = time.time()
            # Deliberately not saved: a heartbeat every ten seconds must not
            # be a disk write every ten seconds. Liveness lives in the hub,
            # in memory, where it belongs; this field is a coarse hint that
            # gets flushed by the next real mutation.

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
                self._save()
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
                self._save()
                return IdentityVerdict(False, reason)
            return IdentityVerdict(True)

    # ── Housekeeping ─────────────────────────────────────────────────────

    def sweep(self, *, now: float | None = None) -> int:
        """Drop tokens that are both expired and long past use.

        Expiry-based sweeping is what Swarm does, and it is what keeps a
        blacklist from growing without bound. A *used* token is kept for a
        grace period so that a retried install is told "already used" rather
        than the less useful "not recognised".
        """
        with self._lock:
            removed = self._sweep_locked(now if now is not None else time.time())
            if removed:
                self._save()
            return removed

    def _sweep_locked(self, now: float) -> int:
        grace = TOKEN_TTL_SECONDS
        stale = [
            token_hash
            for token_hash, grant in self._state.tokens.items()
            if now > grant.expires_at + grace
            and (not grant.used_at or now > grant.used_at + grace)
        ]
        for token_hash in stale:
            del self._state.tokens[token_hash]
        return len(stale)
