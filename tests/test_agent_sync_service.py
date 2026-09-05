"""The synchronous face of the agent, over a real loopback agent.

Nothing here is stubbed between the call and the container: a test calls
:class:`AgentNodeService` from a worker thread exactly as a FastAPI sync
endpoint does, the call crosses to the event loop, goes out over real mTLS
gRPC to a real enrolled agent, and lands in ``MockDockerService``. Only Docker
is fake — which is the same boundary ``test_agent_transport`` draws, on
purpose, so the two files cannot disagree about where reality stops.

What is actually under test is the bridge's three obligations:

* it returns what ``DockerService`` returns, unchanged;
* it keeps ``NodeUnreachable`` and ``NodeOperationError`` apart, because
  releasing a GPU on the first one is how two gangs end up on one device;
* it refuses, loudly, to be called from the loop thread — where it would
  deadlock silently.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from spark_pulse.agent.errors import NodeUnreachable, NodeOperationError
from spark_pulse.agent.hub import Liveness
from spark_pulse.agent.runtime import ControlPlaneRuntime, current, use
from spark_pulse.agent.sync_service import (
    CONTRACT_EXCEPTIONS,
    RESULT_MARGIN,
    AgentNodeService,
)
from spark_pulse.mock.docker import MockDockerClient, MockDockerService
from spark_pulse.tools.docker import ContainerMetadata, PullCancelled
from spark_pulse.tools.node_service import (
    NODE_SERVICE_METHODS,
    Node,
    control_node,
    peer_node,
)

pytestmark = pytest.mark.asyncio

METADATA = ContainerMetadata(deployment="dep-1", recipe="r", image="img:1")


def service_for_node(agent_server, agent_node, **kwargs) -> AgentNodeService:
    return AgentNodeService(
        agent_server.hub,
        agent_node.node_id,
        asyncio.get_running_loop(),
        timeout=10,
        **kwargs,
    )


async def call(service, method: str, *args, **kwargs):
    """Invoke a sync service method from a worker thread, as FastAPI does."""
    return await asyncio.to_thread(getattr(service, method), *args, **kwargs)


# ── The interface ───────────────────────────────────────────────────────────


async def test_it_implements_every_node_service_method(agent_server, agent_node):
    """The ratchet: a method added to the interface must land here too."""
    service = service_for_node(agent_server, agent_node)
    missing = [
        m for m in NODE_SERVICE_METHODS if not callable(getattr(service, m, None))
    ]
    assert missing == []


async def test_a_container_runs_inspects_and_stops(agent_server, agent_node):
    service = service_for_node(agent_server, agent_node)
    info = await call(service, "run_container", "img:1", "c1", {"A": "b"}, METADATA)
    assert info.name == "c1"

    status = await call(service, "get_container_status", "c1")
    assert status["running"] is True

    listing = await call(service, "list_managed_containers")
    assert [c.name for c in listing] == ["c1"]

    assert await call(service, "stop_container", "c1") is True
    assert (await call(service, "get_container_status", "c1"))["status"] == "missing"


async def test_it_answers_for_its_own_node_and_no_other(agent_server, join_agent):
    """Two nodes, two dockers: an answer for the wrong node is visibly wrong."""
    a = await join_agent("spark-a")
    b = await join_agent("spark-b")
    try:
        loop = asyncio.get_running_loop()
        first = AgentNodeService(agent_server.hub, a.node_id, loop, timeout=10)
        second = AgentNodeService(agent_server.hub, b.node_id, loop, timeout=10)
        await call(first, "run_container", "img:1", "only-on-a", {}, METADATA)

        assert [c.name for c in await call(first, "list_managed_containers")] == [
            "only-on-a"
        ]
        assert await call(second, "list_managed_containers") == []
    finally:
        await a.close()
        await b.close()


async def test_exec_accepts_a_container_object_the_way_docker_service_does(
    agent_server, agent_node
):
    """``native_runtime`` passes the ContainerInfo it just got back."""
    service = service_for_node(agent_server, agent_node)
    info = await call(service, "run_container", "img:1", "c1", {}, METADATA)
    result = await call(service, "exec_in_container", info, ["echo", "hi"])
    assert result.returncode == 0


# ── The two failure kinds, kept apart ───────────────────────────────────────


async def test_a_failed_operation_on_a_reachable_node_is_an_operation_error(
    agent_server, join_agent
):
    """The node answered, and the answer was "no". Definite, and still here."""

    class Broken(MockDockerService):
        def list_images(self):
            raise RuntimeError("the docker daemon is not running")

    joined = await join_agent("spark-broken", docker=Broken(MockDockerClient()))
    try:
        service = AgentNodeService(
            agent_server.hub, joined.node_id, asyncio.get_running_loop(), timeout=10
        )
        with pytest.raises(NodeOperationError) as caught:
            await call(service, "list_images")
        assert caught.value.error_type == "RuntimeError"
        assert not isinstance(caught.value, NodeUnreachable)
        # A failed command is not a lost node.
        assert agent_server.hub.is_connected(joined.node_id)
    finally:
        await joined.close()


async def test_a_node_that_never_connected_is_unreachable_not_failed(agent_server):
    """The distinction the whole transport exists to keep."""
    service = AgentNodeService(
        agent_server.hub,
        "node-that-is-not-there",
        asyncio.get_running_loop(),
        timeout=2,
    )
    with pytest.raises(NodeUnreachable) as caught:
        await call(service, "get_container_status", "anything")
    assert not isinstance(caught.value, NodeOperationError)


async def test_a_dropped_connection_is_unreachable_not_missing(
    agent_server, join_agent
):
    """A container on a node we lost is *unknown*, never reported as gone."""
    joined = await join_agent("spark-a")
    service = AgentNodeService(
        agent_server.hub, joined.node_id, asyncio.get_running_loop(), timeout=2
    )
    await call(service, "run_container", "img:1", "c1", {}, METADATA)
    await joined.close()
    await asyncio.sleep(0.1)

    with pytest.raises(NodeUnreachable):
        await call(service, "get_container_status", "c1")


# ── Contract exceptions ─────────────────────────────────────────────────────


class SlowPull(MockDockerService):
    """A Docker whose pull takes long enough to be cancelled, and honours it.

    It polls ``cancel`` and raises ``PullCancelled`` because that is what
    ``DockerService.pull_image`` does; Docker is the declared fake here, and
    standing in for it means standing in for its contract too.
    """

    def pull_image(self, ref, progress=None, interval=0.0, cancel=None, **kwargs):
        for _ in range(400):
            if cancel is not None and cancel():
                raise PullCancelled(f"pull of {ref} cancelled")
            time.sleep(0.01)
        return super().pull_image(ref, progress, interval=interval, cancel=cancel)


async def test_a_cancelled_pull_travels_to_the_node_and_comes_back_as_pull_cancelled(
    agent_server, join_agent
):
    """``native_runtime`` and ``images`` catch ``PullCancelled`` by type.

    Two things at once, because they are one property: the Cancel reaches the
    node (or the pull never ends), and the node's answer arrives as
    ``PullCancelled`` rather than a generic failure — a remote pull that came
    back as a plain ``NodeOperationError`` would slip past all three handlers
    and be recorded as a deployment failure instead of a teardown.
    """
    joined = await join_agent("spark-slow", docker=SlowPull(MockDockerClient()))
    try:
        service = AgentNodeService(
            agent_server.hub, joined.node_id, asyncio.get_running_loop(), timeout=30
        )
        cancelled = threading.Event()

        async def trip():
            await asyncio.sleep(0.3)
            cancelled.set()

        asyncio.create_task(trip())
        with pytest.raises(PullCancelled):
            await call(service, "pull_image", "slow:1", None, 0.05, cancelled.is_set)
    finally:
        await joined.close()


async def test_a_pull_failure_the_table_does_not_name_stays_an_agent_error(
    agent_server, join_agent
):
    """The table is a contract, not a general-purpose exception tunnel."""

    class Refuses(MockDockerService):
        def pull_image(self, ref, *args, **kwargs):
            raise ValueError("no such registry")

    joined = await join_agent("spark-refuses", docker=Refuses(MockDockerClient()))
    try:
        service = AgentNodeService(
            agent_server.hub, joined.node_id, asyncio.get_running_loop(), timeout=10
        )
        with pytest.raises(NodeOperationError) as caught:
            await call(service, "pull_image", "img:1")
        assert caught.value.error_type == "ValueError"
    finally:
        await joined.close()


async def test_the_translation_table_is_small_and_explicit():
    """It maps contract failures only; everything else stays an agent error."""
    assert set(CONTRACT_EXCEPTIONS) == {"PullCancelled", "PullStalled"}


# ── The deadlock guard ──────────────────────────────────────────────────────


async def test_calling_from_the_loop_thread_raises_instead_of_hanging(
    agent_server, agent_node
):
    """The failure this guard replaces is a hang with no traceback."""
    service = service_for_node(agent_server, agent_node)
    with pytest.raises(RuntimeError) as caught:
        service.get_container_status("c1")  # no to_thread: on the loop thread
    message = str(caught.value)
    assert "deadlock" in message
    assert "NodeOperations" in message


async def test_the_guard_does_not_leave_a_coroutine_un_awaited(
    agent_server, agent_node, recwarn
):
    service = service_for_node(agent_server, agent_node)
    with pytest.raises(RuntimeError):
        service.image_exists("img:1")
    assert not [w for w in recwarn if "never awaited" in str(w.message)]


async def test_the_thread_side_deadline_is_longer_than_the_command_deadline():
    """It bounds the *loop*, not the node, so it must never fire first."""
    assert RESULT_MARGIN > 0


# ── The runtime handle ──────────────────────────────────────────────────────


class FakeLocal:
    def __init__(self, node_id: str):
        self.node_id = node_id


def runtime_for(agent_server, control_id: str = "") -> ControlPlaneRuntime:
    runtime = ControlPlaneRuntime(agent_server, asyncio.get_event_loop())
    if control_id:
        runtime.local = FakeLocal(control_id)
    return runtime


async def test_the_control_node_resolves_without_consulting_the_registry(
    agent_server, agent_node
):
    """No lookup can be wrong if no lookup happens."""
    runtime = runtime_for(agent_server, control_id=agent_node.node_id)
    assert runtime.node_id_for(control_node()) == agent_node.node_id
    assert isinstance(runtime.service_for(control_node()), AgentNodeService)


async def test_an_enrolled_id_resolves_to_itself(agent_server, agent_node):
    runtime = runtime_for(agent_server)
    record = Node(id=agent_node.node_id, address="10.0.0.2")
    assert runtime.node_id_for(record) == agent_node.node_id


async def test_a_node_with_no_agent_is_a_refusal_not_a_fallback(agent_server):
    """The bug this prevents ran the control plane's own docker for a worker."""
    runtime = runtime_for(agent_server)
    with pytest.raises(LookupError) as caught:
        runtime.service_for(peer_node("10.0.0.9"))
    assert "no enrolled agent" in str(caught.value)


async def test_liveness_of_an_unenrolled_node_is_dead_not_an_error(agent_server):
    runtime = runtime_for(agent_server)
    assert runtime.liveness(peer_node("10.0.0.9")) is Liveness.DEAD


async def test_use_restores_the_previous_runtime(agent_server):
    runtime = runtime_for(agent_server)
    other = ControlPlaneRuntime(agent_server, asyncio.get_event_loop())
    with use(runtime):
        assert current() is runtime
        with use(other):
            assert current() is other
        assert current() is runtime
    assert current() is None


async def test_an_address_resolves_through_the_node_registry(
    agent_server, agent_node, monkeypatch
):
    """The weakest of the three joins, and the only one that can go stale."""
    import spark_pulse.tools.node_registry as registry

    class Record:
        id = agent_node.node_id
        address = "10.0.0.5"

    monkeypatch.setattr(registry, "list_nodes", lambda: [Record()])
    runtime = runtime_for(agent_server)
    assert runtime.node_id_for(peer_node("10.0.0.5")) == agent_node.node_id
    assert runtime.node_id_for(peer_node("10.0.0.6")) == ""
