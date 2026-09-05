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

Three implementations satisfy the interface, and they are signature-identical
because the interface *is* :class:`~spark_pulse.tools.docker.DockerService`'s
own method set:

* :class:`~spark_pulse.tools.docker.DockerService` — this machine, Docker SDK.
* :class:`RemoteNodeService` — one node, via the docker CLI over SSH, or via
  the local SDK when that node is this machine.
* :class:`~spark_pulse.mock.docker.MockDockerService` — simulation, in memory.

:func:`service_for` is the resolver: hand it a :class:`Node`, get the service
bound to it. ``spark_pulse.mock.node_service`` overrides only the resolver, so
simulation mode swaps the implementation without changing any call site.

:class:`Node` is deliberately minimal — an identifier, an address, whether it
is this machine, an SSH user and the interface names. The full node registry
of the cluster-agent plan's step one is later work; this is only the boundary
that registry will later populate.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from spark_pulse.tools.docker import (
    PULL_PROGRESS_INTERVAL,
    ContainerInfo,
    ContainerMetadata,
    DockerService,
    ExecResult,
    PullCancelled,
    _labels_match,
    prepare_labels,
    split_ref,
)
from spark_pulse.tools.labels import MANAGED_FILTER
from spark_pulse.tools.ssh import OpenSSHClient, SSHClient

logger = logging.getLogger(__name__)

CONTROL_NODE_ID = "control"

#: Addresses that always mean "the machine this process runs on". An empty
#: address is included because the old API used it as the local sentinel, and
#: a stray "" must resolve to the control node rather than to an SSH attempt.
LOOPBACK_ADDRESSES = frozenset(
    {"", "local", "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}
)

#: Seconds a container-state probe waits for a node before giving up on it.
#:
#: This is a *liveness* timeout, not a work timeout, and the two want very
#: different numbers. A node that is up answers ``docker inspect`` in 28.7 ms
#: over a warm multiplexed connection, and pays about 500 ms when the control
#: master has to be established first — both measured on a GB10
#: (``docs/rank-state-transport.md`` §1.1-1.2). A node that is gone answers
#: never, and the only thing a long wait buys is the wait: with ssh's own
#: ``ConnectTimeout`` of 10 s, one silent node cost a full ten seconds on
#: every probe, nine consecutive times over ninety seconds, and the poller
#: learned nothing in between (§2.1). Three seconds is six times the cold
#: handshake and a hundred times the warm answer, so it is only ever spent on
#: a node that was not going to answer at all.
#:
#: Exceeding it raises :class:`~spark_pulse.tools.ssh.SSHError` from the
#: transport, which is the honest outcome: unreachable, not missing.
STATUS_PROBE_TIMEOUT = 3

#: A daemon-liveness probe, run only once an inspect has already failed.
#:
#: ``docker inspect`` exits 1 both for a container that is not there and for a
#: daemon that is not there, so the exit code we already hold carries no
#: signal. Asking the daemon for its *server* version does: it cannot fail on
#: account of the container, and it exits zero only if a daemon answered. See
#: :meth:`RemoteNodeService.get_container_status`.
DAEMON_PROBE_COMMAND = "docker version --format '{{.Server.Version}}'"


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


# ── Helpers shared by the SSH path ───────────────────────────────────────────


def _to_exec_result(result: Any) -> ExecResult:
    """Normalise an SSHResult (or ExecResult) into an ExecResult."""
    if isinstance(result, ExecResult):
        return result
    return ExecResult(
        returncode=getattr(result, "returncode", 1),
        stdout=getattr(result, "stdout", "") or "",
        stderr=getattr(result, "stderr", "") or "",
    )


def _argv(command: str | list[str]) -> str:
    """Render a command as a single shell-safe string."""
    if isinstance(command, str):
        return command
    return " ".join(shlex.quote(part) for part in command)


def _image_info_from_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """The ``image_info`` shape out of ``docker image inspect``'s JSON.

    The SDK path builds the same dict out of ``image.attrs``, which is the
    same daemon response — so there is one function rather than two spellings
    of one mapping.
    """
    return {
        "id": attrs.get("Id", ""),
        "size_bytes": int(attrs.get("Size") or 0),
        "created": attrs.get("Created"),
        "repo_tags": list(attrs.get("RepoTags") or []),
        "repo_digests": list(attrs.get("RepoDigests") or []),
    }


def _parse_cli_labels(raw: str) -> dict[str, str]:
    """Parse the comma-separated ``key=value`` label list from ``docker ps``."""
    labels: dict[str, str] = {}
    for pair in (raw or "").split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            labels[key.strip()] = value.strip()
    return labels


#: Docker's own container status vocabulary, which is what the SDK path hands
#: back verbatim as ``container.status``.
DOCKER_STATES = frozenset(
    {"created", "restarting", "running", "removing", "paused", "exited", "dead"}
)


def _normalize_cli_status(raw: str | None, running: bool = False) -> str:
    """Map a ``docker ps``/``docker inspect`` state word onto Docker's own.

    This used to collapse everything to ``running`` or ``stopped``, which is a
    vocabulary the local path never speaks: ``DockerService`` returns
    ``container.status``, so ``exited``, ``created``, ``paused`` and ``dead``
    all reach a caller unchanged from the control node and never from a peer.
    :func:`~spark_pulse.tools.reconciliation._clean_orphaned_containers` sweeps
    on ``status == "exited"`` and so could not fire on a peer at all.

    ``docker ps --format '{{json .}}'`` puts that same word in ``State``, and
    ``docker inspect``'s ``State.Status`` is the identical field, so in practice
    this passes it straight through. The ``Up 2 minutes`` / ``Exited (0) …``
    prose of the ``Status`` column is the fallback, for a Docker old enough not
    to publish ``State``.
    """
    text = (raw or "").strip().lower()
    if text in DOCKER_STATES:
        return text
    if text.startswith("up"):
        return "running"
    if text.startswith("exited") or text.startswith("dead"):
        return "exited"
    if text.startswith("created"):
        return "created"
    if text.startswith("paused"):
        return "paused"
    if text.startswith("restarting"):
        return "restarting"
    return "running" if running else "exited"


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


# ── The node-bound remote service ────────────────────────────────────────────


class RemoteNodeService(NodeService):
    """The container service for one node, over SSH or over the local SDK.

    The node is fixed at construction, so no *caller* ever chooses a
    transport: the control node is served by :class:`DockerService` through
    the Docker SDK, and a peer by the docker CLI over SSH. Both return the same
    shapes, so callers never learn which one they hold.

    The local branch exists so a size-one cluster runs the ordinary path
    against loopback rather than through a special case, which is what every
    orchestrator surveyed for the cluster-agent plan does.

    What the branch is *not* is "picked once and for all". Every method below
    branches on ``node.is_self`` again, so there are two implementations of one
    interface speaking two languages about the same facts — the SDK's objects
    and typed exceptions on one side, the CLI's text and exit codes on the
    other — and each pair is a place they can disagree.
    ``docs/transport-reexamined.md`` §5.1 audited them and found thirty
    divergences, three of them live bugs, twenty-seven of them in methods no
    test exercised. ``tests/test_container_service_contract.py`` now drives all
    fifteen methods through every implementation and states the differences
    that are deliberate, so a new one fails a test rather than a deployment.
    The permanent fix is one implementation over two transports — the Docker
    Engine API over a unix socket locally and over SSH remotely — which is that
    document's recommendation and is not what this class is.
    """

    def __init__(
        self,
        node: Node,
        ssh_client: SSHClient | None = None,
        docker_service: DockerService | None = None,
    ):
        """Bind a container service to one node.

        Args:
            node: The node every operation runs against.
            ssh_client: SSH transport, used only for a peer.
            docker_service: Local Docker service, used only for the control
                node. Built lazily so a peer-bound service never so much as
                constructs a client for the local daemon.
        """
        self.node = node
        self._ssh_client = ssh_client
        self._docker_service = docker_service

    # ── Transport ────────────────────────────────────────────────────────

    @property
    def is_local(self) -> bool:
        """Whether this service runs against the local Docker daemon."""
        return self.node.is_self

    @property
    def _local(self) -> DockerService:
        """The local Docker service, built on first use."""
        if not self.node.is_self:
            raise RuntimeError(
                f"node {self.node.label} is a peer; it has no local Docker daemon"
            )
        if self._docker_service is None:
            self._docker_service = DockerService()
        return self._docker_service

    @property
    def _ssh(self) -> SSHClient:
        """The SSH transport, built on first use."""
        if self.node.is_self:
            raise RuntimeError(
                f"node {self.node.label} is this machine; it is not reached over SSH"
            )
        if self._ssh_client is None:
            self._ssh_client = OpenSSHClient(user=self.node.ssh_user or None)
        return self._ssh_client

    def _exec(self, command: str, timeout: int = 30) -> Any:
        """Run a shell command on the node over SSH."""
        return self._ssh.exec(self.node.address, command, timeout=timeout)

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        where = "local" if self.node.is_self else f"ssh {self.node.address}"
        return f"<RemoteNodeService {self.node.id} ({where})>"

    # ── Lifecycle ────────────────────────────────────────────────────────

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
        """Run a container on the node."""
        if self.node.is_self:
            return self._local.run_container(
                image=image,
                name=name,
                env_vars=env_vars,
                metadata=metadata,
                privileged=privileged,
                memory_limit_gb=memory_limit_gb,
                shm_size_gb=shm_size_gb,
                pids_limit=pids_limit,
                nofile_limit=nofile_limit,
                cache_dirs=cache_dirs,
                port_mappings=port_mappings,
                entrypoint_clear=entrypoint_clear,
                detach=detach,
                command=command,
                mounts=mounts,
                network_host=network_host,
                ipc_host=ipc_host,
                devices=devices,
                cap_add=cap_add,
                ulimits=ulimits,
                auto_remove=auto_remove,
            )

        labels = prepare_labels(metadata, name, image)
        cmd_parts = ["docker", "run"]
        if detach:
            cmd_parts.append("-d")

        for key, value in (env_vars or {}).items():
            cmd_parts.extend(["-e", shlex.quote(f"{key}={value}")])

        if privileged:
            cmd_parts.append("--privileged")
        else:
            # Same default as the SDK path: unprivileged still needs IPC_LOCK.
            extra_caps = list(cap_add or [])
            if "IPC_LOCK" not in extra_caps:
                extra_caps.append("IPC_LOCK")
            for capability in extra_caps:
                cmd_parts.extend(["--cap-add", capability])
        if memory_limit_gb:
            cmd_parts.extend(["--memory", f"{memory_limit_gb}g"])
            # The SDK path derives ``memswap_limit`` from the memory limit and
            # for a long time this one did not, so a rank on a peer got the
            # daemon's default (swap unlimited) where the control node's rank
            # got limit + 10 GB. ``run_kwargs_from_docker_config`` drops the
            # caller's ``memory_swap_limit_gb`` because "the container service
            # derives swap from the memory limit itself, on both the SDK and
            # CLI paths" — which was true of one path only.
            memswap = DockerService._calc_memory_swap(memory_limit_gb)
            if memswap is not None:
                cmd_parts.extend(["--memory-swap", str(memswap)])
        if pids_limit:
            cmd_parts.extend(["--pids-limit", str(pids_limit)])
        if shm_size_gb:
            cmd_parts.extend(["--shm-size", f"{shm_size_gb}g"])
        if nofile_limit:
            cmd_parts.extend(["--ulimit", f"nofile={nofile_limit}"])
        for ulimit_name, raw in (ulimits or {}).items():
            if ulimit_name != "nofile":
                cmd_parts.extend(["--ulimit", f"{ulimit_name}={raw}"])

        for cache_dir in cache_dirs or []:
            cmd_parts.extend(["-v", f"{cache_dir}:{cache_dir}:rw"])
        for host_path, container_path in (mounts or {}).items():
            cmd_parts.extend(["-v", f"{host_path}:{container_path}:rw"])

        # The SDK path always requests every GPU; the CLI path now matches it.
        cmd_parts.extend(["--gpus", "all"])

        if network_host is None:
            use_host_network = not port_mappings
        else:
            use_host_network = bool(network_host)
        if use_host_network:
            cmd_parts.extend(["--network", "host"])
        for mapping in port_mappings or []:
            cmd_parts.extend(["-p", mapping])
        if ipc_host:
            cmd_parts.extend(["--ipc", "host"])
        for device in devices or []:
            cmd_parts.extend(["--device", f"{device}:{device}:rwm"])
        if auto_remove:
            cmd_parts.append("--rm")
        # Never restart — same reason as the SDK path: a rebooting node must
        # not resurrect a rank into a torn-down deployment.
        cmd_parts.extend(["--restart", "no"])

        for key, value in labels.items():
            cmd_parts.extend(["--label", shlex.quote(f"{key}={value}")])

        cmd_parts.extend(["--name", name])
        if entrypoint_clear:
            cmd_parts.extend(["--entrypoint", '""'])

        cmd_parts.append(image)
        if command is not None:
            cmd_parts.append(_argv(command))

        result = self._exec(" ".join(cmd_parts), timeout=120)
        if not result.ok:
            raise RuntimeError(
                f"docker run failed on {self.node.label}: {result.stderr}"
            )

        container_id = result.stdout.strip().split("\n")[0]
        return ContainerInfo(
            id=container_id,
            name=name,
            status="running",
            image=image,
            metadata=metadata,
            labels=labels,
        )

    def ensure_directories(self, paths: Iterable[str]) -> list[str]:
        """``mkdir -p`` every path on the node. Returns the ones that failed.

        Upstream does exactly this before ``docker run``, locally at
        ``launch-cluster.sh`` line 1094 and over SSH at line 1104, and for the
        same reason: a bind source docker has to invent is created as root,
        and these are the login user's caches. The runbook's whole
        "Troubleshoot model-copy permissions" section is the aftermath.
        """
        # Stripped, like the local path: a trailing newline out of a config
        # file would otherwise become part of a directory name on a peer and
        # not on the control node.
        wanted = [str(path).strip() for path in paths or [] if str(path).strip()]
        if not wanted:
            return []

        if self.node.is_self:
            failed: list[str] = []
            for path in wanted:
                try:
                    os.makedirs(path, exist_ok=True)
                except OSError as exc:
                    logger.warning("could not create %s: %s", path, exc)
                    failed.append(path)
            return failed

        quoted = " ".join(shlex.quote(path) for path in wanted)
        result = self._exec(f"mkdir -p {quoted}", timeout=30)
        if result.ok:
            return []
        logger.warning(
            "could not create %s on %s: %s", quoted, self.node.label, result.stderr
        )
        return wanted

    def stop_container(self, name: str, timeout: int = 30) -> bool:
        """Stop **and remove** a container on the node.

        The removal is not decoration. ``DockerService.stop_container`` does
        ``container.stop()`` followed by ``container.remove(force=True)``, and
        this path used to issue ``docker stop`` alone — the string ``docker
        rm`` appeared nowhere in this module — while its docstring promised the
        same thing. Containers are created with ``auto_remove=False``
        (``native_runtime`` keeps them so ``docker logs`` survives a crash), so
        nothing removed them afterwards either. A stopped-but-present container
        still answers ``docker inspect``, so it never read as ``missing``,
        :func:`~spark_pulse.tools.native_runtime._is_confirmed_gone` never
        confirmed it, ``_confirm_gone`` spun for its full 30 s and **every
        multi-node teardown recorded an outstanding orphan and held that rank's
        ports for good** — per rank, per teardown, on every peer.

        ``&&`` rather than ``;`` on purpose: it mirrors the local path, where a
        ``stop`` that raises returns False without reaching the remove.
        """
        if self.node.is_self:
            return self._local.stop_container(name, timeout=timeout)

        quoted = shlex.quote(name)
        result = self._exec(
            f"docker stop -t {timeout} {quoted} && docker rm -f {quoted}",
            timeout=timeout + 10,
        )
        if not result.ok:
            logger.warning(
                "Failed to stop and remove container %s on %s: %s",
                name,
                self.node.label,
                result.stderr,
            )
        return bool(result.ok)

    def _daemon_answered(self) -> bool:
        """Whether this node's Docker daemon is there at all.

        Raises :class:`~spark_pulse.tools.ssh.SSHError` if the node itself
        cannot be reached — the caller must not turn that into an answer.
        """
        return bool(self._exec(DAEMON_PROBE_COMMAND, timeout=STATUS_PROBE_TIMEOUT).ok)

    def _inspect_failed_status(self, name: str, stderr: str) -> dict[str, Any]:
        """Tell "no such container" apart from "no daemon answered".

        Both exit 1. Measured on a GB10 running Docker 29.2.1
        (``docs/rank-state-transport.md`` §2.4): ``docker inspect`` against a
        container that does not exist exits 1 with ``no such object``, and
        ``docker inspect`` against an unreachable daemon exits 1 with ``Cannot
        connect to the Docker daemon``. So the exit code carries no signal and
        the only difference sitting in front of us is English prose.

        Rather than read that prose and call it a fact, this asks the daemon a
        question that cannot fail on account of the container: ``docker
        version`` reports the *server* version, so it exits zero only when a
        daemon answered. That exit status is structural, it costs one extra
        round trip on a path that has already failed, and it is what this
        branches on. The stderr text is carried into ``error`` for a human and
        is never a decision input.

        Getting this wrong in the "missing" direction is the expensive one:
        :func:`native_runtime.sweep_orphans` clears an orphan — and with it
        the ports the record was holding — on ``status == "missing"`` alone.
        A rank we could not ask about must never produce it.
        """
        if self._daemon_answered():
            return {
                "status": "missing",
                "running": False,
                "id": None,
                "state": {},
                "error": f"Container '{name}' not found on {self.node.label}",
            }
        detail = stderr.strip() or "docker did not answer"
        return {
            # The same third state the local path reports as "error"
            # (``DockerService.get_container_status``) and that ``_rank_status``
            # names for a node it could not reach: we did not learn anything,
            # which is not the same as learning the container is gone.
            "status": "unknown",
            "running": False,
            "id": None,
            "state": {},
            "error": (
                f"Docker on {self.node.label} did not answer, so the state of "
                f"'{name}' is unknown: {detail}"
            ),
        }

    def get_container_status(self, name: str) -> dict[str, Any]:
        """Return the container's state on the node.

        Three outcomes, never two: Docker's own status word when the daemon
        described the container, ``missing`` when the daemon told us there is
        no such container, and ``unknown`` when nobody told us anything. An
        unreachable node raises instead, which is the fourth way of saying the
        third thing.

        The status word is ``State.Status`` verbatim — ``running``, ``exited``,
        ``created``, ``paused``, ``restarting``, ``dead`` — because that is
        what the local path returns and callers compare against it:
        :func:`~spark_pulse.tools.reconciliation._clean_orphaned_containers`
        sweeps on ``status == "exited"``, which a running/stopped collapse
        could never match on a peer. ``id`` is asked for in the same inspect,
        so a caller can compare a peer's container id with a local one.
        """
        if self.node.is_self:
            return self._local.get_container_status(name)

        result = self._exec(
            # One inspect, both facts. ``.Id`` is the full 64-hex id, the same
            # thing the SDK's ``container.id`` is.
            f"docker inspect --format '{{{{json .State}}}}\t{{{{.Id}}}}' "
            f"{shlex.quote(name)}",
            timeout=STATUS_PROBE_TIMEOUT,
        )
        if not result.ok:
            return self._inspect_failed_status(name, result.stderr or "")

        raw_state, _, raw_id = (result.stdout or "").strip().partition("\t")
        try:
            state = json.loads(raw_state.strip())
        except json.JSONDecodeError:
            return {
                "status": "unknown",
                "running": False,
                "id": None,
                "state": {},
                "error": f"Could not parse docker inspect output for '{name}'",
            }

        running = bool(state.get("Running", False))
        return {
            "status": _normalize_cli_status(state.get("Status"), running=running),
            "running": running,
            "id": raw_id.strip() or None,
            "state": state,
            "error": None,
        }

    # ── Exec / copy / logs ───────────────────────────────────────────────

    def exec_in_container(
        self,
        container: str | Any,
        command: str | list[str],
        detach: bool = False,
        timeout: int | None = None,
    ) -> ExecResult:
        """Execute a command inside a container on the node.

        ``container`` may be a name or an object with a ``name`` — the SDK path
        accepts a ``Container`` and this one used to interpolate whatever it
        was handed straight into a shell command, which for an object is its
        ``repr``.
        """
        if self.node.is_self:
            return _to_exec_result(
                self._local.exec_in_container(
                    container, command, detach=detach, timeout=timeout
                )
            )

        flags = " -d" if detach else ""
        name = container if isinstance(container, str) else getattr(container, "name")
        return _to_exec_result(
            self._exec(
                f"docker exec{flags} {shlex.quote(str(name))} {_argv(command)}",
                timeout=timeout if timeout is not None else 30,
            )
        )

    def copy_to_container(
        self,
        container: str,
        local_path: str,
        remote_path: str,
        timeout: int = 120,
    ) -> bool:
        """Copy a file **or directory** from this machine into a container.

        On a peer the path is staged in ``/tmp`` there first, then
        ``docker cp``-ed into the container and removed.

        The directory case is the one that was broken. ``docker cp`` takes
        files and directories alike, and
        :func:`~spark_pulse.tools.native_runtime._apply_mods` says so in a
        comment while copying every entry of a mod directory — but the peer
        path staged with :meth:`~spark_pulse.tools.ssh.SSHClient.copy`, which
        is ``scp`` with no ``-r``, so a mod with a subdirectory worked on the
        control node and failed on every peer. Directories go over
        :meth:`~spark_pulse.tools.ssh.SSHClient.copy_dir` (rsync, or ``scp -r``
        when there is no rsync), which is the transport's own recursive path.

        Every failure — including an unreachable node — comes back as
        ``False``, on both halves of the peer path. It used to come back as
        ``False`` from the staging copy and as a raised
        :class:`~spark_pulse.tools.ssh.SSHError` from the ``docker cp``, so one
        method answered the same question two ways depending on which half of
        it failed. ``False`` is the whole contract here and it is honest either
        way: unlike an empty container list, it is not a claim about the state
        of the node — only that the file is not in the container. The transport
        detail goes to the log, and both callers
        (:func:`~spark_pulse.tools.native_runtime._deploy_script` and
        ``_apply_mods``) turn ``False`` into a fatal error naming the node.
        """
        if self.node.is_self:
            return self._local.copy_to_container(
                container, local_path, remote_path, timeout=timeout
            )

        basename = local_path.rstrip("/").rsplit("/", 1)[-1]
        staged = f"/tmp/spark-pulse-{container}-{basename}"
        try:
            if os.path.isdir(local_path):
                # rsync and ``scp -r`` both want the destination to exist.
                self._exec(f"mkdir -p {shlex.quote(staged)}", timeout=timeout)
                self._ssh.copy_dir(
                    local_path, self.node.address, staged, timeout=timeout
                )
            else:
                self._ssh.copy(local_path, self.node.address, staged, timeout=timeout)

            result = self._exec(
                f"docker cp {shlex.quote(staged)} "
                f"{shlex.quote(container)}:{shlex.quote(remote_path)} "
                f"&& rm -rf {shlex.quote(staged)}",
                timeout=timeout,
            )
        except Exception as exc:
            logger.error(
                "Failed to copy %s into %s on %s: %s",
                local_path,
                container,
                self.node.label,
                exc,
            )
            return False

        if not result.ok:
            logger.error(
                "docker cp into %s on %s failed: %s",
                container,
                self.node.label,
                result.stderr,
            )
        return bool(result.ok)

    def get_logs(self, name: str, tail: int = 200) -> str:
        """Return the tail of a container's logs on the node.

        ``2>&1`` is the whole point of the redirection. ``docker logs`` writes
        the container's stdout to *its* stdout and the container's stderr to
        *its* stderr, and this used to return ``result.stdout`` alone — so a
        peer's log pane silently dropped every line the engine wrote to stderr,
        which for vLLM and SGLang is most of them. The local path calls
        ``container.logs()``, whose default is ``stdout=True, stderr=True``:
        both streams, interleaved. This is that, over a shell.
        """
        if self.node.is_self:
            return self._local.get_logs(name, tail=tail)

        result = self._exec(
            f"docker logs --tail {int(tail)} {shlex.quote(name)} 2>&1", timeout=30
        )
        if result.ok:
            return result.stdout
        # A failure here used to return "", which reads exactly like a
        # container that started quietly. The local path says the container is
        # not there, and raises for anything else; so does this, and it decides
        # which by asking the daemon rather than by reading its prose.
        if self._daemon_answered():
            return f"Container '{name}' not found"
        raise RuntimeError(
            f"Docker on {self.node.label} did not answer, so the logs of "
            f"'{name}' could not be read: {(result.stderr or '').strip()}"
        )

    # ── Inspection ───────────────────────────────────────────────────────

    def list_managed_containers(
        self,
        labels: dict[str, str] | None = None,
    ) -> list[ContainerInfo]:
        """Managed containers on the node matching ``labels``.

        Raises rather than returning ``[]`` when the node did not answer. An
        empty list is a claim — "there is nothing here" — and this method used
        to make it out of every failure, which is the same "we learned nothing,
        therefore there is nothing" inference that
        :meth:`_inspect_failed_status` exists to refuse, three methods away.
        The consequences were not hypothetical:
        :func:`~spark_pulse.tools.reconciliation._reconcile_clusters_real` and
        ``_reconcile_deployments_real`` rebuild the world from this list, so a
        peer whose daemon had died erased that peer's clusters and deployments
        from the reconciled state; ``_clean_orphaned_containers`` saw nothing to
        clean; and ``native_runtime._stale_names`` concluded there was no
        earlier generation to reap and started a new one **on top of a rank
        that may still hold the GPU** — the failure the reap path exists to
        prevent. The local path lets the exception out, so this does too.
        """
        if self.node.is_self:
            return self._local.list_managed_containers(labels)

        filter_args = f" --filter label={MANAGED_FILTER}"
        for key, value in (labels or {}).items():
            filter_args += f" --filter label={key}" + (f"={value}" if value else "")

        result = self._exec(
            # --no-trunc so container ids are the full 64 hex the SDK path
            # returns; a truncated id compares equal to nothing.
            f"docker ps --all --no-trunc{filter_args} --format '{{{{json .}}}}'",
            timeout=10,
        )
        if not result.ok:
            detail = (result.stderr or "").strip() or "docker did not answer"
            raise RuntimeError(
                f"could not list containers on {self.node.label}: {detail}"
            )

        containers: list[ContainerInfo] = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                info = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            container_labels = _parse_cli_labels(info.get("Labels", ""))
            if not _labels_match(container_labels, labels):
                continue
            containers.append(
                ContainerInfo(
                    id=info.get("ID", ""),
                    # A container can carry several names; ``docker ps`` joins
                    # them with commas and the SDK's ``container.name`` is the
                    # first one.
                    name=str(info.get("Names", "")).split(",")[0],
                    status=_normalize_cli_status(
                        info.get("State") or info.get("Status")
                    ),
                    image=info.get("Image", ""),
                    metadata=ContainerMetadata.from_labels(container_labels),
                    labels=container_labels,
                )
            )
        return containers

    def get_container_by_deployment(self, deployment: str) -> ContainerInfo | None:
        """The container carrying ``deployment``'s label on the node."""
        if self.node.is_self:
            return self._local.get_container_by_deployment(deployment)
        from spark_pulse.tools.labels import DEPLOYMENT_LABEL

        found = self.list_managed_containers({DEPLOYMENT_LABEL: deployment})
        return found[0] if found else None

    def get_container_by_recipe(self, recipe: str) -> list[ContainerInfo]:
        """Every container on the node carrying ``recipe``'s label."""
        if self.node.is_self:
            return self._local.get_container_by_recipe(recipe)
        from spark_pulse.tools.labels import RECIPE_LABEL

        return self.list_managed_containers({RECIPE_LABEL: recipe})

    # ── Images ───────────────────────────────────────────────────────────

    def image_exists(self, ref: str) -> bool:
        """Whether ``ref`` is present on the node."""
        if not ref:
            return False
        if self.node.is_self:
            return self._local.image_exists(ref)
        result = self._exec(
            f"docker image inspect {shlex.quote(ref)} --format '{{{{.Id}}}}'",
            timeout=30,
        )
        return bool(result.ok and result.stdout.strip())

    def image_info(self, ref: str) -> dict[str, Any] | None:
        """Image metadata for ``ref`` on the node, or None when absent."""
        if self.node.is_self:
            return self._local.image_info(ref)
        result = self._exec(
            f"docker image inspect {shlex.quote(ref)} --format '{{{{json .}}}}'",
            timeout=30,
        )
        if not result.ok or not (result.stdout or "").strip():
            return None
        try:
            attrs = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return None
        return _image_info_from_attrs(attrs)

    def list_images(self) -> list[dict[str, Any]]:
        """Every image on the node, shaped exactly like :meth:`image_info`.

        Two commands, not one, and the second is why. ``docker images``
        publishes ``Size`` only as prose — ``26.8GB`` — so this used to
        hardcode ``"size_bytes": 0`` for every image on a peer while the local
        path reported the real number: the Images page showed a fleet where the
        control node had images with a size and every other node had images
        weighing nothing, and disk arithmetic over the fleet was wrong by the
        whole of it. ``docker image inspect`` answers with the same JSON the
        SDK's ``image.attrs`` carries — exact ``Size``, full ``Id``, ISO
        ``Created``, ``RepoTags`` and ``RepoDigests`` — so the peer's rows are
        the local rows, field for field.
        """
        if self.node.is_self:
            return self._local.list_images()
        listing = self._exec("docker image ls --quiet --no-trunc", timeout=60)
        if not listing.ok:
            raise RuntimeError(
                f"could not list images on {self.node.label}: {listing.stderr}"
            )
        ids: list[str] = []
        for line in (listing.stdout or "").splitlines():
            image_id = line.strip()
            # ``docker image ls -q`` repeats an id once per tag.
            if image_id and image_id not in ids:
                ids.append(image_id)
        if not ids:
            return []

        quoted = " ".join(shlex.quote(image_id) for image_id in ids)
        details = self._exec(
            f"docker image inspect {quoted} --format '{{{{json .}}}}'", timeout=60
        )
        if not details.ok:
            raise RuntimeError(
                f"could not inspect images on {self.node.label}: {details.stderr}"
            )
        out: list[dict[str, Any]] = []
        for line in (details.stdout or "").strip().split("\n"):
            if not line.strip():
                continue
            try:
                attrs = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.append(_image_info_from_attrs(attrs))
        return out

    def pull_image(
        self,
        ref: str,
        progress: Any | None = None,
        interval: float = PULL_PROGRESS_INTERVAL,
        cancel: Callable[[], bool] | None = None,
        stall_timeout: float | None = None,
    ) -> dict[str, Any]:
        """Pull ``ref`` onto the node.

        On the control node this is the SDK path, with real per-layer
        aggregation, cancellation and the stall watchdog. Over SSH the docker
        CLI runs non-interactively as one blocking command, so ``interval`` and
        ``stall_timeout`` have nothing to act on: a single terminal snapshot is
        reported when the pull finishes. Fetch-once on the control node
        followed by a fan-out is the plan's answer to that, and it is later
        work.

        ``cancel`` is honoured, at the only two moments this shape has one: on
        entry, and when the blocking pull returns. That is coarse — a cancel
        raised mid-pull is not seen until the pull finishes — but the outcome
        is the one that matters. ``native_runtime.start`` catches
        :class:`~spark_pulse.tools.docker.PullCancelled` to record a deployment
        torn down during its own image pull as ``stopped``; without it that
        handler could only ever fire for the control node, and a teardown
        during a peer's pull was recorded as ``error`` — the exact
        miscategorisation the handler exists to prevent.
        """
        if not ref:
            raise RuntimeError("pull_image needs an image reference")
        if self.node.is_self:
            return self._local.pull_image(
                ref,
                progress,
                interval=interval,
                cancel=cancel,
                stall_timeout=stall_timeout,
            )

        if cancel is not None and cancel():
            raise PullCancelled(f"pull of {ref} cancelled")
        repo, tag = split_ref(ref)
        result = self._exec(f"docker pull {shlex.quote(ref)}", timeout=7200)
        if cancel is not None and cancel():
            raise PullCancelled(f"pull of {ref} cancelled")
        if not result.ok:
            raise RuntimeError(
                f"docker pull {ref} failed on {self.node.label}: "
                f"{(result.stderr or result.stdout or '').strip()}"
            )
        info = self.image_info(ref) or {}
        size = int(info.get("size_bytes") or 0)
        snapshot = {
            "ref": ref,
            "status": "pull complete",
            "layers": 0,
            "bytes_done": size,
            "bytes_total": size,
            "percent": 100.0,
        }
        if progress is not None:
            progress(snapshot)
        return {
            "ref": ref,
            "repository": repo,
            "tag": tag,
            "bytes_done": size,
            "bytes_total": size,
            "percent": 100.0,
            "id": str(info.get("id") or ""),
            "size_bytes": size,
        }

    def remove_image(self, ref: str, force: bool = False) -> bool:
        """Remove ``ref`` from the node. False when it was not there."""
        if self.node.is_self:
            return self._local.remove_image(ref, force=force)
        flag = " -f" if force else ""
        result = self._exec(f"docker rmi{flag} {shlex.quote(ref)}", timeout=120)
        if result.ok:
            return True
        text = (result.stderr or "").lower()
        if "no such image" in text or "not found" in text:
            return False
        raise RuntimeError(
            f"could not remove image {ref} on {self.node.label}: {result.stderr}"
        )


# ── The resolver ─────────────────────────────────────────────────────────────


def service_for(
    node: Node,
    *,
    ssh_client: SSHClient | None = None,
    docker_service: DockerService | None = None,
) -> NodeService:
    """The container service bound to ``node``.

    The control node gets the process-wide :class:`DockerService` — one
    service, thread-local clients — and a peer gets a :class:`RemoteNodeService`
    over SSH. ``spark_pulse.mock.node_service`` overrides this function to hand
    back the simulation service instead, so simulation mode is one swap at the
    resolver rather than a branch at every call site.
    """
    if node.is_self:
        if docker_service is not None:
            return docker_service
        from spark_pulse.tools.docker import _get_service

        return _get_service()
    return RemoteNodeService(node, ssh_client=ssh_client, docker_service=None)


class NodeServices:
    """A resolver that remembers the service it built for each node.

    Callers hold one of these instead of a service, because they act on
    several nodes: the orchestrator, the health validator and the Ray manager
    all take one. It is a plain callable, so a test can pass a lambda.
    """

    def __init__(
        self,
        ssh_client: SSHClient | None = None,
        docker_service: DockerService | None = None,
        resolver: Callable[..., NodeService] | None = None,
    ):
        self._ssh_client = ssh_client
        self._docker_service = docker_service
        self._resolver = resolver or service_for
        self._cache: dict[str, NodeService] = {}

    def __call__(self, node: Node) -> NodeService:
        """Return (and cache) the service bound to ``node``."""
        key = f"{node.id}\0{node.address}\0{node.is_self}"
        service = self._cache.get(key)
        if service is None:
            service = self._resolver(
                node,
                ssh_client=self._ssh_client,
                docker_service=self._docker_service,
            )
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
    "DAEMON_PROBE_COMMAND",
    "DOCKER_STATES",
    "LOOPBACK_ADDRESSES",
    "NODE_SERVICE_METHODS",
    "STATUS_PROBE_TIMEOUT",
    "Node",
    "NodeService",
    "NodeServices",
    "RemoteNodeService",
    "control_node",
    "is_local_address",
    "local_addresses",
    "node_for",
    "peer_node",
    "reset_local_addresses",
    "run_kwargs_from_docker_config",
    "service_for",
]
