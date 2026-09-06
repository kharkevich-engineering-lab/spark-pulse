"""Identity, enrollment tokens and the ledger — no sockets, no hardware.

Everything here runs in milliseconds against a temporary directory. The
transport tests in ``test_agent_transport.py`` exercise the same objects over
a real mTLS connection; these pin the properties that are easier to state than
to observe.
"""

from __future__ import annotations

import datetime as dt
import random

import hashlib

import pytest
from cryptography import x509

from spark_pulse.agent import identity as ident
from spark_pulse.agent import keepalive
from spark_pulse.agent.enrollment import (
    TOKEN_TTL_SECONDS,
    EnrollmentLedger,
    NodeState,
)
from spark_pulse.agent.errors import EnrollmentRejected
from spark_pulse.agent.store import AgentIdentity


@pytest.fixture
def ca(tmp_path):
    return ident.CertificateAuthority.load_or_create(tmp_path / "ca")


@pytest.fixture
def ledger(tmp_path):
    return EnrollmentLedger(tmp_path / "enrollment.json")


# ── The CA ──────────────────────────────────────────────────────────────────


def test_ca_is_created_once_and_reloaded(tmp_path):
    """A control plane that restarts keeps the authority it already had.

    Minting a new one would silently invalidate every enrolled node, which is
    the kind of failure that looks like a network problem for a day.
    """
    first = ident.CertificateAuthority.load_or_create(tmp_path / "ca")
    second = ident.CertificateAuthority.load_or_create(tmp_path / "ca")
    assert first.certificate_pem == second.certificate_pem
    assert first.trust_bundle_pin == second.trust_bundle_pin


def test_ca_key_is_not_world_readable(tmp_path):
    ident.CertificateAuthority.load_or_create(tmp_path / "ca")
    mode = (tmp_path / "ca" / "ca.key").stat().st_mode & 0o777
    assert mode == 0o600


def test_node_certificate_carries_only_a_spiffe_uri(ca):
    """Identity is logical. No hostname, no address, nothing impersonable."""
    pair = ident.build_csr()
    issued = ca.issue_node_certificate(pair.csr_pem, "abc-123")
    cert = x509.load_pem_x509_certificate(issued.certificate_pem)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.UniformResourceIdentifier) == [
        "spiffe://spark-pulse/node/abc-123"
    ]
    assert san.get_values_for_type(x509.DNSName) == []
    assert san.get_values_for_type(x509.IPAddress) == []
    assert ident.peer_node_id(issued.certificate_pem) == "abc-123"


def test_node_certificate_is_client_auth_only(ca):
    """A node certificate cannot be replayed as a server certificate.

    Extended key usage cannot separate a control plane from a minion on its
    own — that is what the role in the SPIFFE path is for — but it does stop a
    stolen agent key from standing up something other agents will talk to.
    """
    pair = ident.build_csr()
    issued = ca.issue_node_certificate(pair.csr_pem, "abc-123")
    cert = x509.load_pem_x509_certificate(issued.certificate_pem)
    usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(usage) == [x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]


def test_certificates_are_backdated_against_a_slow_clock(ca):
    """A node whose clock is behind must not reject what it was just given."""
    pair = ident.build_csr()
    issued = ca.issue_node_certificate(pair.csr_pem, "abc-123")
    skew = (dt.datetime.now(dt.timezone.utc) - issued.not_before).total_seconds()
    assert skew >= ident.CLOCK_SKEW_BACKDATE - 5


def test_node_certificate_lifetime_is_ninety_days(ca):
    pair = ident.build_csr()
    issued = ca.issue_node_certificate(pair.csr_pem, "abc-123")
    days = (issued.not_after - issued.not_before).days
    assert days == pytest.approx(ident.NODE_CERT_LIFETIME_DAYS, abs=1)


def test_control_plane_identity_is_not_a_node_identity(ca):
    """The role is in the name, so a node URI cannot become a control-plane one."""
    assert ident.node_id_from_spiffe(ident.CONTROL_PLANE_SPIFFE_ID) is None
    assert ident.node_id_from_spiffe("spiffe://elsewhere/node/abc") is None
    assert ident.node_id_from_spiffe("spiffe://spark-pulse/node/a/b") is None
    assert ident.node_id_from_spiffe("spiffe://spark-pulse/node/abc") == "abc"


def test_a_certificate_claiming_two_identities_has_none(ca):
    """Two SPIFFE SANs is not a node with options; it is a certificate we refuse."""
    pair = ident.build_csr()
    issued = ca._sign(
        x509.load_pem_x509_csr(pair.csr_pem),
        ident.node_spiffe_id("first"),
        90,
        server=False,
        extra_sans=[
            x509.UniformResourceIdentifier(ident.node_spiffe_id("second")),
        ],
    )
    assert ident.peer_node_id(issued.certificate_pem) is None


# ── The pin ─────────────────────────────────────────────────────────────────


def test_pin_is_over_the_bundle_not_the_bytes(ca, tmp_path):
    """Re-encoding the bundle must not invalidate every enrollment token."""
    bundle = ca.trust_bundle_pem
    reencoded = b"\n".join(bundle.split(b"\n")) + b"\n\n"
    assert ident.spki_pin(reencoded) == ident.spki_pin(bundle)


def test_pin_changes_when_an_authority_is_added(ca, tmp_path):
    other = ident.CertificateAuthority.load_or_create(tmp_path / "other-ca")
    combined = ca.trust_bundle_pem + other.trust_bundle_pem
    assert ident.spki_pin(combined) != ca.trust_bundle_pin


def test_pin_does_not_depend_on_bundle_order(ca, tmp_path):
    other = ident.CertificateAuthority.load_or_create(tmp_path / "other-ca")
    a, b = ca.trust_bundle_pem, other.trust_bundle_pem
    assert ident.spki_pin(a + b) == ident.spki_pin(b + a)


def test_empty_bundle_is_an_error_not_a_pin():
    with pytest.raises(ValueError):
        ident.spki_pin(b"")


# ── Renewal timing ──────────────────────────────────────────────────────────


def test_renewal_lands_between_fifty_and_eighty_percent_of_life():
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    not_after = now + dt.timedelta(days=90)
    lifetime = 90 * 86400
    seen = set()
    for seed in range(200):
        delay = ident.renewal_delay(now, not_after, now=now, rng=random.Random(seed))
        assert 0.50 * lifetime <= delay <= 0.80 * lifetime
        seen.add(round(delay))
    # Jitter, not a constant: a rack enrolled in one minute must not renew in
    # one minute.
    assert len(seen) > 100


def test_renewal_of_an_overdue_certificate_is_immediate():
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    assert (
        ident.renewal_delay(
            now - dt.timedelta(days=89), now + dt.timedelta(days=1), now=now
        )
        == 0.0
    )


# ── Keepalive ───────────────────────────────────────────────────────────────


def test_server_min_ping_interval_is_below_the_client_keepalive():
    """The ENHANCE_YOUR_CALM trap, asserted rather than remembered.

    Break the relationship and the connection dies, the client silently
    doubles its interval, and detection gets slower over hours with nothing in
    any log saying why.
    """
    keepalive.check_invariant()
    assert keepalive.SERVER_MIN_PING_INTERVAL_MS < keepalive.CLIENT_KEEPALIVE_MS


def test_both_sides_permit_pings_without_calls():
    """An agent holding an idle stream is the normal case, not the exception."""
    for options in (keepalive.client_options(), keepalive.server_options()):
        assert ("grpc.keepalive_permit_without_calls", 1) in options


def test_the_invariant_is_checkable_and_fails_loudly(monkeypatch):
    monkeypatch.setattr(keepalive, "SERVER_MIN_PING_INTERVAL_MS", 20_000)
    with pytest.raises(ValueError, match="ENHANCE_YOUR_CALM"):
        keepalive.check_invariant()


# ── Tokens ──────────────────────────────────────────────────────────────────


def test_token_mints_a_random_uuid_not_the_name(ledger):
    """The node never proposes an identity and cannot choose one."""
    first = ledger.redeem_token(ledger.mint_token("spark-a"))
    second = ledger.redeem_token(ledger.mint_token("spark-a"))
    assert first != second
    assert "spark-a" not in first


def test_the_token_itself_is_never_stored(ledger, tmp_path):
    """Only the SHA-256 is kept, so a stolen state store yields no token.

    Asserted against what the store actually holds rather than against a
    serialisation of it: the ledger lives in the database now, and what matters
    is that the secret is absent from whatever is written, not from the shape
    it used to be written in. On SQLite that is the bytes of the file, which
    would catch the secret leaking into any column or index; on a server
    backend the file is on another machine, so it is the rows themselves.
    """
    from pathlib import Path

    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from spark_pulse import db as database

    secret = ledger.mint_token("spark-a")
    url = make_url(database.database_url())
    if url.get_backend_name() == "sqlite":
        database.dispose()  # flush the pool so the file on disk is complete
        stored = Path(url.database or "").read_bytes()
    else:
        with database.session_scope() as session:
            rows = session.execute(text("SELECT * FROM enrollment_tokens")).all()
        stored = repr(rows).encode()

    assert secret.encode() not in stored
    assert hashlib.sha256(secret.encode()).hexdigest().encode() in stored


def test_token_reuse_is_refused_and_says_so(ledger):
    secret = ledger.mint_token("spark-a")
    ledger.redeem_token(secret)
    with pytest.raises(EnrollmentRejected, match="already used"):
        ledger.redeem_token(secret)


def test_expired_token_is_refused_and_says_so(ledger):
    secret = ledger.mint_token("spark-a")
    with pytest.raises(EnrollmentRejected, match="expired"):
        ledger.redeem_token(secret, now=9e9)


def test_token_lifetime_is_ten_minutes(ledger):
    secret = ledger.mint_token("spark-a")
    grant = ledger.token_for(secret)
    assert TOKEN_TTL_SECONDS == 600
    assert grant.expires_at - grant.created_at == pytest.approx(600, abs=1)


def test_unknown_token_is_refused(ledger):
    with pytest.raises(EnrollmentRejected, match="not recognised"):
        ledger.redeem_token("not-a-token")


def test_sweep_drops_only_long_dead_tokens(ledger):
    ledger.mint_token("spark-a")
    assert ledger.sweep() == 0
    assert ledger.sweep(now=9e9) == 1


def test_ledger_survives_a_reload(tmp_path):
    first = EnrollmentLedger(tmp_path / "e.json")
    node_id = first.redeem_token(first.mint_token("spark-a"))
    first.record_issue(node_id, public_key_fingerprint="abc", not_after=1.0)
    second = EnrollmentLedger(tmp_path / "e.json")
    assert second.get(node_id).state == NodeState.ACCEPTED.value
    assert second.authorize(node_id)


def test_the_json_import_runs_once_and_a_removal_does_not_undo_it(tmp_path):
    """Keyed on the ``meta`` table, never on "are the tables empty".

    Removing the last node empties them, and an import keyed on emptiness
    would then readmit exactly the node an operator had deliberately removed —
    which for a membership list is the worst of the stores to get wrong.
    """
    import json

    path = tmp_path / "e.json"
    path.write_text(
        json.dumps(
            {
                "nodes": {"node-1": {"node_id": "node-1", "state": "accepted"}},
                "tokens": {},
            }
        )
    )
    assert EnrollmentLedger(path).get("node-1").state == NodeState.ACCEPTED.value

    EnrollmentLedger(path).remove("node-1")

    assert EnrollmentLedger(path).get("node-1") is None


# ── One row at a time ───────────────────────────────────────────────────────
#
# Two ledgers over one database is how these state "somebody else wrote a row
# we have not seen". It is also the only cheap way to observe *which* rows a
# write touched: a write that rewrote the whole ledger from its own working
# copy shows up as the other ledger's node quietly disappearing.


def test_accepting_one_node_leaves_a_node_it_never_saw_alone(tmp_path):
    """One acceptance writes one row.

    The whole-ledger write deleted every row absent from the working copy, so
    admitting a node erased any node enrolled after this ledger last read.
    """
    first = EnrollmentLedger(tmp_path / "e.json")
    second = EnrollmentLedger(tmp_path / "e.json")
    mine = first.redeem_token(first.mint_token("spark-a"))
    theirs = second.redeem_token(second.mint_token("spark-b"))

    first.record_issue(mine, public_key_fingerprint="k", not_after=1.0)

    reloaded = EnrollmentLedger(tmp_path / "e.json")
    assert reloaded.get(mine).state == NodeState.ACCEPTED.value
    assert reloaded.get(theirs) is not None


def test_denying_one_node_leaves_a_node_it_never_saw_alone(tmp_path):
    """Denial is the mutation on the connection path, and it is one row."""
    first = EnrollmentLedger(tmp_path / "e.json")
    second = EnrollmentLedger(tmp_path / "e.json")
    mine = first.redeem_token(first.mint_token("spark-a"))
    theirs = second.redeem_token(second.mint_token("spark-b"))

    first.deny(mine, "operator said so")

    reloaded = EnrollmentLedger(tmp_path / "e.json")
    assert reloaded.get(mine).state == NodeState.DENIED.value
    assert reloaded.get(theirs) is not None


def test_removing_a_node_deletes_that_row_and_no_other(tmp_path):
    """A removal names the node it removes, rather than everything else."""
    first = EnrollmentLedger(tmp_path / "e.json")
    second = EnrollmentLedger(tmp_path / "e.json")
    mine = first.redeem_token(first.mint_token("spark-a"))
    theirs = second.redeem_token(second.mint_token("spark-b"))

    first.remove(mine)

    reloaded = EnrollmentLedger(tmp_path / "e.json")
    assert reloaded.get(mine) is None
    assert reloaded.get(theirs) is not None


def test_a_redemption_stores_the_spent_token_and_the_node_together(ledger, tmp_path):
    """Half a redemption is a token that can be spent a second time."""
    secret = ledger.mint_token("spark-a")
    node_id = ledger.redeem_token(secret)

    reloaded = EnrollmentLedger(tmp_path / "enrollment.json")
    assert reloaded.get(node_id).state == NodeState.PENDING.value
    with pytest.raises(EnrollmentRejected, match="already used"):
        reloaded.redeem_token(secret)


def test_a_certificate_count_is_not_reset_by_a_ledger_that_missed_the_others(tmp_path):
    """``issued`` is the "re-enrolling in a loop" signal, and it is a counter.

    A counter is the one field a narrow write can silently roll back: this
    ledger's copy says zero because it has not read since. Writing one over a
    stored two is the reading that says nothing is wrong.
    """
    first = EnrollmentLedger(tmp_path / "e.json")
    node_id = first.redeem_token(first.mint_token("spark-a"))
    second = EnrollmentLedger(tmp_path / "e.json")
    second.record_issue(node_id, public_key_fingerprint="k", not_after=1.0)
    second.record_issue(node_id, public_key_fingerprint="k", not_after=1.0)

    assert first.record_issue(node_id, public_key_fingerprint="k", not_after=1.0).issued
    assert EnrollmentLedger(tmp_path / "e.json").get(node_id).issued == 3


def test_a_heartbeat_reaches_the_database_with_that_nodes_next_write(tmp_path):
    """Ten-second heartbeats are not ten-second database writes."""
    ledger = EnrollmentLedger(tmp_path / "e.json")
    node_id = ledger.redeem_token(ledger.mint_token("spark-a"))

    ledger.note_seen(node_id)
    seen = ledger.get(node_id).last_seen
    assert seen > 0
    assert EnrollmentLedger(tmp_path / "e.json").get(node_id).last_seen == 0.0

    ledger.record_issue(node_id, public_key_fingerprint="k", not_after=1.0)
    assert EnrollmentLedger(tmp_path / "e.json").get(node_id).last_seen == seen


def test_the_housekeeping_sweep_is_the_write_that_reconciles_the_whole_ledger(tmp_path):
    """The whole-ledger write keeps its semantics: absent from the set, gone.

    That is why nothing on the enrollment or connection path uses it any more.
    Housekeeping is where reconciling the tables with the working copy is the
    job rather than a side effect of saving one node.
    """
    first = EnrollmentLedger(tmp_path / "e.json")
    second = EnrollmentLedger(tmp_path / "e.json")
    theirs = second.redeem_token(second.mint_token("spark-b"))
    first.mint_token("spark-a")

    assert first.sweep(now=9e9) == 1

    reloaded = EnrollmentLedger(tmp_path / "e.json")
    assert reloaded.get(theirs) is None


def test_minting_a_token_deletes_the_tokens_its_sweep_dropped(tmp_path):
    """A token dropped from memory and left in the table comes back on reload."""
    ledger = EnrollmentLedger(tmp_path / "e.json")
    ledger.mint_token("spark-a", ttl=-2 * TOKEN_TTL_SECONDS)
    ledger.mint_token("spark-b")

    reloaded = EnrollmentLedger(tmp_path / "e.json")
    assert reloaded.sweep(now=9e9) == 1


# ── Membership and reimage detection ────────────────────────────────────────


def test_an_unenrolled_node_is_not_authorized(ledger):
    assert not ledger.authorize("never-seen")
    assert "not enrolled" in ledger.authorize("never-seen").reason


def test_a_new_key_for_an_accepted_uuid_denies_the_node(ledger):
    """A reimaged or copied identity is surfaced, never silently trusted."""
    node_id = ledger.redeem_token(ledger.mint_token("spark-a"))
    ledger.record_issue(node_id, public_key_fingerprint="key-one", not_after=1.0)
    verdict = ledger.authorize(node_id, public_key_fingerprint="key-two")
    assert not verdict
    assert "different key" in verdict.reason
    # And it stays denied: the decision is a human's, not the next retry's.
    assert not ledger.authorize(node_id, public_key_fingerprint="key-one")


def test_a_changed_hardware_fingerprint_denies_the_node(ledger):
    node_id = ledger.redeem_token(ledger.mint_token("spark-a"))
    ledger.record_issue(
        node_id,
        public_key_fingerprint="key-one",
        not_after=1.0,
        facts={"hardware_fingerprint": "board-one"},
    )
    verdict = ledger.authorize(
        node_id,
        public_key_fingerprint="key-one",
        facts={"hardware_fingerprint": "board-two"},
    )
    assert not verdict
    assert "reimaged" in verdict.reason


def test_a_reboot_does_not_deny_the_node(ledger):
    """boot_id changes every reboot, so it must not be an identity check."""
    node_id = ledger.redeem_token(ledger.mint_token("spark-a"))
    ledger.record_issue(
        node_id,
        public_key_fingerprint="key-one",
        not_after=1.0,
        facts={"boot_id": "boot-one", "hardware_fingerprint": "board-one"},
    )
    assert ledger.authorize(
        node_id,
        public_key_fingerprint="key-one",
        facts={"boot_id": "boot-two", "hardware_fingerprint": "board-one"},
    )


def test_removal_and_denial_are_different_actions(ledger):
    node_id = ledger.redeem_token(ledger.mint_token("spark-a"))
    ledger.record_issue(node_id, public_key_fingerprint="k", not_after=1.0)
    ledger.deny(node_id, "operator said so")
    assert ledger.get(node_id).state == NodeState.DENIED.value
    ledger.remove(node_id)
    assert ledger.get(node_id) is None


def test_an_unreadable_ledger_refuses_to_start(tmp_path):
    """Not an empty cluster. §3.3: refuse to start, do not report nothing."""
    from spark_pulse.tools.atomic_json import StateFileError

    path = tmp_path / "e.json"
    path.write_text("{ this is not json")
    with pytest.raises(StateFileError):
        EnrollmentLedger(path)


# ── The node's identity directory ───────────────────────────────────────────


def test_identity_round_trips(tmp_path, ca):
    pair = ident.build_csr()
    issued = ca.issue_node_certificate(pair.csr_pem, "abc-123")
    identity = AgentIdentity(
        directory=tmp_path / "agent",
        node_id="abc-123",
        key_pem=pair.key_pem,
        certificate_pem=issued.certificate_pem,
        trust_bundle_pem=ca.trust_bundle_pem,
        trust_bundle_pin=ca.trust_bundle_pin,
    )
    identity.save()
    loaded = AgentIdentity.load(tmp_path / "agent")
    assert loaded.node_id == "abc-123"
    assert loaded.trust_bundle_pin == ca.trust_bundle_pin
    assert (tmp_path / "agent" / "node.key").stat().st_mode & 0o777 == 0o600


def test_no_identity_is_none_but_half_an_identity_is_an_error(tmp_path):
    """Half an identity is a failed install, not a node that never enrolled."""
    assert AgentIdentity.load(tmp_path / "agent") is None
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "node.crt").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="partial agent identity"):
        AgentIdentity.load(tmp_path / "agent")


def test_destroy_is_the_remove_action(tmp_path, ca):
    pair = ident.build_csr()
    issued = ca.issue_node_certificate(pair.csr_pem, "abc-123")
    identity = AgentIdentity(
        directory=tmp_path / "agent",
        node_id="abc-123",
        key_pem=pair.key_pem,
        certificate_pem=issued.certificate_pem,
        trust_bundle_pem=ca.trust_bundle_pem,
        trust_bundle_pin=ca.trust_bundle_pin,
    )
    identity.save()
    identity.destroy()
    assert AgentIdentity.load(tmp_path / "agent") is None


def test_a_renewal_carrying_a_different_bundle_is_refused(tmp_path, ca):
    """The one thing a pin exists to catch."""

    other = ident.CertificateAuthority.load_or_create(tmp_path / "other")
    pair = ident.build_csr()
    issued = ca.issue_node_certificate(pair.csr_pem, "abc-123")
    identity = AgentIdentity(
        directory=tmp_path / "agent",
        node_id="abc-123",
        key_pem=pair.key_pem,
        certificate_pem=issued.certificate_pem,
        trust_bundle_pem=ca.trust_bundle_pem,
        trust_bundle_pin=ca.trust_bundle_pin,
    )
    identity.save()
    # The check renewal makes before adopting anything. A renewal that arrives
    # carrying a different trust bundle is the one thing a pin exists to
    # catch, and adopting it would delete the protection at exactly the moment
    # it mattered. The agent refuses it; this is the predicate it refuses on.
    assert identity.verify_pin(identity.trust_bundle_pem)
    assert not identity.verify_pin(other.trust_bundle_pem)
    del issued
