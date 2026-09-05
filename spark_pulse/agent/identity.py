"""The certificate authority, SPIFFE identities, and the trust-bundle pin.

Everything here follows ``docs/cluster-agent-plan.md`` §3.1-3.2:

* **The CA key never leaves the control node.** A node generates its own key
  and sends a CSR; only the public half ever crosses a wire. This is the one
  thing NVIDIA's ``discover-sparks`` does that we deliberately do not copy —
  it pushes one shared private key to every Spark, so compromising any one of
  them yields access to all of them.
* **Identity is logical, never an address.** Each certificate carries a URI
  subject alternative name and nothing else that identifies the holder:
  ``spiffe://spark-pulse/node/<uuid>`` for an agent,
  ``spiffe://spark-pulse/control-plane`` for the control plane. Nomad keeps
  hostnames and IPs out of agent certificates for exactly this reason —
  otherwise any service on a host can impersonate the agent — and the role
  goes *in the name* because extended key usage cannot separate a control
  plane from a minion.
* **The node id is a server-minted random uuid.** Not the hostname, and
  explicitly not ``/etc/machine-id``: Sparks ship duplicates, and k0s
  abandoned machine-id for identity in v1.30 over the same class of failure.
  See :func:`spark_pulse.tools.node_registry.mint_node_id`, which is the one
  minting function and is reused here rather than duplicated.
* **The CA is pinned by SPKI over the whole trust bundle** — not one
  certificate, and not the PEM bytes. Pinning the bytes means a re-encoded
  bundle breaks every node; pinning one certificate means rotating a CA in
  breaks every node. Hashing the sorted SubjectPublicKeyInfo of *every*
  certificate in the bundle survives both, and still changes the moment an
  unexpected authority is added.
* **NotBefore is backdated five minutes.** A node whose clock is behind must
  not reject the certificate it was just handed. This is one of the two
  "costs a day each" traps §3.1 names.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
import random
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

__all__ = [
    "TRUST_DOMAIN",
    "CONTROL_PLANE_SPIFFE_ID",
    "CA_LIFETIME_DAYS",
    "SERVER_CERT_LIFETIME_DAYS",
    "NODE_CERT_LIFETIME_DAYS",
    "CLOCK_SKEW_BACKDATE",
    "RENEWAL_FRACTION_MIN",
    "RENEWAL_FRACTION_MAX",
    "CertificateAuthority",
    "IssuedCertificate",
    "NodeKeyPair",
    "generate_key",
    "build_csr",
    "node_spiffe_id",
    "node_id_from_spiffe",
    "spiffe_id_of",
    "peer_node_id",
    "spki_pin",
    "public_key_fingerprint",
    "renewal_delay",
]

#: The SPIFFE trust domain. One cluster, one domain.
TRUST_DOMAIN = "spark-pulse"

#: The control plane's own identity. The role lives in the path, so a node
#: certificate can never be replayed as a control-plane certificate.
CONTROL_PLANE_SPIFFE_ID = f"spiffe://{TRUST_DOMAIN}/control-plane"

_NODE_PREFIX = f"spiffe://{TRUST_DOMAIN}/node/"

#: Ten years. The CA outlives every certificate it signs by a wide margin
#: because renewing it is an operator event, not a scheduled one.
CA_LIFETIME_DAYS = 3650

#: One year, rotated hot.
SERVER_CERT_LIFETIME_DAYS = 365

#: Ninety days: deliberately between Consul's 72 hours and k3s's year. Short
#: enough to bound a stolen certificate, long enough that a node switched off
#: for a fortnight comes back on its own.
NODE_CERT_LIFETIME_DAYS = 90

#: How far NotBefore is backdated, in seconds.
CLOCK_SKEW_BACKDATE = 300

#: Renewal happens at a jittered fraction of the certificate's lifetime. The
#: jitter matters at cluster scale: without it every node enrolled in the same
#: minute renews in the same minute, forever.
RENEWAL_FRACTION_MIN = 0.50
RENEWAL_FRACTION_MAX = 0.80


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ── SPIFFE names ────────────────────────────────────────────────────────────


def node_spiffe_id(node_id: str) -> str:
    """The SPIFFE URI for a node uuid."""
    return f"{_NODE_PREFIX}{node_id}"


def node_id_from_spiffe(uri: str) -> str | None:
    """The node uuid inside a SPIFFE URI, or None if it is not a node's.

    A control-plane URI, another trust domain, or a nested path all return
    None rather than something that looks like a uuid.
    """
    if not uri.startswith(_NODE_PREFIX):
        return None
    tail = uri[len(_NODE_PREFIX) :]
    if not tail or "/" in tail:
        return None
    return tail


def spiffe_id_of(cert: x509.Certificate) -> str | None:
    """The single SPIFFE URI SAN in a certificate, or None.

    More than one URI SAN is treated as no identity at all: a certificate
    claiming two identities has none we are willing to act on.
    """
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    uris = [str(u) for u in san.get_values_for_type(x509.UniformResourceIdentifier)]
    spiffe = [u for u in uris if u.startswith(f"spiffe://{TRUST_DOMAIN}/")]
    if len(spiffe) != 1:
        return None
    return spiffe[0]


def peer_node_id(cert_pem: bytes | str) -> str | None:
    """The node uuid a PEM-encoded peer certificate is entitled to."""
    if isinstance(cert_pem, str):
        cert_pem = cert_pem.encode()
    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
    except Exception:
        return None
    uri = spiffe_id_of(cert)
    return node_id_from_spiffe(uri) if uri else None


# ── The trust-bundle pin ────────────────────────────────────────────────────


def spki_pin(bundle_pem: bytes | str) -> str:
    """base64(sha256) over the sorted DER SPKI of every certificate in a bundle.

    Sorted so the pin does not depend on concatenation order, and computed
    over the public keys rather than the certificates so that re-issuing a CA
    certificate for the same key — a renewal — does not invalidate every
    enrollment token in flight.
    """
    if isinstance(bundle_pem, str):
        bundle_pem = bundle_pem.encode()
    certs = x509.load_pem_x509_certificates(bundle_pem)
    if not certs:
        raise ValueError("trust bundle contains no certificates")
    spkis = sorted(
        cert.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        for cert in certs
    )
    digest = hashlib.sha256()
    for spki in spkis:
        digest.update(spki)
    return base64.b64encode(digest.digest()).decode()


def public_key_fingerprint(cert_pem: bytes | str) -> str:
    """sha256 of a certificate's public key, hex.

    Recorded at enrollment so that a *new key* presented for an
    already-accepted uuid is detectable — which, with a matching uuid, means
    the node was reimaged or its identity was copied. ``salt-key`` surfaces
    exactly this for a human decision rather than deciding on its own.
    """
    if isinstance(cert_pem, str):
        cert_pem = cert_pem.encode()
    cert = x509.load_pem_x509_certificate(cert_pem)
    spki = cert.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(spki).hexdigest()


# ── Renewal timing ──────────────────────────────────────────────────────────


def renewal_delay(
    not_before: dt.datetime,
    not_after: dt.datetime,
    *,
    now: dt.datetime | None = None,
    rng: random.Random | None = None,
) -> float:
    """Seconds to wait before renewing, jittered over 50-80% of the lifetime.

    Never negative: a certificate already past its renewal point renews now.
    """
    now = now or _now()
    rng = rng or random
    lifetime = (not_after - not_before).total_seconds()
    fraction = rng.uniform(RENEWAL_FRACTION_MIN, RENEWAL_FRACTION_MAX)
    renew_at = not_before + dt.timedelta(seconds=lifetime * fraction)
    return max(0.0, (renew_at - now).total_seconds())


# ── Keys and CSRs ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NodeKeyPair:
    """A private key that stays on the node, and the CSR built from it."""

    key_pem: bytes
    csr_pem: bytes


def generate_key() -> ec.EllipticCurvePrivateKey:
    """A P-256 key. Fast to generate on a Spark, small on the wire."""
    return ec.generate_private_key(ec.SECP256R1())


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def build_csr(common_name: str = "spark-pulse-node") -> NodeKeyPair:
    """Generate a key and a CSR for it.

    The CSR carries no SAN and no claimed identity. It is a request for *a*
    key to be certified; the control plane decides which identity that key
    gets, and a node cannot ask for one. The common name is a label for
    ``openssl x509 -text`` and nothing reads it.
    """
    key = generate_key()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .sign(key, hashes.SHA256())
    )
    return NodeKeyPair(
        key_pem=_key_pem(key),
        csr_pem=csr.public_bytes(serialization.Encoding.PEM),
    )


# ── The authority ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class IssuedCertificate:
    """A signed certificate and the window it is valid in."""

    certificate_pem: bytes
    not_before: dt.datetime
    not_after: dt.datetime
    spiffe_id: str


class CertificateAuthority:
    """The cluster CA. Its key is written 0600 and never transmitted.

    Construct with :meth:`load_or_create`, which is idempotent: a control
    plane that restarts keeps the authority it already had, because minting a
    new one would silently invalidate every enrolled node.
    """

    def __init__(self, key: ec.EllipticCurvePrivateKey, cert: x509.Certificate):
        self._key = key
        self._cert = cert

    # ── Construction ─────────────────────────────────────────────────────

    @classmethod
    def load_or_create(cls, directory: Path | str) -> CertificateAuthority:
        """Load the CA under ``directory``, creating it on first use."""
        directory = Path(directory)
        key_path = directory / "ca.key"
        cert_path = directory / "ca.crt"
        if key_path.exists() and cert_path.exists():
            key = serialization.load_pem_private_key(key_path.read_bytes(), None)
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            return cls(key, cert)  # type: ignore[arg-type]

        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        key = generate_key()
        now = _now()
        subject = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "spark-pulse cluster CA")]
        )
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(seconds=CLOCK_SKEW_BACKDATE))
            .not_valid_after(now + dt.timedelta(days=CA_LIFETIME_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        # The key is written first and 0600 from the moment it exists: a
        # world-readable window, however short, is the whole compromise.
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(_key_pem(key))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return cls(key, cert)

    # ── The bundle ───────────────────────────────────────────────────────

    @property
    def certificate_pem(self) -> bytes:
        """This CA's own certificate."""
        return self._cert.public_bytes(serialization.Encoding.PEM)

    @property
    def trust_bundle_pem(self) -> bytes:
        """Every authority a node should trust.

        One certificate today. It is a bundle rather than a certificate so
        that adding a second authority — the whole point of a rotation — is a
        change to this property and to nothing else.
        """
        return self.certificate_pem

    @property
    def trust_bundle_pin(self) -> str:
        """The SPKI pin a node stores at enrollment and checks thereafter."""
        return spki_pin(self.trust_bundle_pem)

    # ── Issuing ──────────────────────────────────────────────────────────

    def _sign(
        self,
        csr: x509.CertificateSigningRequest,
        spiffe_id: str,
        lifetime_days: int,
        *,
        server: bool,
        extra_sans: list[x509.GeneralName] | None = None,
    ) -> IssuedCertificate:
        if not csr.is_signature_valid:
            raise ValueError("CSR signature is not valid")
        now = _now()
        not_before = now - dt.timedelta(seconds=CLOCK_SKEW_BACKDATE)
        not_after = now + dt.timedelta(days=lifetime_days)
        sans: list[x509.GeneralName] = [x509.UniformResourceIdentifier(spiffe_id)]
        sans.extend(extra_sans or [])
        usages = [ExtendedKeyUsageOID.SERVER_AUTH]
        if not server:
            usages = [ExtendedKeyUsageOID.CLIENT_AUTH]
        cert = (
            x509.CertificateBuilder()
            .subject_name(csr.subject)
            .issuer_name(self._cert.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
            .sign(self._key, hashes.SHA256())
        )
        return IssuedCertificate(
            certificate_pem=cert.public_bytes(serialization.Encoding.PEM),
            not_before=not_before,
            not_after=not_after,
            spiffe_id=spiffe_id,
        )

    def issue_node_certificate(
        self, csr_pem: bytes, node_id: str, *, lifetime_days: int | None = None
    ) -> IssuedCertificate:
        """Certify a node's key for ``spiffe://spark-pulse/node/<node_id>``.

        Client auth only. A node certificate presented as a *server*
        certificate fails the extended key usage check, so a compromised agent
        cannot stand up something the other agents will talk to.
        """
        csr = x509.load_pem_x509_csr(csr_pem)
        return self._sign(
            csr,
            node_spiffe_id(node_id),
            lifetime_days or NODE_CERT_LIFETIME_DAYS,
            server=False,
        )

    def issue_server_certificate(
        self,
        csr_pem: bytes,
        *,
        dns_names: list[str] | None = None,
        ip_addresses: list[str] | None = None,
        lifetime_days: int | None = None,
    ) -> IssuedCertificate:
        """Certify the control plane's key.

        Unlike a node certificate this one does carry addresses, because a
        client has to match the target it dialled against the certificate
        before it has any other way to know who answered. The SPIFFE URI is
        still what the *agent-side* authorisation reads.
        """
        import ipaddress

        csr = x509.load_pem_x509_csr(csr_pem)
        extra: list[x509.GeneralName] = [
            x509.DNSName(name) for name in dns_names or ["localhost"]
        ]
        for raw in ip_addresses or ["127.0.0.1", "::1"]:
            try:
                extra.append(x509.IPAddress(ipaddress.ip_address(raw)))
            except ValueError:
                continue
        return self._sign(
            csr,
            CONTROL_PLANE_SPIFFE_ID,
            lifetime_days or SERVER_CERT_LIFETIME_DAYS,
            server=True,
            extra_sans=extra,
        )

    def issue_server_identity(
        self,
        *,
        dns_names: list[str] | None = None,
        ip_addresses: list[str] | None = None,
    ) -> tuple[bytes, bytes]:
        """A fresh key and certificate for the control plane's own listener.

        Returns ``(key_pem, certificate_pem)``. Kept here rather than in the
        server so that the only code that touches the CA key is in this file.
        """
        pair = build_csr("spark-pulse control plane")
        issued = self.issue_server_certificate(
            pair.csr_pem, dns_names=dns_names, ip_addresses=ip_addresses
        )
        return pair.key_pem, issued.certificate_pem


def mint_node_id() -> str:
    """A fresh random node id.

    Delegates to the node registry so there is exactly one minting function in
    the codebase, and falls back to :func:`uuid.uuid4` only if the registry is
    not importable — which in practice means a stripped agent-only install.
    """
    try:
        from spark_pulse.tools.node_registry import mint_node_id as registry_mint

        return registry_mint()
    except Exception:  # pragma: no cover — agent-only installs
        return str(uuid.uuid4())
