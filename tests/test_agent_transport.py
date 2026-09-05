"""The **control plane's** half of the agent protocol, against a stub node.

What is under test here is the hub, the session servicer and the enrolment
ledger: the code that decides which node a certificate entitles you to be,
what happens to a command whose answer never arrives, and whether a revoked
node gets back in. The counterparty is `tests/agent_stub.py` — a fake node
that can misbehave on purpose.

**The agent itself is not tested here, and that is deliberate.** The agent is
one static Rust binary; `tests/test_agent_rust_interop.py` drives *that*,
as a binary, against a real control plane and a real Docker daemon. Everything
this file used to assert about containers, images, copies and pulls moved
there, where it runs against the implementation that actually ships instead of
against a Python one that did not.

The distinction is worth stating because it is exactly the one
`docs/transport-reexamined.md` §5.1 was written about. A second *agent* would
be a second implementation of the thing we install, and the two would drift.
A stub counterparty is not that: it is a fake for the far side of a protocol,
used to test the near side, and it is never installed anywhere. It also does
things no correct agent can — claim a neighbour's identity, accept a command
and never answer, vanish halfway through one — which is precisely what the
control plane's hardest paths need in order to be tested at all.
"""

from __future__ import annotations

import asyncio

import grpc
import pytest

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.agent import agent_pb2_grpc as pb_grpc
from spark_pulse.agent import identity as ident
from spark_pulse.agent.errors import (
    NodeOperationError,
    NodeUnreachable,
    UnreachableReason,
)
from spark_pulse.agent.hub import Liveness
from spark_pulse.agent.operations import NODE_OPERATIONS, NodeOperations
from spark_pulse.version import __version__
from spark_pulse.mock import agent_node as agent_stub

pytestmark = pytest.mark.asyncio


def ops(server, node, timeout: float = 10) -> NodeOperations:
    return NodeOperations(server.hub, node.node_id, timeout=timeout)


def never_answers(_command: pb.Command) -> None:
    """Accept the command and go quiet. The node is 'still working on it'."""
    return None


def fails_with(kind: str, message: str):
    def handler(command: pb.Command) -> pb.CommandResult:
        result = pb.CommandResult(command_id=command.command_id)
        result.failure.CopyFrom(pb.CommandFailure(type=kind, message=message))
        return result

    return handler


# ── The happy path ──────────────────────────────────────────────────────────


async def test_a_connected_node_is_healthy_and_carries_its_facts(
    agent_server, agent_node
):
    assert agent_server.hub.connected() == [agent_node.node_id]
    assert agent_server.hub.liveness(agent_node.node_id) is Liveness.HEALTHY

    snapshot = agent_server.hub.nodes()[0]
    assert snapshot.node_id == agent_node.node_id
    # Facts arrive on the Hello, before any command is sent — which is what
    # lets the control plane describe a node it has never asked anything.
    assert snapshot.facts.hostname == "stub-node"
    assert snapshot.agent_version == __version__


async def test_the_welcome_carries_the_cluster_and_the_epoch(agent_server, agent_node):
    assert agent_node.welcome is not None
    assert agent_node.welcome.cluster_id == agent_server.hub.cluster_id
    assert agent_node.welcome.epoch == agent_server.hub.epoch


async def test_a_command_reaches_the_node_and_its_answer_comes_back(
    agent_server, agent_node
):
    facts = await ops(agent_server, agent_node).get_facts()
    assert facts.hostname == "stub-node"
    assert [c.WhichOneof("op") for c in agent_node.commands] == ["get_facts"]


async def test_every_operation_the_protocol_carries_has_a_method(agent_server):
    """The surface, checked by name, so adding one to the proto and not to
    ``NodeOperations`` fails here rather than at a call site."""
    for name in NODE_OPERATIONS:
        assert callable(getattr(NodeOperations, name))


# ── The three outcomes ──────────────────────────────────────────────────────


async def test_a_failure_on_the_node_is_definite_and_reachable(
    agent_server, join_agent
):
    """The node ran it and it failed. That is not the same as silence."""
    node = await join_agent(
        "spark-broken",
        handler=fails_with("RuntimeError", "docker daemon is not running"),
    )
    with pytest.raises(NodeOperationError) as caught:
        await ops(agent_server, node).list_images()

    assert caught.value.error_type == "RuntimeError"
    assert "daemon is not running" in caught.value.error_message
    # And the node is still there. A failed command is not a lost node.
    assert agent_server.hub.is_connected(node.node_id)


async def test_a_node_that_never_connects_is_unreachable(agent_server):
    """Not enrolled, or enrolled and never dialled: both are *unknown*."""
    service = NodeOperations(agent_server.hub, "a-node-that-never-showed-up", timeout=1)
    with pytest.raises(NodeUnreachable) as caught:
        await service.get_container_status("c1")
    assert caught.value.reason is UnreachableReason.NOT_CONNECTED
    assert agent_server.hub.liveness("a-node-that-never-showed-up") is Liveness.DEAD


async def test_an_enrolled_node_whose_agent_never_starts_is_unreachable(
    agent_server, join_agent
):
    node = await join_agent("spark-quiet", connect=False)
    # Enrolled — the ledger accepts it — but nothing is holding a stream.
    assert agent_server.ledger.authorize(node.node_id)
    with pytest.raises(NodeUnreachable):
        await NodeOperations(agent_server.hub, node.node_id, timeout=1).list_images()


async def test_a_disconnect_mid_operation_is_unknown_never_a_failure(
    agent_server, join_agent
):
    """The property the whole package exists for.

    The node is executing a command when its stream dies. The caller must
    learn that the outcome is *unknown* — the container may be running, may be
    half-created, may be nothing at all. Reporting a failure here is what
    releases a GPU that is still in use.
    """
    node = await join_agent("spark-slow", handler=never_answers)
    call = asyncio.ensure_future(
        NodeOperations(agent_server.hub, node.node_id, timeout=30).get_container_status(
            "c1"
        )
    )
    while not node.commands:
        await asyncio.sleep(0.01)

    await node.close()  # the node goes away mid-command

    with pytest.raises(NodeUnreachable) as caught:
        await call
    assert caught.value.reason is UnreachableReason.DISCONNECTED
    assert caught.value.node_id == node.node_id


async def test_a_deadline_makes_the_outcome_unknown_not_failed(
    agent_server, join_agent
):
    """A deadline bounds our waiting, not the node's working."""
    node = await join_agent("spark-late", handler=never_answers)
    with pytest.raises(NodeUnreachable) as caught:
        await NodeOperations(
            agent_server.hub, node.node_id, timeout=0.4
        ).get_container_status("c1")

    assert caught.value.reason is UnreachableReason.TIMED_OUT
    # Still connected: the node is busy, not gone.
    assert agent_server.hub.is_connected(node.node_id)


async def test_a_cancelled_command_is_sent_to_the_node(agent_server, join_agent):
    """A deadline cancels on the node too, or it goes on working for nobody."""
    node = await join_agent("spark-cancel", handler=never_answers)
    with pytest.raises(NodeUnreachable):
        await NodeOperations(
            agent_server.hub, node.node_id, timeout=0.3
        ).get_container_status("c1")

    for _ in range(100):
        if node.cancels:
            break
        await asyncio.sleep(0.01)
    assert node.cancels == [node.commands[0].command_id]


async def test_a_shutting_down_control_plane_reports_unknown_not_unavailable(
    agent_server, join_agent
):
    """A gracefully stopping gRPC server returns UNAVAILABLE, like a dead node.

    Which is why the hub resolves every waiter itself, explicitly, before the
    transport is touched — and why nothing anywhere reads a status to decide
    what happened to a command.
    """
    node = await join_agent("spark-bye", handler=never_answers)
    call = asyncio.ensure_future(
        NodeOperations(agent_server.hub, node.node_id, timeout=30).get_container_status(
            "c"
        )
    )
    while not node.commands:
        await asyncio.sleep(0.01)

    await agent_server.hub.shutdown()

    with pytest.raises(NodeUnreachable) as caught:
        await call
    assert caught.value.reason is UnreachableReason.SHUTTING_DOWN


async def test_no_result_ever_becomes_a_failure():
    """Stated once, directly: unreachable and failed are different types.

    A caller cannot accidentally handle one as the other, because catching
    NodeOperationError does not catch NodeUnreachable and neither is a
    subclass of the other.
    """
    assert not issubclass(NodeUnreachable, NodeOperationError)
    assert not issubclass(NodeOperationError, NodeUnreachable)


async def test_progress_is_delivered_but_is_never_an_outcome(agent_server, join_agent):
    """A pull reports as it goes; the command is finished only by its result."""
    seen: list[dict] = []
    finished = asyncio.Event()

    node = await join_agent("spark-pull", handler=never_answers)
    call = asyncio.ensure_future(
        NodeOperations(agent_server.hub, node.node_id, timeout=30).pull_image(
            "img:1", progress=lambda event: (seen.append(event), finished.set())[0]
        )
    )
    while not node.commands:
        await asyncio.sleep(0.01)
    command_id = node.commands[0].command_id
    node.report_progress(command_id, ref="img:1", status="Downloading", percent=42.0)
    await asyncio.wait_for(finished.wait(), 5)

    assert seen[0]["percent"] == 42.0
    assert not call.done(), "a progress event must not complete the command"

    result = pb.CommandResult(command_id=command_id)
    result.pull.CopyFrom(pb.PullOutcome(ref="img:1", percent=100.0))
    node.send(pb.AgentMessage(result=result))
    assert (await call)["percent"] == 100.0


# ── Identity ────────────────────────────────────────────────────────────────


async def test_a_certificate_is_entitled_to_exactly_one_node_id(
    agent_server, agent_node, join_agent
):
    """A valid certificate plus a different claimed id is refused.

    The heart of it: a compromised node must not be able to answer for its
    neighbour. Only a stub can attempt this — the agent claims the id in its
    own certificate because it has no other one to claim.
    """
    impostor = await join_agent(
        "spark-liar", claim_node_id=agent_node.node_id, connect=False
    )
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await impostor.connect(timeout=10)

    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED
    assert impostor.node_id in caught.value.details()
    # And the node it tried to impersonate is untouched.
    assert agent_server.hub.connected() == [agent_node.node_id]


async def test_a_session_without_a_certificate_cannot_be_opened(agent_server):
    """The session listener requires mTLS at the transport, not in a handler."""
    credentials = grpc.ssl_channel_credentials(
        root_certificates=agent_server.trust_bundle_pem
    )
    async with grpc.aio.secure_channel(
        agent_server.session_target(), credentials
    ) as channel:
        call = pb_grpc.NodeSessionStub(channel).Session()
        # The TLS handshake itself fails — PEER_DID_NOT_RETURN_A_CERTIFICATE —
        # so this never reaches a handler and the failure surfaces on whichever
        # of write or read touches the socket first.
        with pytest.raises(grpc.aio.AioRpcError):
            await call.write(pb.AgentMessage(hello=pb.Hello(node_id="anyone")))
            await call.read()


async def test_a_denied_node_is_refused_on_its_next_connection(
    agent_server, join_agent
):
    """Revocation with no CRL and no OCSP: a row in the ledger."""
    node = await join_agent("spark-revoked")
    await node.close()
    agent_server.ledger.deny(node.node_id, "operator removed this node")

    again = agent_stub.AgentStub(node.identity, agent_server.session_target())
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await again.connect(timeout=10)
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED
    assert "operator removed" in caught.value.details()
    await again.close()


async def test_the_ledger_is_consulted_on_every_connection_not_just_the_first(
    agent_server, join_agent
):
    node = await join_agent("spark-later")
    assert agent_server.hub.is_connected(node.node_id)
    await node.close()

    agent_server.ledger.deny(node.node_id, "removed between sessions")
    again = agent_stub.AgentStub(node.identity, agent_server.session_target())
    with pytest.raises(grpc.aio.AioRpcError):
        await again.connect(timeout=10)
    await again.close()


# ── Enrolment ───────────────────────────────────────────────────────────────


async def test_token_reuse_is_refused_over_the_wire(agent_server, tmp_path):
    token = agent_server.mint_token("spark-twice")
    await agent_stub.enroll(
        agent_server, "spark-twice", tmp_path / "first", token=token
    )
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await agent_stub.enroll(
            agent_server, "spark-twice", tmp_path / "second", token=token
        )
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED
    assert "already used" in caught.value.details()


async def test_an_expired_token_is_refused_over_the_wire(agent_server, tmp_path):
    token = agent_server.ledger.mint_token("spark-late", ttl=-1)
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await agent_stub.enroll(
            agent_server, "spark-late", tmp_path / "late", token=token
        )
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED
    assert "expired" in caught.value.details()


async def test_a_token_is_scoped_to_one_node_and_mints_its_uuid(agent_server, tmp_path):
    a = await agent_stub.enroll(agent_server, "spark-a", tmp_path / "a")
    b = await agent_stub.enroll(agent_server, "spark-a", tmp_path / "b")
    # Same requested name, two identities. The name is a label; the uuid is
    # the identity, and the server minted it.
    assert a.node_id != b.node_id
    assert a.spiffe_id == f"spiffe://spark-pulse/node/{a.node_id}"


async def test_enrolling_against_a_mismatched_pin_is_refused(agent_server, tmp_path):
    with pytest.raises(RuntimeError, match="does not match the pin"):
        await agent_stub.enroll(
            agent_server, "spark-a", tmp_path / "a", pin="not-the-pin"
        )


async def test_renewal_happens_over_the_authenticated_channel(agent_server, agent_node):
    """A node with a certificate needs no token to get another."""
    identity = agent_node.identity
    before = identity.certificate_pem
    credentials = grpc.ssl_channel_credentials(
        root_certificates=identity.trust_bundle_pem,
        private_key=identity.key_pem,
        certificate_chain=identity.certificate_pem,
    )
    pair = ident.build_csr()
    async with grpc.aio.secure_channel(
        agent_server.session_target(), credentials
    ) as channel:
        issued = await pb_grpc.EnrollmentStub(channel).Renew(
            pb.RenewRequest(csr_pem=pair.csr_pem)
        )
    assert issued.node_id == agent_node.node_id
    assert issued.certificate_pem != before
    assert issued.trust_bundle_spki == agent_server.trust_bundle_pin


async def test_renewal_without_a_certificate_is_refused(agent_server):
    pair = ident.build_csr()
    credentials = grpc.ssl_channel_credentials(
        root_certificates=agent_server.trust_bundle_pem
    )
    async with grpc.aio.secure_channel(
        agent_server.enrollment_target(), credentials
    ) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await pb_grpc.EnrollmentStub(channel).Renew(
                pb.RenewRequest(csr_pem=pair.csr_pem)
            )
    assert caught.value.code() is grpc.StatusCode.UNAUTHENTICATED


# ── Reconnection ────────────────────────────────────────────────────────────


async def test_a_node_that_reconnects_replaces_its_own_connection(
    agent_server, join_agent
):
    """A network blip must not leave two connections claiming one node."""
    node = await join_agent("spark-flappy")
    first = agent_server.hub.get(node.node_id)
    await node.close()

    again = agent_stub.AgentStub(node.identity, agent_server.session_target())
    await again.connect(timeout=10)
    try:
        assert agent_server.hub.connected() == [node.node_id]
        assert agent_server.hub.get(node.node_id) is not first
        # And it serves again.
        assert (await ops(agent_server, again).get_facts()).hostname == "stub-node"
    finally:
        await again.close()
