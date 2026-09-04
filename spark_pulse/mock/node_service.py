"""Mock node services — the real implementations over simulated transports.

This module used to be ``spark_pulse.mock.remote_docker``: a second, hand-
written copy of the remote container service that had drifted from the real
one (different label matching, no image-info shape, a ``ray status`` answer
baked into ``exec``). A parallel implementation cannot catch a bug in the
implementation it is standing in for.

So there is no parallel implementation any more. Simulation runs the *real*
:class:`~spark_pulse.tools.node_service.RemoteNodeService` with a simulated
SSH transport underneath it, exactly the way ``mock/native_runtime.py`` runs
the real native runtime over the mock container service. The command building,
the label filtering, the JSON parsing and the local/peer branch are all the
production code; only the bytes on the wire are invented.

Only the resolver differs from the real module: the control node gets
:class:`~spark_pulse.mock.docker.MockDockerService`, and a peer gets the real
remote service over :class:`SimulatedDockerSSHClient`.
"""

from __future__ import annotations

import json
import shlex
import uuid
from typing import Any, Callable

from spark_pulse.tools.docker import DockerService
from spark_pulse.tools.node_service import (
    CONTROL_NODE_ID as CONTROL_NODE_ID,
    LOOPBACK_ADDRESSES as LOOPBACK_ADDRESSES,
    NODE_SERVICE_METHODS as NODE_SERVICE_METHODS,
    Node as Node,
    NodeService as NodeService,
    NodeServices as _NodeServices,
    RemoteNodeService as RemoteNodeService,
    control_node as control_node,
    is_local_address as is_local_address,
    local_addresses as local_addresses,
    node_for as node_for,
    peer_node as peer_node,
    reset_local_addresses as reset_local_addresses,
    run_kwargs_from_docker_config as run_kwargs_from_docker_config,
)
from spark_pulse.tools.ssh import SSHClient, SSHResult

DEFAULT_IMAGE_SIZE = 26_843_545_600


def _flag_value(parts: list[str], flag: str) -> str:
    """Value following ``flag`` in an argv list, or ""."""
    if flag in parts:
        index = parts.index(flag)
        if index + 1 < len(parts):
            return parts[index + 1].strip("'\"")
    return ""


def _label_filter_matches(labels: dict[str, str], term: str) -> bool:
    """Whether ``labels`` satisfies one ``docker ps --filter label=`` term.

    ``key`` matches presence; ``key=value`` matches the value — the same rule
    the daemon applies.
    """
    key, sep, value = term.partition("=")
    if not sep:
        return key in labels
    return labels.get(key) == value


class SimulatedDockerSSHClient(SSHClient):
    """An SSH transport that answers the docker CLI out of memory.

    One store per host, so a container started on ``10.0.0.2`` is invisible on
    ``10.0.0.3`` — which is the property the empty-host bug destroyed and the
    property a simulation has to preserve to be worth anything.

    Every command it is asked to run is recorded in :attr:`commands`, so a test
    can assert that an operation aimed at a peer actually left the machine.
    """

    def __init__(
        self,
        images: dict[str, int] | None = None,
        fail_hosts: list[str] | None = None,
    ):
        """Simulate docker over SSH.

        Args:
            images: Image references every host starts with, mapped to size in
                bytes.
            fail_hosts: Hosts whose every command fails, as an unreachable
                node would.
        """
        self.commands: list[dict[str, Any]] = []
        self.copies: list[dict[str, Any]] = []
        #: Per-host container environment, so the NCCL consistency check has
        #: something that can genuinely differ between nodes.
        self.env: dict[str, dict[str, str]] = {}
        self._containers: dict[str, dict[str, dict[str, Any]]] = {}
        self._images: dict[str, dict[str, dict[str, Any]]] = {}
        self._seed_images = dict(images or {})
        self._fail_hosts = set(fail_hosts or [])

    # ── Store ────────────────────────────────────────────────────────────

    def containers_on(self, host: str) -> dict[str, dict[str, Any]]:
        """The container store for ``host``, created on first touch."""
        return self._containers.setdefault(host, {})

    def images_on(self, host: str) -> dict[str, dict[str, Any]]:
        """The image store for ``host``, seeded on first touch."""
        store = self._images.get(host)
        if store is None:
            store = {
                ref: {
                    "Id": f"sha256:{uuid.uuid5(uuid.NAMESPACE_URL, ref).hex}",
                    "Size": size,
                    "Created": "2026-01-01T00:00:00Z",
                    "RepoTags": [ref],
                    "RepoDigests": [],
                }
                for ref, size in self._seed_images.items()
            }
            self._images[host] = store
        return store

    def env_on(self, host: str) -> dict[str, str]:
        """The simulated container environment on ``host``."""
        return self.env.setdefault(host, {"NCCL_SOCKET_IFNAME": "eth0"})

    def hosts_seen(self) -> list[str]:
        """Every host this client was asked to reach, in order."""
        seen: list[str] = []
        for entry in self.commands + self.copies:
            if entry["host"] not in seen:
                seen.append(entry["host"])
        return seen

    # ── SSHClient ────────────────────────────────────────────────────────

    def exec(
        self,
        host: str,
        command: str,
        timeout: int = 30,
        batch_mode: bool = True,
    ) -> SSHResult:
        """Answer a docker CLI invocation for ``host``."""
        self.commands.append({"host": host, "command": command, "timeout": timeout})
        if host in self._fail_hosts:
            return SSHResult(returncode=255, stdout="", stderr=f"unreachable: {host}")

        parts = shlex.split(command.replace("&&", "\n").split("\n")[0])
        if len(parts) < 2 or parts[0] != "docker":
            return SSHResult(returncode=127, stdout="", stderr=f"not docker: {command}")

        verb = parts[1]
        handler = getattr(self, f"_docker_{verb.replace('-', '_')}", None)
        if handler is None:
            return SSHResult(returncode=1, stdout="", stderr=f"unsupported: {command}")
        return handler(host, parts, command)

    def remote_shell_command(
        self, host: str, remote_command: str | None = None
    ) -> list[str]:
        """Argv that would run ``remote_command`` on ``host``."""
        args = ["ssh", "-o", "BatchMode=yes", host]
        if remote_command:
            args.append(remote_command)
        return args

    def copy(
        self,
        local_path: str,
        host: str,
        remote_path: str,
        timeout: int = 30,
    ) -> None:
        """Record a file transfer to ``host``."""
        if host in self._fail_hosts:
            raise OSError(f"unreachable: {host}")
        self.copies.append({"host": host, "local": local_path, "remote": remote_path})

    def copy_dir(
        self,
        local_dir: str,
        host: str,
        remote_dir: str,
        timeout: int = 60,
    ) -> None:
        """Record a directory transfer to ``host``."""
        self.copy(local_dir, host, remote_dir, timeout=timeout)

    # ── docker verbs ─────────────────────────────────────────────────────

    def _docker_run(self, host: str, parts: list[str], _raw: str) -> SSHResult:
        name = _flag_value(parts, "--name")
        labels: dict[str, str] = {}
        for index, part in enumerate(parts):
            if part == "--label" and index + 1 < len(parts):
                key, _, value = parts[index + 1].strip("'\"").partition("=")
                labels[key] = value
        record = {
            "ID": f"id-{name}",
            "Names": name,
            "Image": parts[-1],
            "State": "running",
            "Labels": ",".join(f"{k}={v}" for k, v in labels.items()),
        }
        self.containers_on(host)[name] = record
        return SSHResult(returncode=0, stdout=f"id-{name}\n", stderr="")

    def _docker_ps(self, host: str, parts: list[str], _raw: str) -> SSHResult:
        wanted: list[str] = [
            parts[index + 1]
            for index, part in enumerate(parts[:-1])
            if part == "--filter" and parts[index + 1].startswith("label=")
        ]
        records = []
        for record in self.containers_on(host).values():
            labels = dict(
                pair.split("=", 1)
                for pair in record["Labels"].split(",")
                if "=" in pair
            )
            if all(_label_filter_matches(labels, term[6:]) for term in wanted):
                records.append(record)
        lines = "\n".join(json.dumps(c) for c in records)
        return SSHResult(returncode=0, stdout=lines, stderr="")

    def _docker_stop(self, host: str, parts: list[str], _raw: str) -> SSHResult:
        name = parts[-1]
        if name not in self.containers_on(host):
            return SSHResult(returncode=1, stdout="", stderr="No such container")
        del self.containers_on(host)[name]
        return SSHResult(returncode=0, stdout=name, stderr="")

    def _docker_exec(self, host: str, parts: list[str], _raw: str) -> SSHResult:
        argv = [p for p in parts[2:] if not p.startswith("-")]
        name = argv[0] if argv else ""
        if name not in self.containers_on(host):
            return SSHResult(returncode=1, stdout="", stderr="No such container")
        inner = " ".join(parts[parts.index(name) + 1 :])
        if "ray" in inner and "status" in inner:
            return SSHResult(returncode=0, stdout="Cluster is ready. OK", stderr="")
        if "nvidia-smi" in inner:
            return SSHResult(returncode=0, stdout="1", stderr="")
        if inner.strip() == "env":
            body = "\n".join(f"{k}={v}" for k, v in self.env_on(host).items())
            return SSHResult(returncode=0, stdout=body, stderr="")
        return SSHResult(returncode=0, stdout=inner, stderr="")

    def _docker_cp(self, host: str, _parts: list[str], _raw: str) -> SSHResult:
        _ = host
        return SSHResult(returncode=0, stdout="", stderr="")

    def _docker_logs(self, host: str, parts: list[str], _raw: str) -> SSHResult:
        name = parts[-1]
        if name not in self.containers_on(host):
            return SSHResult(returncode=1, stdout="", stderr="No such container")
        return SSHResult(returncode=0, stdout=f"[simulated logs for {name}]", stderr="")

    def _docker_inspect(self, host: str, parts: list[str], _raw: str) -> SSHResult:
        name = parts[-1]
        record = self.containers_on(host).get(name)
        if record is None:
            return SSHResult(returncode=1, stdout="", stderr="No such object")
        state = {"Running": record["State"] == "running", "Status": record["State"]}
        return SSHResult(returncode=0, stdout=json.dumps(state), stderr="")

    def _docker_image(self, host: str, parts: list[str], raw: str) -> SSHResult:
        if len(parts) < 3 or parts[2] != "inspect":
            return SSHResult(returncode=1, stdout="", stderr=f"unsupported: {raw}")
        ref = parts[3]
        entry = self.images_on(host).get(ref)
        if entry is None:
            return SSHResult(returncode=1, stdout="", stderr=f"No such image: {ref}")
        if ".Id" in raw:
            return SSHResult(returncode=0, stdout=f"{entry['Id']}\n", stderr="")
        if ".Size" in raw:
            return SSHResult(returncode=0, stdout=f"{entry['Size']}\n", stderr="")
        return SSHResult(returncode=0, stdout=json.dumps(entry), stderr="")

    def _docker_images(self, host: str, _parts: list[str], _raw: str) -> SSHResult:
        lines = []
        for ref, entry in self.images_on(host).items():
            repository, _, tag = ref.rpartition(":")
            lines.append(
                json.dumps(
                    {
                        "ID": entry["Id"],
                        "Repository": repository or ref,
                        "Tag": tag or "latest",
                        "CreatedAt": entry["Created"],
                        "Digest": "",
                    }
                )
            )
        return SSHResult(returncode=0, stdout="\n".join(lines), stderr="")

    def _docker_pull(self, host: str, parts: list[str], _raw: str) -> SSHResult:
        ref = parts[2]
        repository, _, digest = ref.partition("@")
        # A digest-pinned pull is content-addressed, so every node that pulls
        # it ends up with the *same* image ID and the digest it asked for.
        # Inventing a per-node ID here would hide the very property the
        # registry path exists to preserve.
        image_id = (
            f"sha256:{digest.partition(':')[2]}"
            if digest
            else f"sha256:{uuid.uuid5(uuid.NAMESPACE_URL, ref).hex}"
        )
        self.images_on(host)[ref] = {
            "Id": image_id,
            "Size": DEFAULT_IMAGE_SIZE,
            "Created": "2026-01-01T00:00:00Z",
            "RepoTags": [] if digest else [ref],
            "RepoDigests": [f"{repository}@{digest}"] if digest else [],
        }
        return SSHResult(returncode=0, stdout=f"Downloaded {ref}\n", stderr="")

    def _docker_rmi(self, host: str, parts: list[str], _raw: str) -> SSHResult:
        ref = parts[-1]
        if self.images_on(host).pop(ref, None) is None:
            return SSHResult(returncode=1, stdout="", stderr=f"No such image: {ref}")
        return SSHResult(returncode=0, stdout=f"Untagged: {ref}", stderr="")


_default_ssh_client: SimulatedDockerSSHClient | None = None


def default_ssh_client() -> SimulatedDockerSSHClient:
    """The process-wide simulated SSH transport."""
    global _default_ssh_client
    if _default_ssh_client is None:
        _default_ssh_client = SimulatedDockerSSHClient()
    return _default_ssh_client


def reset() -> None:
    """Drop the simulated transport, and with it every simulated node."""
    global _default_ssh_client
    _default_ssh_client = None


def service_for(
    node: Node,
    *,
    ssh_client: SSHClient | None = None,
    docker_service: DockerService | None = None,
) -> NodeService:
    """The simulated container service bound to ``node``.

    The control node gets the in-memory Docker service every other simulated
    tool already shares. A peer gets the *real* remote service over a
    simulated SSH transport, so the command building and parsing under test
    are the production ones.
    """
    if node.is_self:
        if docker_service is not None:
            return docker_service
        from spark_pulse.mock.docker import _get_service

        return _get_service()
    return RemoteNodeService(
        node,
        ssh_client=ssh_client or default_ssh_client(),
        docker_service=None,
    )


class NodeServices(_NodeServices):
    """The real resolver cache, defaulting to the simulated resolver."""

    def __init__(
        self,
        ssh_client: SSHClient | None = None,
        docker_service: DockerService | None = None,
        resolver: Callable[..., NodeService] | None = None,
    ):
        super().__init__(
            ssh_client=ssh_client,
            docker_service=docker_service,
            resolver=resolver or service_for,
        )


__all__ = [
    "CONTROL_NODE_ID",
    "DEFAULT_IMAGE_SIZE",
    "LOOPBACK_ADDRESSES",
    "NODE_SERVICE_METHODS",
    "Node",
    "NodeService",
    "NodeServices",
    "RemoteNodeService",
    "SimulatedDockerSSHClient",
    "control_node",
    "default_ssh_client",
    "is_local_address",
    "local_addresses",
    "node_for",
    "peer_node",
    "reset",
    "reset_local_addresses",
    "run_kwargs_from_docker_config",
    "service_for",
]
