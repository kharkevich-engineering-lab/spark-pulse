"""Node-bound container services, simulated.

This module used to be a second, hand-written copy of a remote container
service, which had drifted from the real one — different label matching, no
image-info shape, a ``ray status`` answer baked into ``exec``. A parallel
implementation cannot catch a bug in the implementation it is standing in for,
so it was replaced by the *real* docker-over-SSH service driven through a
simulated SSH transport.

That transport is gone too, and for a better reason than drift: **production
does not run docker over SSH any more.** Every operation goes to a node's
agent, and the agent's own executor is a ``DockerService`` on the far side. So
what a peer looks like from here is not a command line to parse — it is a
container service with its own state. That is exactly what
:class:`~spark_pulse.mock.docker.MockDockerService` is.

The one property worth preserving from the old transport is preserved, and it
is the important one: **every node has its own Docker.** When all nodes read
one daemon, an answer for the wrong node is indistinguishable from the right
answer, which is how thirteen call sites came to query the control node while
claiming to reach a worker (``docs/transport-reexamined.md`` §5.1). Here, a
node that answers for another node answers with the wrong containers, loudly.

Only the resolver differs from the real module. Everything else — the node
record, the interface, the address logic — is imported from it unchanged.
"""

from __future__ import annotations

from typing import Any, Callable

from spark_pulse.tools.docker import DockerService
from spark_pulse.tools.node_service import (
    CONTROL_NODE_ID as CONTROL_NODE_ID,
    LOOPBACK_ADDRESSES as LOOPBACK_ADDRESSES,
    NODE_SERVICE_METHODS as NODE_SERVICE_METHODS,
    STATUS_PROBE_TIMEOUT as STATUS_PROBE_TIMEOUT,
    NoAgent as NoAgent,
    Node as Node,
    NodeService as NodeService,
    NodeServices as _NodeServices,
    control_node as control_node,
    is_local_address as is_local_address,
    local_addresses as local_addresses,
    node_for as node_for,
    peer_node as peer_node,
    reset_local_addresses as reset_local_addresses,
    run_kwargs_from_docker_config as run_kwargs_from_docker_config,
)

DEFAULT_IMAGE_SIZE = 26_843_545_600

#: One simulated Docker per peer, keyed by whatever identifies that peer.
#: Never keyed by name — two nodes may share a name, and the failure that
#: causes is two machines sharing a container list.
_peers: dict[str, Any] = {}


def _key(node: Node) -> str:
    """How a peer is identified here: its id, or its address if it has none."""
    return node.id or node.address


def docker_for(node: Node) -> Any:
    """The simulated Docker belonging to ``node``, created on first ask.

    This is the seam a test uses to look at, or prime, one node's containers
    without going through the service — the replacement for reaching into the
    old simulated SSH transport, and a much smaller one.
    """
    from spark_pulse.mock.docker import MockDockerClient, MockDockerService

    if node.is_self:
        from spark_pulse.mock.docker import _get_service

        return _get_service()
    key = _key(node)
    service = _peers.get(key)
    if service is None:
        # No seeded images. A node that has just been added has not pulled
        # anything, and a simulation that pretends otherwise makes every
        # preflight report a 26 GB download as already done — the one answer
        # that check exists to give.
        service = MockDockerService(MockDockerClient(seeded_images=False))
        _peers[key] = service
    return service


#: Peers whose agent is not connected. Every operation on one is *unknown*.
unreachable: set[str] = set()

#: Peers that answer, whose Docker daemon does not. Reachable, and the failure
#: is definite — which is the distinction the whole transport exists to keep,
#: so simulation has to be able to produce both.
daemon_down: set[str] = set()

#: Every operation asked of a peer, in order: ``{"host", "op", "args"}``.
#: Simulation's replacement for a command log. It records *operations* rather
#: than command lines because there are no command lines any more — which is
#: also why it is shorter and cannot drift from a parser.
calls: list[dict[str, Any]] = []


def reset() -> None:
    """Forget every simulated peer, and with it every container on one."""
    _peers.clear()
    unreachable.clear()
    daemon_down.clear()
    calls.clear()


def containers_on(host: str) -> list[str]:
    """The managed containers a simulated peer is holding, by name."""
    docker = docker_for(peer_node(host))
    return sorted(c.name for c in docker.list_managed_containers())


def hosts_seen() -> list[str]:
    """Every peer anything was asked of, in first-contact order."""
    seen: list[str] = []
    for entry in calls:
        if entry["host"] not in seen:
            seen.append(entry["host"])
    return seen


def operations_on(host: str) -> list[str]:
    """The names of the operations asked of one peer, in order."""
    return [entry["op"] for entry in calls if entry["host"] == host]


class SimulatedPeerService:
    """A peer's container service, over a transport that can fail two ways.

    Every method is generated rather than written out, and that is safe here
    in a way it would not be in production: this class adds no behaviour of
    its own, so there is no signature for a caller to depend on beyond the one
    ``MockDockerService`` already publishes. What it *does* add is the two
    failure modes a real node has and an in-memory service does not — no
    answer at all, and an answer that says no — because a simulation that can
    only succeed cannot exercise the code that tells those apart.
    """

    def __init__(self, host: str, docker: Any):
        self.host = host
        self.docker = docker

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<SimulatedPeerService {self.host}>"

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name not in NODE_SERVICE_METHODS:
            raise AttributeError(name)
        method = getattr(self.docker, name)

        def _call(*args: Any, **kwargs: Any) -> Any:
            calls.append({"host": self.host, "op": name, "args": args})
            if self.host in unreachable:
                from spark_pulse.agent.errors import NodeUnreachable

                raise NodeUnreachable(self.host)
            # A refusing daemon refuses at the *client*, not here, so what
            # runs above it is ``DockerService``'s own handling of that —
            # ``unknown`` from a status, ``False`` from ``image_exists``, a
            # raise from a listing. Raising from the transport instead would
            # exercise a branch production never takes.
            self.docker.client.daemon.down = self.host in daemon_down
            try:
                return method(*args, **kwargs)
            finally:
                self.docker.client.daemon.down = False

        return _call


def service_for(
    node: Node,
    *,
    docker_service: DockerService | None = None,
    **_ignored: Any,
) -> NodeService:
    """The simulated container service bound to ``node``.

    The control node gets the in-memory Docker every other simulated tool
    already shares, so a container started through ``tools.docker`` and one
    started through a node service are the same container. A peer gets its
    own.
    """
    if docker_service is not None:
        return docker_service
    if node.is_self:
        return docker_for(node)
    return SimulatedPeerService(_key(node), docker_for(node))


class NodeServices(_NodeServices):
    """The real resolver cache, defaulting to the simulated resolver."""

    def __init__(
        self,
        docker_service: DockerService | None = None,
        resolver: Callable[..., NodeService] | None = None,
    ):
        super().__init__(
            docker_service=docker_service,
            resolver=resolver or service_for,
        )


__all__ = [
    "CONTROL_NODE_ID",
    "DEFAULT_IMAGE_SIZE",
    "LOOPBACK_ADDRESSES",
    "NODE_SERVICE_METHODS",
    "STATUS_PROBE_TIMEOUT",
    "NoAgent",
    "Node",
    "NodeService",
    "NodeServices",
    "SimulatedPeerService",
    "calls",
    "containers_on",
    "control_node",
    "daemon_down",
    "docker_for",
    "hosts_seen",
    "operations_on",
    "unreachable",
    "is_local_address",
    "local_addresses",
    "node_for",
    "peer_node",
    "reset",
    "reset_local_addresses",
    "run_kwargs_from_docker_config",
    "service_for",
]
