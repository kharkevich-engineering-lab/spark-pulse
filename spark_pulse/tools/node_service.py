"""Node-bound container services.

A :class:`NodeService` is the container service *for one node*. The node is
chosen once, at construction, and never appears again: no method takes a host.

This replaces ``RemoteDockerService``, whose every method took a host as its
first argument with an empty string meaning "this node". Thirteen call sites
passed that empty string, so cluster stop, cluster status, every cluster health
check, Ray start-up and cluster reconciliation all queried the control node's
own Docker daemon while claiming to reach a worker. The health check that
compares ``NCCL_SOCKET_IFNAME`` across nodes compared the control node with
itself. Node identity as a per-call argument with a falsy default is a defect
generator, so it is gone.

**There is one transport, and it is the agent.** SSH does exactly one job in
this system now — carrying the agent onto a node during bootstrap — and never
runs an operation. The docker-CLI-over-SSH service that used to live in this
file is gone, along with the local/peer branch inside it: this process runs an
agent for *itself* and reaches it over loopback, so the control node is not a
special case and there is no local branch left to go untested. That branch
going untested is what made the thirteen call sites invisible.

Two implementations satisfy the interface, and they are signature-identical
because the interface *is* :class:`~spark_pulse.tools.docker.DockerService`'s
own method set:

* :class:`~spark_pulse.agent.sync_service.AgentNodeService` — one node, over
  its agent. What the control plane holds for every node, including its own.
* :class:`~spark_pulse.mock.docker.MockDockerService` — simulation, in memory.

``DockerService`` itself is still the thing that talks to Docker; it is just
on the far side now, as the agent's executor, one per node.

:func:`service_for` is the resolver: hand it a :class:`Node`, get the service
bound to it. ``spark_pulse.mock.node_service`` overrides only the resolver, so
simulation mode swaps the implementation without changing any call site.

:class:`Node` is deliberately minimal — an identifier, an address, whether it
is this machine, an SSH user and the interface names. It is a *reference* to a
node, not a description of one; the node registry holds the description and
:meth:`~spark_pulse.agent.runtime.ControlPlaneRuntime.node_id_for` is the one
place the two are joined.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from spark_pulse.tools.docker import (
    PULL_PROGRESS_INTERVAL,
    ContainerInfo,
    ContainerMetadata,
    DockerService,
    ExecResult,
)

logger = logging.getLogger(__name__)

CONTROL_NODE_ID = "control"

#: Addresses that always mean "the machine this process runs on". An empty
#: address is included because the old API used it as the local sentinel, and
#: a stray "" must resolve to the control node rather than to some other one.
LOOPBACK_ADDRESSES = frozenset(
    {"", "local", "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}
)

#: Seconds a container-state probe waits for a node before giving up on it.
#:
#: This is a *liveness* timeout, not a work timeout, and the two want very
#: different numbers. A node that is up answers an inspect in tens of
#: milliseconds — 28.7 ms measured on a GB10 over a warm connection
#: (``docs/rank-state-transport.md`` §1.1). A node that is gone answers never,
#: and the only thing a long wait buys is the wait: one silent node used to
#: cost a full ten seconds on every probe, nine consecutive times over ninety
#: seconds, and the poller learned nothing in between (§2.1). Three seconds is
#: a hundred times the warm answer, so it is only ever spent on a node that
#: was not going to answer at all.
#:
#: Exceeding it raises :class:`~spark_pulse.agent.errors.NodeUnreachable`,
#: which is the honest outcome: unknown, not missing.
STATUS_PROBE_TIMEOUT = 3


# ── The node record ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Node:
    """The minimum needed to reach one machine.

    Attributes:
        id: Stable identifier for the node. The control node uses
            :data:`CONTROL_NODE_ID`; peers default to their address.
        address: IP or hostname. Empty on the control node when nothing has
            discovered its address yet.
        is_self: Whether this record describes the machine we are running on.
            This is the only field that changes which transport is used.
        ssh_user: SSH login for a peer. Empty means "let ssh_config decide".
        interfaces: Interface names on this node, for NCCL/GLOO pinning. Not
            consumed yet — the interface-pinning gate is a later step.
    """

    id: str
    address: str = ""
    is_self: bool = False
    ssh_user: str = ""
    interfaces: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """Human-readable name for logs and error messages."""
        return self.address or self.id


def control_node(address: str = "", interfaces: tuple[str, ...] = ()) -> Node:
    """The node record for the machine this process runs on."""
    return Node(
        id=CONTROL_NODE_ID,
        address=address,
        is_self=True,
        interfaces=tuple(interfaces),
    )


def peer_node(
    address: str,
    *,
    node_id: str | None = None,
    ssh_user: str = "",
    interfaces: tuple[str, ...] = (),
) -> Node:
    """The node record for another machine, reached over SSH.

    This never consults the local addresses: a caller that says "peer" gets a
    peer. Use :func:`node_for` when the address may turn out to be our own.
    """
    if not address:
        raise ValueError("a peer node needs an address")
    return Node(
        id=node_id or address,
        address=address,
        is_self=False,
        ssh_user=ssh_user,
        interfaces=tuple(interfaces),
    )


def local_addresses() -> frozenset[str]:
    """Every address that resolves to this machine.

    Discovery shells out, so the answer is cached for the life of the process;
    a node's own addresses do not change under us mid-deployment.
    """
    global _local_addresses
    if _local_addresses is None:
        found = set(LOOPBACK_ADDRESSES)
        try:
            from spark_pulse.tools import discovery

            for interface in discovery.detect_network_interfaces():
                if interface.ip:
                    found.add(interface.ip)
            primary = discovery.detect_local_ip()
            if primary:
                found.add(primary)
        except Exception as exc:  # pragma: no cover — discovery is best effort
            logger.debug("Could not enumerate local addresses: %s", exc)
        _local_addresses = frozenset(found)
    return _local_addresses


_local_addresses: frozenset[str] | None = None


def reset_local_addresses() -> None:
    """Drop the cached local-address set (tests, and address changes)."""
    global _local_addresses
    _local_addresses = None


def is_local_address(address: str) -> bool:
    """Whether ``address`` names the machine this process runs on."""
    return (address or "").strip().lower() in local_addresses()


def node_for(
    address: str,
    *,
    ssh_user: str = "",
    interfaces: tuple[str, ...] = (),
) -> Node:
    """The node record for ``address``, control node when it is this machine.

    This is the one place that decides "is that us?", so a caller holding an
    IP out of a container label or a request body does not have to.
    """
    if is_local_address(address):
        return control_node(address=address, interfaces=interfaces)
    return peer_node(address, ssh_user=ssh_user, interfaces=interfaces)


# ── The interface ────────────────────────────────────────────────────────────


class NodeService(Protocol):
    """Container operations on exactly one node.

    Every method's signature is :class:`DockerService`'s, unchanged, because
    ``native_runtime`` already calls a service through these names and
    ``MockDockerService`` already subclasses ``DockerService``. What used to be
    the first argument — the host — is now the node the service was built for.
    """

    def run_container(
        self,
        image: str,
        name: str,
        env_vars: dict[str, str],
        metadata: ContainerMetadata,
        privileged: bool = True,
        memory_limit_gb: float | None = None,
        shm_size_gb: float = 64,
        pids_limit: int = 4096,
        nofile_limit: int = 1048576,
        cache_dirs: list[str] | None = None,
        port_mappings: list[str] | None = None,
        entrypoint_clear: bool = True,
        detach: bool = True,
        command: str | list[str] | None = None,
        mounts: dict[str, str] | None = None,
        network_host: bool | None = None,
        ipc_host: bool = False,
        devices: list[str] | None = None,
        cap_add: list[str] | None = None,
        ulimits: dict[str, str] | None = None,
        auto_remove: bool = True,
    ) -> ContainerInfo:
        """Build and start a container carrying spark-pulse labels."""
        ...

    def ensure_directories(self, paths: Iterable[str]) -> list[str]:
        """Create bind-mount source directories on the node before a run.

        Docker creates a missing bind source itself, **owned by root**, and
        the caches mounted here are the login user's — so a directory that
        does not exist yet is how ``~/.cache/huggingface`` ends up root-owned
        and every later model copy fails with a permission error.
        ``launch-cluster.sh`` does the same ``mkdir -p``, on the head at line
        1094 and on every worker at line 1104.

        Returns the paths it could not create; a caller treats them as a
        warning rather than a failure, since docker will still start.
        """
        ...

    def stop_container(self, name: str, timeout: int = 30) -> bool:
        """Stop and remove a container by name."""
        ...

    def get_container_status(self, name: str) -> dict[str, Any]:
        """Return ``{status, running, id, state, error}`` for a container."""
        ...

    def exec_in_container(
        self,
        container: str | Any,
        command: str | list[str],
        detach: bool = False,
        timeout: int | None = None,
    ) -> ExecResult:
        """Execute a command inside a running container."""
        ...

    def copy_to_container(
        self,
        container: str,
        local_path: str,
        remote_path: str,
        timeout: int = 120,
    ) -> bool:
        """Copy a file from this machine into a container on the node."""
        ...

    def get_logs(self, name: str, tail: int = 200) -> str:
        """Return the tail of a container's logs."""
        ...

    def list_managed_containers(
        self,
        labels: dict[str, str] | None = None,
    ) -> list[ContainerInfo]:
        """Every spark-pulse managed container on the node, filtered by label."""
        ...

    def get_container_by_deployment(self, deployment: str) -> ContainerInfo | None:
        """The container carrying a deployment label, or None."""
        ...

    def get_container_by_recipe(self, recipe: str) -> list[ContainerInfo]:
        """Every container carrying a recipe label."""
        ...

    def image_exists(self, ref: str) -> bool:
        """Whether the image reference resolves on the node."""
        ...

    def image_info(self, ref: str) -> dict[str, Any] | None:
        """``{id, size_bytes, created, repo_tags, repo_digests}`` or None."""
        ...

    def list_images(self) -> list[dict[str, Any]]:
        """Every image on the node, shaped like :meth:`image_info`."""
        ...

    def pull_image(
        self,
        ref: str,
        progress: Any | None = None,
        interval: float = PULL_PROGRESS_INTERVAL,
        cancel: Callable[[], bool] | None = None,
        stall_timeout: float | None = None,
    ) -> dict[str, Any]:
        """Pull an image onto the node, reporting aggregated progress."""
        ...

    def remove_image(self, ref: str, force: bool = False) -> bool:
        """Remove an image from the node. False when it was not there."""
        ...


#: The method names the three implementations must agree on.
NODE_SERVICE_METHODS: tuple[str, ...] = (
    "run_container",
    "ensure_directories",
    "stop_container",
    "get_container_status",
    "exec_in_container",
    "copy_to_container",
    "get_logs",
    "list_managed_containers",
    "get_container_by_deployment",
    "get_container_by_recipe",
    "image_exists",
    "image_info",
    "list_images",
    "pull_image",
    "remove_image",
)


def run_kwargs_from_docker_config(docker_config: dict[str, Any] | None) -> dict:
    """Map the cluster ``docker_config`` dict onto ``run_container`` kwargs.

    The cluster API still takes an untyped config blob from its request body.
    ``memory_swap_limit_gb`` is dropped on purpose: the container service
    derives swap from the memory limit itself, on both the SDK and CLI paths.
    """
    config = docker_config or {}
    kwargs: dict[str, Any] = {
        "privileged": bool(config.get("privileged", True)),
        "shm_size_gb": config.get("shm_size_gb", 64),
        "pids_limit": config.get("pids_limit", 4096),
        "nofile_limit": config.get("nofile_limit", 1048576),
    }
    if config.get("memory_limit_gb"):
        kwargs["memory_limit_gb"] = config["memory_limit_gb"]
    if config.get("cache_dirs"):
        kwargs["cache_dirs"] = list(config["cache_dirs"])
    if config.get("port_mappings"):
        kwargs["port_mappings"] = list(config["port_mappings"])
    for passthrough in ("entrypoint_clear", "network_host", "command", "mounts"):
        if passthrough in config:
            kwargs[passthrough] = config[passthrough]
    return kwargs


# ── The resolver ─────────────────────────────────────────────────────────────


class NoAgent(LookupError):
    """There is no agent for this node, so there is no way to reach it.

    A named error rather than a fallback. The alternative — quietly using the
    control plane's own Docker daemon when a node cannot be reached — is not
    hypothetical: ``docs/transport-reexamined.md`` §5.1 found thirteen call
    sites doing exactly that, and every one of them read plausible answers off
    the wrong machine.
    """


def service_for(
    node: Node,
    *,
    docker_service: DockerService | None = None,
    **_ignored: Any,
) -> NodeService:
    """The container service bound to ``node``, over that node's agent.

    There is one transport now. The control node is not a special case: this
    process runs an agent for itself and reaches it over loopback, exactly as
    it reaches a peer, so there is no local branch here to leave untested.
    That was the previous boundary's defining defect.

    ``docker_service`` pins a service outright and skips resolution. It exists
    for callers that already hold one — chiefly tests, and
    ``native_runtime.rank_services(docker=...)`` — and never as a fallback.

    ``spark_pulse.mock.node_service`` overrides this function, so simulation
    is one swap at the resolver rather than a branch at every call site.
    """
    if docker_service is not None:
        return docker_service
    from spark_pulse.agent import runtime as agent_runtime

    current = agent_runtime.current()
    if current is None:
        raise NoAgent(
            f"the control plane's agent transport is not running, so {node.label} "
            "cannot be reached. This is a startup ordering problem: nothing "
            "should resolve a node service before the runtime is up."
        )
    try:
        return current.service_for(node)
    except LookupError as exc:
        raise NoAgent(str(exc)) from None


class NodeServices:
    """A resolver that remembers the service it built for each node.

    Callers hold one of these instead of a service, because they act on
    several nodes: the orchestrator, the health validator and the Ray manager
    all take one. It is a plain callable, so a test can pass a lambda.
    """

    def __init__(
        self,
        docker_service: DockerService | None = None,
        resolver: Callable[..., NodeService] | None = None,
    ):
        self._docker_service = docker_service
        self._resolver = resolver or service_for
        self._cache: dict[str, NodeService] = {}

    def __call__(self, node: Node) -> NodeService:
        """Return (and cache) the service bound to ``node``."""
        key = f"{node.id}\0{node.address}\0{node.is_self}"
        service = self._cache.get(key)
        if service is None:
            service = self._resolver(node, docker_service=self._docker_service)
            self._cache[key] = service
        return service

    def for_address(self, address: str, *, ssh_user: str = "") -> NodeService:
        """The service for whatever machine ``address`` names."""
        return self(node_for(address, ssh_user=ssh_user))

    def control(self) -> NodeService:
        """The service for the machine this process runs on."""
        return self(control_node())


__all__ = [
    "CONTROL_NODE_ID",
    "LOOPBACK_ADDRESSES",
    "NODE_SERVICE_METHODS",
    "STATUS_PROBE_TIMEOUT",
    "NoAgent",
    "Node",
    "NodeService",
    "NodeServices",
    "control_node",
    "is_local_address",
    "local_addresses",
    "node_for",
    "peer_node",
    "reset_local_addresses",
    "run_kwargs_from_docker_config",
    "service_for",
]
