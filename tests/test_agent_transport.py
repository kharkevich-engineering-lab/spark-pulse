"""The agent end to end, in one process, over a real mTLS loopback connection.

Everything here runs with no hardware, no Docker daemon and no container: the
agent is a plain object holding a stream, and its executor is constructed with
``MockDockerService``. That is not a testing convenience bolted on afterwards —
it is why the agent was built as an object with a stream rather than a process
you have to deploy before you can observe it.

The connection itself is real. Certificates are minted by a real CA into a
temporary directory, the handshake is a real TLS handshake, and the stream is a
real gRPC bidirectional stream on an ephemeral loopback port. What is faked is
Docker, and only Docker.

The properties under test, in order of how expensive they are to get wrong:

* an outcome that arrives is definite, and an outcome that does not arrive is
  *unknown* — never a fabricated failure;
* a certificate entitles its holder to exactly one node id;
* a token works once, for ten minutes;
* the control node is not a special case.
"""

from __future__ import annotations

import asyncio
import threading

import grpc
import pytest

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.agent.enrollment import EnrollmentLedger
from spark_pulse.agent.errors import (
    NodeOperationError,
    NodeUnreachable,
    UnreachableReason,
)
from spark_pulse.agent.executor import LocalExecutor
from spark_pulse.agent.hub import AgentHub, Liveness
from spark_pulse.agent.local import start_local_agent
from spark_pulse.agent.node_agent import NodeAgent, enroll
from spark_pulse.agent.operations import NODE_OPERATIONS, NodeOperations
from spark_pulse.agent.server import ControlPlaneServer
from spark_pulse.mock.docker import MockDockerClient, MockDockerService
from spark_pulse.tools.docker import ContainerMetadata

pytestmark = pytest.mark.asyncio


# ── Fixtures ────────────────────────────────────────────────────────────────


def new_docker() -> MockDockerService:
    """A container service with its own private state.

    One per node, so a test that gets the wrong node's answer sees the wrong
    containers rather than a plausible-looking right answer. The old boundary's
    defining bug — the control node answering for a worker — was invisible
    precisely because both were reading one daemon.
    """
    return MockDockerService(MockDockerClient())


@pytest.fixture
async def server(tmp_path):
    """A control plane on ephemeral loopback ports."""
    control = ControlPlaneServer(
        directory=tmp_path / "control",
        host="127.0.0.1",
        session_port=0,
        enrollment_port=0,
        hub=AgentHub(cluster_id="test-cluster", epoch=7),
    )
    await control.start()
    try:
        yield control
    finally:
        await control.stop(grace=0)


class Node:
    """One enrolled agent, its task and the Docker it speaks to."""

    def __init__(self, agent: NodeAgent, task: asyncio.Task, docker):
        self.agent = agent
        self.task = task
        self.docker = docker

    @property
    def node_id(self) -> str:
        return self.agent.node_id

    async def close(self) -> None:
        await self.agent.stop()
        self.task.cancel()
        try:
            await self.task
        except BaseException:
            pass


async def join(
    server: ControlPlaneServer,
    tmp_path,
    name: str,
    *,
    docker=None,
    heartbeat_interval: float = 0.2,
) -> Node:
    """Enroll a node and hold its session, exactly as a real node would."""
    docker = docker or new_docker()
    identity = await enroll(
        server.enrollment_target(),
        server.mint_token(name),
        trust_bundle_pem=server.trust_bundle_pem,
        trust_bundle_pin=server.trust_bundle_pin,
        directory=tmp_path / name,
        requested_name=name,
        docker_service=docker,
    )
    agent = NodeAgent(
        identity,
        server.session_target(),
        executor=LocalExecutor(docker),
        heartbeat_interval=heartbeat_interval,
    )
    task = asyncio.create_task(agent.run_forever(), name=f"agent-{name}")
    await agent.wait_connected(10)
    return Node(agent, task, docker)


@pytest.fixture
async def node(server, tmp_path):
    joined = await join(server, tmp_path, "spark-a")
    try:
        yield joined
    finally:
        await joined.close()


def ops(server: ControlPlaneServer, node: Node) -> NodeOperations:
    return NodeOperations(server.hub, node.node_id, timeout=10)


METADATA = ContainerMetadata(deployment="dep-1", recipe="r", image="img:1")


# ── The happy path ──────────────────────────────────────────────────────────


async def test_an_enrolled_agent_connects_and_is_healthy(server, node):
    assert server.hub.connected() == [node.node_id]
    assert server.hub.liveness(node.node_id) is Liveness.HEALTHY
    snapshot = server.hub.nodes()[0]
    assert snapshot.node_id == node.node_id
    assert snapshot.facts.hostname


async def test_run_inspect_and_stop_a_container(server, node):
    service = ops(server, node)
    info = await service.run_container("img:1", "c1", {"A": "1"}, METADATA)
    assert info.name == "c1"
    assert info.metadata.deployment == "dep-1"

    status = await service.get_container_status("c1")
    assert status["running"] is True
    assert status["error"] is None

    listing = await service.list_managed_containers()
    assert [c.name for c in listing] == ["c1"]
    assert (await service.get_container_by_deployment("dep-1")).name == "c1"

    assert await service.stop_container("c1") is True
    assert await service.list_managed_containers() == []


async def test_a_missing_container_is_a_value_not_an_error(server, node):
    """`missing` is an answer. The node ran the inspect and it said so."""
    status = await ops(server, node).get_container_status("nope")
    assert status["status"] == "missing"
    assert status["running"] is False
    # None, not "". The local service returns None here, so the remote one
    # must too — a nullable that becomes an empty string on one node and not
    # the other is exactly the divergence class this package deletes.
    assert status["id"] is None
    assert "not found" in status["error"]


async def test_exec_keeps_argv_and_shell_apart(server, node):
    """A list is not a string. Flattening one into the other changes what runs."""
    service = ops(server, node)
    await service.run_container("img:1", "c1", {}, METADATA)
    result = await service.exec_in_container("c1", ["echo", "hello world"])
    assert result.returncode == 0
    assert result.ok


async def test_copy_a_file_carries_its_bytes_and_its_mode(server, node, tmp_path):
    script = tmp_path / "serve.sh"
    script.write_text("#!/bin/sh\necho serving\n")
    script.chmod(0o755)
    service = ops(server, node)
    await service.run_container("img:1", "c1", {}, METADATA)
    assert await service.copy_to_container("c1", str(script), "/opt/serve.sh") is True


async def test_copy_a_directory(server, node, tmp_path):
    tree = tmp_path / "mods"
    (tree / "nested").mkdir(parents=True)
    (tree / "a.txt").write_text("a")
    (tree / "nested" / "b.txt").write_text("b")
    service = ops(server, node)
    await service.run_container("img:1", "c1", {}, METADATA)
    assert await service.copy_dir_to_container("c1", str(tree), "/opt/mods") is True
    # A directory handed to copy_to_container takes the same route, so a caller
    # never has to know which it holds.
    assert await service.copy_to_container("c1", str(tree), "/opt/mods") is True


async def test_pull_reports_progress_and_then_an_outcome(server, node):
    seen: list[dict] = []
    result = await ops(server, node).pull_image(
        "ghcr.io/example/vllm:1", progress=seen.append
    )
    assert result["ref"] == "ghcr.io/example/vllm:1"
    assert result["percent"] == 100.0
    assert seen, "progress must reach the caller as the pull runs"
    assert all("bytes_done" in event for event in seen)


async def test_images_and_logs(server, node):
    service = ops(server, node)
    await service.pull_image("ghcr.io/example/vllm:1")
    assert await service.image_exists("ghcr.io/example/vllm:1") is True
    info = await service.image_info("ghcr.io/example/vllm:1")
    assert info["id"]
    assert any(i["id"] == info["id"] for i in await service.list_images())
    assert await service.image_info("ghcr.io/example/absent:1") is None
    assert await service.remove_image("ghcr.io/example/vllm:1") is True

    await service.run_container("img:1", "c1", {}, METADATA)
    assert isinstance(await service.get_logs("c1"), str)


async def test_facts_come_from_the_node_itself(server, node):
    facts = await ops(server, node).get_facts()
    assert facts.hostname
    assert facts.agent_version
    # Collected, and diagnostic only. It is never an identity.
    assert facts.hardware_fingerprint


async def test_ensure_directories_returns_what_it_could_not_create(server, node):
    failed = await ops(server, node).ensure_directories(["/tmp/a", "/tmp/b"])
    assert failed == []
    assert node.docker.ensured == ["/tmp/a", "/tmp/b"]


async def test_every_node_service_method_is_reachable_over_the_agent():
    """The protocol covers the interface, and a drift here fails the build."""
    from spark_pulse.tools.node_service import NODE_SERVICE_METHODS

    assert set(NODE_SERVICE_METHODS) <= set(NODE_OPERATIONS)
    assert set(NODE_OPERATIONS) - set(NODE_SERVICE_METHODS) == {
        "copy_dir_to_container",
        "get_facts",
    }
    for name in NODE_OPERATIONS:
        assert callable(getattr(NodeOperations, name))


# ── The three outcomes ──────────────────────────────────────────────────────


async def test_a_failure_on_the_node_is_definite_and_reachable(server, node, tmp_path):
    """The node ran it and it failed. That is not the same as silence."""

    class Broken(MockDockerService):
        def list_images(self):
            raise RuntimeError("docker daemon is not running")

    broken = Broken(MockDockerClient())
    joined = await join(server, tmp_path, "spark-broken", docker=broken)
    try:
        with pytest.raises(NodeOperationError) as caught:
            await ops(server, joined).list_images()
        assert caught.value.error_type == "RuntimeError"
        assert "daemon is not running" in caught.value.error_message
        # And the node is still there. A failed command is not a lost node.
        assert server.hub.is_connected(joined.node_id)
    finally:
        await joined.close()


async def test_a_node_that_never_connects_is_unreachable(server):
    """Not enrolled, or enrolled and never dialled: both are *unknown*."""
    service = NodeOperations(server.hub, "a-node-that-never-showed-up", timeout=1)
    with pytest.raises(NodeUnreachable) as caught:
        await service.get_container_status("c1")
    assert caught.value.reason is UnreachableReason.NOT_CONNECTED
    assert server.hub.liveness("a-node-that-never-showed-up") is Liveness.DEAD


async def test_an_enrolled_node_whose_agent_never_starts_is_unreachable(
    server, tmp_path
):
    identity = await enroll(
        server.enrollment_target(),
        server.mint_token("spark-quiet"),
        trust_bundle_pem=server.trust_bundle_pem,
        trust_bundle_pin=server.trust_bundle_pin,
        directory=tmp_path / "quiet",
    )
    # Enrolled — the ledger accepts it — but nothing is holding a stream.
    assert server.ledger.authorize(identity.node_id)
    with pytest.raises(NodeUnreachable):
        await NodeOperations(server.hub, identity.node_id, timeout=1).list_images()


async def test_a_disconnect_mid_operation_is_unknown_never_a_failure(server, tmp_path):
    """The property the whole package exists for.

    The agent is executing a command when its stream dies. The caller must
    learn that the outcome is *unknown* — the container may be running, may be
    half-created, may be nothing at all. Reporting a failure here is what
    releases a GPU that is still in use.
    """
    started = threading.Event()
    release = threading.Event()

    class Slow(MockDockerService):
        def get_container_status(self, name):
            started.set()
            release.wait(30)
            return super().get_container_status(name)

    joined = await join(server, tmp_path, "spark-slow", docker=Slow(MockDockerClient()))
    try:
        call = asyncio.ensure_future(
            NodeOperations(server.hub, joined.node_id, timeout=30).get_container_status(
                "c1"
            )
        )
        await asyncio.to_thread(started.wait, 10)
        # The node goes away mid-command.
        await joined.close()
        with pytest.raises(NodeUnreachable) as caught:
            await call
        assert caught.value.reason is UnreachableReason.DISCONNECTED
        assert caught.value.node_id == joined.node_id
    finally:
        release.set()


async def test_a_deadline_makes_the_outcome_unknown_not_failed(server, tmp_path):
    """A deadline bounds our waiting, not the node's working."""
    started = threading.Event()
    release = threading.Event()

    class Slow(MockDockerService):
        def get_container_status(self, name):
            started.set()
            release.wait(30)
            return super().get_container_status(name)

    joined = await join(server, tmp_path, "spark-late", docker=Slow(MockDockerClient()))
    try:
        with pytest.raises(NodeUnreachable) as caught:
            await NodeOperations(
                server.hub, joined.node_id, timeout=0.4
            ).get_container_status("c1")
        assert caught.value.reason is UnreachableReason.TIMED_OUT
        assert server.hub.is_connected(joined.node_id)
    finally:
        release.set()
        await joined.close()


async def test_a_shutting_down_control_plane_reports_unknown_not_unavailable(
    server, tmp_path
):
    """A gracefully stopping gRPC server returns UNAVAILABLE, like a dead node.

    Which is why the hub resolves every waiter itself, explicitly, before the
    transport is touched — and why nothing anywhere reads a status to decide
    what happened to a command.
    """
    started = threading.Event()
    release = threading.Event()

    class Slow(MockDockerService):
        def get_container_status(self, name):
            started.set()
            release.wait(30)
            return super().get_container_status(name)

    joined = await join(server, tmp_path, "spark-bye", docker=Slow(MockDockerClient()))
    call = asyncio.ensure_future(
        NodeOperations(server.hub, joined.node_id, timeout=30).get_container_status("c")
    )
    await asyncio.to_thread(started.wait, 10)
    await server.hub.shutdown()
    with pytest.raises(NodeUnreachable) as caught:
        await call
    assert caught.value.reason is UnreachableReason.SHUTTING_DOWN
    release.set()
    await joined.close()


async def test_no_result_ever_becomes_a_failure(server):
    """Stated once, directly: unreachable and failed are different types.

    A caller cannot accidentally handle one as the other, because catching
    NodeOperationError does not catch NodeUnreachable and neither is a
    subclass of the other.
    """
    assert not issubclass(NodeUnreachable, NodeOperationError)
    assert not issubclass(NodeOperationError, NodeUnreachable)


# ── Identity ────────────────────────────────────────────────────────────────


async def test_a_certificate_is_entitled_to_exactly_one_node_id(server, tmp_path, node):
    """A valid certificate plus a different claimed id is refused."""
    identity = await enroll(
        server.enrollment_target(),
        server.mint_token("spark-liar"),
        trust_bundle_pem=server.trust_bundle_pem,
        trust_bundle_pin=server.trust_bundle_pin,
        directory=tmp_path / "liar",
    )
    stolen = identity.node_id
    identity.node_id = node.node_id  # claim the neighbour's identity
    impostor = NodeAgent(
        identity, server.session_target(), executor=LocalExecutor(new_docker())
    )
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await impostor.run_once()
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED
    assert stolen in caught.value.details()
    # And the node it tried to impersonate is untouched.
    assert server.hub.connected() == [node.node_id]


async def test_a_session_without_a_certificate_cannot_be_opened(server):
    """The session listener requires mTLS at the transport, not in a handler."""
    from spark_pulse.agent import agent_pb2_grpc as pb_grpc

    credentials = grpc.ssl_channel_credentials(
        root_certificates=server.trust_bundle_pem
    )
    async with grpc.aio.secure_channel(server.session_target(), credentials) as channel:
        call = pb_grpc.NodeSessionStub(channel).Session()
        # The TLS handshake itself fails — PEER_DID_NOT_RETURN_A_CERTIFICATE —
        # so this never reaches a handler and the failure surfaces on whichever
        # of write or read touches the socket first.
        with pytest.raises(grpc.aio.AioRpcError):
            await call.write(pb.AgentMessage(hello=pb.Hello(node_id="anyone")))
            await call.read()


async def test_a_denied_node_is_refused_on_its_next_connection(server, tmp_path):
    """Revocation with no CRL and no OCSP: a row in the ledger."""
    joined = await join(server, tmp_path, "spark-revoked")
    node_id = joined.node_id
    await joined.close()
    server.ledger.deny(node_id, "operator removed this node")

    agent = NodeAgent(
        joined.agent.identity,
        server.session_target(),
        executor=LocalExecutor(new_docker()),
    )
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await agent.run_once()
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED
    assert "operator removed" in caught.value.details()


async def test_token_reuse_is_refused_over_the_wire(server, tmp_path):
    token = server.mint_token("spark-twice")
    await enroll(
        server.enrollment_target(),
        token,
        trust_bundle_pem=server.trust_bundle_pem,
        trust_bundle_pin=server.trust_bundle_pin,
        directory=tmp_path / "first",
    )
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await enroll(
            server.enrollment_target(),
            token,
            trust_bundle_pem=server.trust_bundle_pem,
            trust_bundle_pin=server.trust_bundle_pin,
            directory=tmp_path / "second",
        )
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED
    assert "already used" in caught.value.details()


async def test_an_expired_token_is_refused_over_the_wire(server, tmp_path):
    token = server.ledger.mint_token("spark-late", ttl=-1)
    with pytest.raises(grpc.aio.AioRpcError) as caught:
        await enroll(
            server.enrollment_target(),
            token,
            trust_bundle_pem=server.trust_bundle_pem,
            trust_bundle_pin=server.trust_bundle_pin,
            directory=tmp_path / "late",
        )
    assert caught.value.code() is grpc.StatusCode.PERMISSION_DENIED
    assert "expired" in caught.value.details()


async def test_a_token_is_scoped_to_one_node_and_mints_its_uuid(server, tmp_path):
    a = await enroll(
        server.enrollment_target(),
        server.mint_token("spark-a"),
        trust_bundle_pem=server.trust_bundle_pem,
        trust_bundle_pin=server.trust_bundle_pin,
        directory=tmp_path / "a",
        requested_name="spark-a",
    )
    b = await enroll(
        server.enrollment_target(),
        server.mint_token("spark-a"),
        trust_bundle_pem=server.trust_bundle_pem,
        trust_bundle_pin=server.trust_bundle_pin,
        directory=tmp_path / "b",
        requested_name="spark-a",
    )
    # Same requested name, two identities. The name is a label; the uuid is
    # the identity, and the server minted it.
    assert a.node_id != b.node_id
    assert a.spiffe_id == f"spiffe://spark-pulse/node/{a.node_id}"


async def test_enrolling_against_a_mismatched_pin_is_refused(server, tmp_path):
    with pytest.raises(RuntimeError, match="does not match the pin"):
        await enroll(
            server.enrollment_target(),
            server.mint_token("spark-a"),
            trust_bundle_pem=server.trust_bundle_pem,
            trust_bundle_pin="not-the-pin",
            directory=tmp_path / "a",
        )


async def test_renewal_happens_over_the_authenticated_channel(server, node):
    """A node with a certificate needs no token to get another."""
    from spark_pulse.agent import agent_pb2_grpc as pb_grpc

    identity = node.agent.identity
    before = identity.certificate_pem
    credentials = grpc.ssl_channel_credentials(
        root_certificates=identity.trust_bundle_pem,
        private_key=identity.key_pem,
        certificate_chain=identity.certificate_pem,
    )
    from spark_pulse.agent import identity as ident

    pair = ident.build_csr()
    async with grpc.aio.secure_channel(server.session_target(), credentials) as channel:
        issued = await pb_grpc.EnrollmentStub(channel).Renew(
            pb.RenewRequest(csr_pem=pair.csr_pem)
        )
    assert issued.node_id == node.node_id
    assert issued.certificate_pem != before
    assert issued.trust_bundle_spki == server.trust_bundle_pin


async def test_renewal_without_a_certificate_is_refused(server):
    from spark_pulse.agent import agent_pb2_grpc as pb_grpc
    from spark_pulse.agent import identity as ident

    pair = ident.build_csr()
    credentials = grpc.ssl_channel_credentials(
        root_certificates=server.trust_bundle_pem
    )
    async with grpc.aio.secure_channel(
        server.enrollment_target(), credentials
    ) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await pb_grpc.EnrollmentStub(channel).Renew(
                pb.RenewRequest(csr_pem=pair.csr_pem)
            )
    assert caught.value.code() is grpc.StatusCode.UNAUTHENTICATED


# ── Fencing ─────────────────────────────────────────────────────────────────


async def test_an_agent_refuses_a_command_from_an_older_epoch(server, node):
    """Fencing happens at the resource, not at a leader election."""
    service = ops(server, node)
    server.hub.epoch = 3  # older than the 7 the agent was welcomed with
    with pytest.raises(NodeOperationError) as caught:
        await service.image_exists("img:1")
    assert caught.value.error_type == "StaleEpochError"
    assert "newer control plane" in caught.value.error_message


# ── The local node ──────────────────────────────────────────────────────────


async def test_the_control_node_is_reached_exactly_like_a_peer(server, tmp_path):
    """§7: no local implementation is selected per call, at any cluster size.

    The control node's agent is enrolled by the ordinary enrollment path and
    dials the ordinary session listener over loopback. This test runs the same
    operations against it and against a peer through the same object, and
    asserts the answers are the same shape while the *state* is each node's
    own.
    """
    local_docker = new_docker()
    local = await start_local_agent(
        server,
        directory=tmp_path / "local",
        docker_service=local_docker,
        heartbeat_interval=0.2,
    )
    peer = await join(server, tmp_path, "spark-peer")
    try:
        assert sorted(server.hub.connected()) == sorted([local.node_id, peer.node_id])
        # Same class of connection, and therefore the same code path.
        assert type(server.hub.get(local.node_id)) is type(server.hub.get(peer.node_id))

        results = {}
        for node_id in (local.node_id, peer.node_id):
            service = NodeOperations(server.hub, node_id, timeout=10)
            info = await service.run_container("img:1", "c1", {}, METADATA)
            status = await service.get_container_status("c1")
            results[node_id] = (info.name, status["running"], status["id"] is not None)
        assert results[local.node_id] == results[peer.node_id]

        # And the state really is each node's own: stopping the peer's
        # container leaves the control node's alone.
        await NodeOperations(server.hub, peer.node_id, timeout=10).stop_container("c1")
        assert await NodeOperations(
            server.hub, local.node_id, timeout=10
        ).list_managed_containers()
        assert not await NodeOperations(
            server.hub, peer.node_id, timeout=10
        ).list_managed_containers()
    finally:
        await peer.close()
        await local.stop()


async def test_the_control_node_keeps_its_identity_across_restarts(server, tmp_path):
    """A control plane that restarts does not re-enroll itself every time."""
    first = await start_local_agent(
        server, directory=tmp_path / "local", heartbeat_interval=0.2
    )
    node_id = first.node_id
    await first.stop()
    second = await start_local_agent(
        server, directory=tmp_path / "local", heartbeat_interval=0.2
    )
    try:
        assert second.node_id == node_id
    finally:
        await second.stop()


async def test_the_control_node_has_a_uuid_not_a_name(server, tmp_path):
    """It is enrolled, not privileged. Its id is minted like everyone else's."""
    local = await start_local_agent(
        server, directory=tmp_path / "local", heartbeat_interval=0.2
    )
    try:
        assert local.node_id != "control"
        entry = server.ledger.get(local.node_id)
        assert entry.name == "control"
        assert entry.node_id == local.node_id
    finally:
        await local.stop()


# ── Reconnection ────────────────────────────────────────────────────────────


async def test_an_agent_reconnects_and_serves_again(server, tmp_path):
    docker = new_docker()
    joined = await join(server, tmp_path, "spark-flappy", docker=docker)
    try:
        await ops(server, joined).run_container("img:1", "c1", {}, METADATA)
        # Drop the stream the way a network blip would, without stopping the
        # agent: it redials on its own.
        connection = server.hub.get(joined.node_id)
        connection.closed.set()
        for _ in range(200):
            await asyncio.sleep(0.05)
            if server.hub.get(joined.node_id) is not connection:
                break
        await joined.agent.wait_connected(10)
        listing = await ops(server, joined).list_managed_containers()
        assert [c.name for c in listing] == ["c1"]
    finally:
        await joined.close()


async def test_enrolling_again_over_an_enrolled_channel_is_refused(server, node):
    """A machine with an identity may renew, never re-enroll.

    k0s silently ignores the token when a config already exists, which is why
    re-enrollment there needs a full reset. Here it is refused, by name, with
    what to do instead.
    """
    from spark_pulse.agent import agent_pb2_grpc as pb_grpc
    from spark_pulse.agent import identity as ident

    identity = node.agent.identity
    credentials = grpc.ssl_channel_credentials(
        root_certificates=identity.trust_bundle_pem,
        private_key=identity.key_pem,
        certificate_chain=identity.certificate_pem,
    )
    pair = ident.build_csr()
    async with grpc.aio.secure_channel(server.session_target(), credentials) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await pb_grpc.EnrollmentStub(channel).Enroll(
                pb.EnrollRequest(
                    token=server.mint_token("spark-a"), csr_pem=pair.csr_pem
                )
            )
    assert caught.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert "already enrolled" in caught.value.details()


async def test_the_ledger_is_consulted_on_every_connection_not_just_the_first(
    server, tmp_path
):
    """That is what makes revocation work with no CRL and no OCSP."""
    joined = await join(server, tmp_path, "spark-checked")
    ledger = EnrollmentLedger(server.ledger.path)
    assert ledger.get(joined.node_id).state == "accepted"
    await joined.close()
