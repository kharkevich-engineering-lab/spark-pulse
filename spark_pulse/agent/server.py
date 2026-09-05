"""The control plane's listeners.

**A departure from §3.2, and why.** The plan says one inbound port. This is
two, and the reason is measured rather than assumed: Python's
``grpc.ssl_server_credentials`` has no equivalent of Go's
``tls.VerifyClientCertIfGiven``. ``require_client_auth=False`` maps to
``GRPC_SSL_DONT_REQUEST_CLIENT_CERTIFICATE``, so the server never asks for a
client certificate at all and ``x509_pem_cert`` is absent from the auth
context even when the client has one — verified against grpcio 1.83 before
writing this. One port therefore has to be *either* mTLS-required *or*
mTLS-incapable, and a node bootstrapping has no certificate to present.

Merging them would mean the session port accepting unauthenticated peers and
checking identity in application code alone, which deletes the guarantee the
whole identity design rests on. Splitting them keeps it. The property §3.2
actually buys — a fixed number of inbound ports rather than one per node —
survives intact: two, at any cluster size.

The enrollment listener is separately startable for exactly that reason. An
operator who does not want a token endpoint reachable at all times can run it
only while enrolling; :meth:`ControlPlaneServer.start_enrollment` and
:meth:`stop_enrollment` are that switch, and the SSH installer is its natural
caller.
"""

from __future__ import annotations

import logging
from pathlib import Path

import grpc

from spark_pulse.agent import agent_pb2_grpc as pb_grpc
from spark_pulse.agent import identity as ident
from spark_pulse.agent import keepalive
from spark_pulse.agent.enrollment import EnrollmentLedger
from spark_pulse.agent.hub import AgentHub
from spark_pulse.agent.servicer import EnrollmentServicer, NodeSessionServicer

logger = logging.getLogger(__name__)

__all__ = ["ControlPlaneServer", "DEFAULT_SESSION_PORT", "DEFAULT_ENROLLMENT_PORT"]

#: The command channel. mTLS required.
DEFAULT_SESSION_PORT = 8110

#: The bootstrap channel. Server-authenticated TLS, token-authorised.
DEFAULT_ENROLLMENT_PORT = 8111


class ControlPlaneServer:
    """Both listeners, one CA, one hub, one ledger.

    Constructed with everything it needs so a test can build it against a
    temporary directory and an ephemeral port and never touch a real config
    directory or a real network.
    """

    def __init__(
        self,
        *,
        directory: Path | str,
        hub: AgentHub | None = None,
        ledger: EnrollmentLedger | None = None,
        ca: ident.CertificateAuthority | None = None,
        host: str = "0.0.0.0",
        session_port: int = DEFAULT_SESSION_PORT,
        enrollment_port: int = DEFAULT_ENROLLMENT_PORT,
        dns_names: list[str] | None = None,
        ip_addresses: list[str] | None = None,
    ):
        self.directory = Path(directory)
        self.ca = ca or ident.CertificateAuthority.load_or_create(self.directory / "ca")
        self.ledger = ledger or EnrollmentLedger(self.directory / "enrollment.json")
        self.hub = hub or AgentHub()
        self.host = host
        self._session_port = session_port
        self._enrollment_port = enrollment_port
        self._server_key, self._server_cert = self.ca.issue_server_identity(
            dns_names=dns_names, ip_addresses=ip_addresses
        )
        self._session: grpc.aio.Server | None = None
        self._enrollment: grpc.aio.Server | None = None

    # ── Credentials ──────────────────────────────────────────────────────

    def _session_credentials(self) -> grpc.ServerCredentials:
        return grpc.ssl_server_credentials(
            [(self._server_key, self._server_cert)],
            root_certificates=self.ca.trust_bundle_pem,
            # Required, not optional. A peer without a certificate the CA
            # signed does not reach a handler at all.
            require_client_auth=True,
        )

    def _enrollment_credentials(self) -> grpc.ServerCredentials:
        return grpc.ssl_server_credentials(
            [(self._server_key, self._server_cert)],
            root_certificates=None,
            require_client_auth=False,
        )

    @property
    def trust_bundle_pem(self) -> bytes:
        """What an installer ships to a node so it can verify this server."""
        return self.ca.trust_bundle_pem

    @property
    def trust_bundle_pin(self) -> str:
        """And the pin that goes with it."""
        return self.ca.trust_bundle_pin

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start both listeners."""
        await self.start_session()
        await self.start_enrollment()

    async def start_session(self) -> None:
        if self._session is not None:
            return
        server = grpc.aio.server(options=keepalive.server_options())
        pb_grpc.add_NodeSessionServicer_to_server(
            NodeSessionServicer(self.ledger, self.hub), server
        )
        # The Enrollment service is on *both* listeners, and each half only
        # works on one of them. §3.2 requires renewal over the already
        # authenticated channel, and this is that channel — the bootstrap
        # listener requests no client certificate at all, so `Renew` there can
        # never find a peer identity and refuses. `Enroll` here is refused for
        # the mirror-image reason: a connection that already holds a node
        # certificate has an identity, and minting a second one is the k0s
        # failure the plan calls out by name.
        pb_grpc.add_EnrollmentServicer_to_server(
            EnrollmentServicer(self.ca, self.ledger, self.hub), server
        )
        self._session_port = server.add_secure_port(
            f"{self.host}:{self._session_port}", self._session_credentials()
        )
        await server.start()
        self._session = server
        logger.info("agent session listener on %s:%s", self.host, self._session_port)

    async def start_enrollment(self) -> None:
        if self._enrollment is not None:
            return
        server = grpc.aio.server(options=keepalive.server_options())
        pb_grpc.add_EnrollmentServicer_to_server(
            EnrollmentServicer(self.ca, self.ledger, self.hub), server
        )
        self._enrollment_port = server.add_secure_port(
            f"{self.host}:{self._enrollment_port}", self._enrollment_credentials()
        )
        await server.start()
        self._enrollment = server
        logger.info(
            "agent enrollment listener on %s:%s", self.host, self._enrollment_port
        )

    async def stop_enrollment(self, grace: float = 1.0) -> None:
        """Close the bootstrap window without disturbing any session."""
        server, self._enrollment = self._enrollment, None
        if server is not None:
            await server.stop(grace)

    async def stop(self, grace: float = 1.0) -> None:
        """Stop both listeners and report every in-flight command unknown.

        The hub is shut down *first*, on purpose. A gracefully stopping gRPC
        server returns ``UNAVAILABLE`` to calls in flight — the same status a
        vanished node produces — so nothing is allowed to read a status here.
        Every waiter is resolved explicitly as ``unreachable, shutting down``
        before the transport is touched.
        """
        await self.hub.shutdown()
        await self.stop_enrollment(grace)
        server, self._session = self._session, None
        if server is not None:
            await server.stop(grace)

    # ── Addresses ────────────────────────────────────────────────────────

    @property
    def session_port(self) -> int:
        return self._session_port

    @property
    def enrollment_port(self) -> int:
        return self._enrollment_port

    def session_target(self, host: str = "127.0.0.1") -> str:
        return f"{host}:{self._session_port}"

    def enrollment_target(self, host: str = "127.0.0.1") -> str:
        return f"{host}:{self._enrollment_port}"

    # ── Bootstrap ────────────────────────────────────────────────────────

    @property
    def enrollment_open(self) -> bool:
        """Whether the bootstrap listener is running right now.

        The SSH installer asks so it can restore what it found: a control plane
        that keeps the token endpoint closed except during an install must not
        be left with it open by one, and a control plane that keeps it open
        must not have it closed underneath a concurrent enrolment.
        """
        return self._enrollment is not None

    def revoke_token(self, secret: str) -> bool:
        """End a token's life, redeemed or not. §3.1 step 8."""
        return self.ledger.revoke_token(secret)

    def mint_token(self, name: str, *, node_id: str = "") -> str:
        """A single-use enrollment token for one node, valid ten minutes.

        This is what the SSH installer carries to the node, alongside the
        trust bundle and its pin. Those three things are the entire handoff.

        ``node_id`` pins the identity the token will mint, for a caller that
        has already put the machine in the node registry.
        """
        return self.ledger.mint_token(name, node_id=node_id)
