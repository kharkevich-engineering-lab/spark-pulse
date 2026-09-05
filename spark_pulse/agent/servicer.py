"""The control plane's side of the wire.

Two servicers, on one port, with deliberately different authentication:

* :class:`EnrollmentServicer` — ``Enroll`` is reached without a client
  certificate, because a node bootstrapping does not have one; it is
  authenticated by a single-use enrollment token. ``Renew`` is on the same
  service but demands the mutually authenticated channel the agent already
  holds, so a renewal never re-opens the token path.
* :class:`NodeSessionServicer` — mTLS only, checked in the handler rather than
  trusted from the listener. The listener accepts connections without a client
  certificate so that enrollment can reach it, which means "the transport
  required a certificate" is not a thing this code may assume. It checks.

The authorisation chain for a session, in order: a peer certificate exists and
the CA signed it (the TLS stack); it carries exactly one SPIFFE URI SAN in our
trust domain and that SAN names a *node* rather than the control plane; the
``Hello`` claims that same node id; and the enrollment ledger still accepts
that node with that key. A failure at any step is ``UNAUTHENTICATED`` or
``PERMISSION_DENIED`` — statuses are for *connection* outcomes, which is the
one thing they are allowed to carry. Command outcomes are payload, always.
"""

from __future__ import annotations

import asyncio
import logging
import time

import grpc

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.agent import agent_pb2_grpc as pb_grpc
from spark_pulse.agent import identity as ident
from spark_pulse.agent.enrollment import EnrollmentLedger
from spark_pulse.agent.errors import EnrollmentRejected
from spark_pulse.agent.facts import facts_dict
from spark_pulse.agent.hub import AgentConnection, AgentHub

logger = logging.getLogger(__name__)

__all__ = [
    "EnrollmentServicer",
    "NodeSessionServicer",
    "peer_certificate",
    "peer_node_id",
]


def peer_certificate(context: grpc.aio.ServicerContext) -> bytes | None:
    """The PEM certificate the peer presented, or None.

    ``auth_context`` only carries ``x509_pem_cert`` once the TLS stack has
    validated the chain against the CA, so its presence *is* the chain check.
    Its absence is a peer that presented nothing — which the listener permits,
    for enrollment's sake, and which every other handler refuses.
    """
    try:
        auth = context.auth_context()
    except Exception:  # pragma: no cover — only on a closed context
        return None
    values = auth.get("x509_pem_cert") or []
    return values[0] if values else None


def peer_node_id(context: grpc.aio.ServicerContext) -> str | None:
    """The node uuid the peer's certificate entitles it to, or None.

    Reads the SPIFFE URI SAN and nothing else. Not the common name, not the
    address the connection came from: identity is logical, so a node that
    moves to the other link or gets a new lease is the same node, and a
    process that happens to run on a node's address is not that node.
    """
    cert = peer_certificate(context)
    return ident.peer_node_id(cert) if cert else None


class EnrollmentServicer(pb_grpc.EnrollmentServicer):
    """Mints identities, and renews them over the authenticated channel."""

    def __init__(
        self,
        ca: ident.CertificateAuthority,
        ledger: EnrollmentLedger,
        hub: AgentHub,
    ):
        self.ca = ca
        self.ledger = ledger
        self.hub = hub

    def _identity(self, node_id: str, issued: ident.IssuedCertificate) -> pb.Identity:
        return pb.Identity(
            node_id=node_id,
            certificate_pem=issued.certificate_pem,
            trust_bundle_pem=self.ca.trust_bundle_pem,
            trust_bundle_spki=self.ca.trust_bundle_pin,
            not_before_unix=int(issued.not_before.timestamp()),
            not_after_unix=int(issued.not_after.timestamp()),
            cluster_id=self.hub.cluster_id,
            epoch=self.hub.epoch,
            spiffe_id=issued.spiffe_id,
        )

    async def Enroll(  # noqa: N802 — the generated name
        self, request: pb.EnrollRequest, context: grpc.aio.ServicerContext
    ) -> pb.Identity:
        existing = peer_node_id(context)
        if existing:
            # This connection already carries a node identity, so enrolling
            # would mint a second uuid for one machine and orphan the first.
            # k0s silently ignores the token in this situation, which is why
            # re-enrollment there needs a full reset (§3.1). We refuse and say
            # what to do instead.
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"this connection is already enrolled as {existing}; use Renew "
                "to obtain a new certificate, or remove the node first",
            )
            raise AssertionError("unreachable")  # pragma: no cover
        try:
            node_id = self.ledger.redeem_token(request.token)
        except EnrollmentRejected as exc:
            # The message says which of unknown/expired/used it was, because
            # an operator debugging a failed install needs to know whether to
            # wait or to mint another.
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, str(exc))
            raise AssertionError("unreachable")  # pragma: no cover
        try:
            issued = self.ca.issue_node_certificate(request.csr_pem, node_id)
        except Exception as exc:
            self.ledger.remove(node_id)
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"bad CSR: {exc}")
            raise AssertionError("unreachable")  # pragma: no cover
        self.ledger.record_issue(
            node_id,
            public_key_fingerprint=ident.public_key_fingerprint(issued.certificate_pem),
            not_after=issued.not_after.timestamp(),
            facts=facts_dict(request.facts),
        )
        logger.info("enrolled node %s as %s", request.requested_name, node_id)
        return self._identity(node_id, issued)

    async def Renew(  # noqa: N802 — the generated name
        self, request: pb.RenewRequest, context: grpc.aio.ServicerContext
    ) -> pb.Identity:
        node_id = peer_node_id(context)
        if not node_id:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "renewal requires the authenticated channel",
            )
            raise AssertionError("unreachable")  # pragma: no cover
        verdict = self.ledger.authorize(node_id, facts=facts_dict(request.facts))
        if not verdict:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, verdict.reason)
            raise AssertionError("unreachable")  # pragma: no cover
        issued = self.ca.issue_node_certificate(request.csr_pem, node_id)
        # The fingerprint is *replaced*, not compared: a renewal is exactly the
        # case where a node legitimately presents a new key, and it does so
        # over a channel it has already authenticated on the old one.
        self.ledger.record_issue(
            node_id,
            public_key_fingerprint=ident.public_key_fingerprint(issued.certificate_pem),
            not_after=issued.not_after.timestamp(),
            facts=facts_dict(request.facts),
        )
        logger.info("renewed certificate for node %s", node_id)
        return self._identity(node_id, issued)


class NodeSessionServicer(pb_grpc.NodeSessionServicer):
    """One long-lived bidirectional stream per node."""

    def __init__(self, ledger: EnrollmentLedger, hub: AgentHub):
        self.ledger = ledger
        self.hub = hub

    async def Session(  # noqa: N802 — the generated name
        self, request_iterator, context: grpc.aio.ServicerContext
    ):
        cert = peer_certificate(context)
        if cert is None:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "a session requires a client certificate",
            )
            return
        certified = ident.peer_node_id(cert)
        if not certified:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "the certificate carries no node identity",
            )
            return

        try:
            first = await request_iterator.__anext__()
        except StopAsyncIteration:
            return
        if first.WhichOneof("body") != "hello":
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "the first message on a session must be a Hello",
            )
            return
        hello = first.hello
        if hello.node_id != certified:
            # The heart of it: a certificate is an entitlement to *one*
            # identity. A node that holds a valid certificate and claims a
            # different id is refused, so a compromised agent cannot answer
            # for its neighbour.
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                f"certificate is for {certified}, Hello claims {hello.node_id}",
            )
            return
        verdict = self.ledger.authorize(
            certified,
            public_key_fingerprint=ident.public_key_fingerprint(cert),
            facts=facts_dict(hello.facts),
        )
        if not verdict:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, verdict.reason)
            return

        connection = AgentConnection(
            certified, agent_version=hello.agent_version, facts=hello.facts
        )
        self.hub.attach(connection)
        logger.info("agent %s connected", certified)
        reader = asyncio.create_task(
            self._read(request_iterator, connection),
            name=f"agent-reader-{certified}",
        )
        try:
            yield pb.ControlMessage(
                welcome=pb.Welcome(
                    cluster_id=self.hub.cluster_id,
                    epoch=self.hub.epoch,
                    server_time_unix=int(time.time()),
                )
            )
            async for message in self._drain(connection):
                yield message
        finally:
            reader.cancel()
            self.hub.detach(connection)
            logger.info("agent %s disconnected", certified)

    async def _drain(self, connection: AgentConnection):
        """Yield queued control messages until the stream closes."""
        closed = asyncio.ensure_future(connection.closed.wait())
        try:
            while True:
                pending = asyncio.ensure_future(connection.outbox.get())
                done, _ = await asyncio.wait(
                    {pending, closed}, return_when=asyncio.FIRST_COMPLETED
                )
                if pending in done:
                    yield pending.result()
                    continue
                pending.cancel()
                return
        finally:
            closed.cancel()

    async def _read(self, request_iterator, connection: AgentConnection) -> None:
        """Consume the agent's half of the stream."""
        try:
            async for message in request_iterator:
                body = message.WhichOneof("body")
                if body == "heartbeat":
                    connection.touch(message.heartbeat.facts)
                    self.ledger.note_seen(connection.node_id)
                elif body == "result":
                    connection.touch()
                    connection.deliver(message.result)
                elif body == "progress":
                    connection.touch()
                    connection.deliver_progress(message.progress)
                elif body == "hello":
                    # A second Hello on one stream. Ignored rather than
                    # honoured: identity was settled by the certificate.
                    logger.debug("ignoring a repeat Hello from %s", connection.node_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("stream from %s ended: %s", connection.node_id, exc)
        finally:
            # Whatever ended the read half ends the write half too, so a node
            # that goes away does not leave a writer waiting on a queue that
            # nothing will ever fill.
            connection.closed.set()
