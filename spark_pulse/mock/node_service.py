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
import os
import shlex
import uuid
from typing import Any, Callable

from spark_pulse.tools.docker import DockerService
from spark_pulse.tools.node_service import (
    CONTROL_NODE_ID as CONTROL_NODE_ID,
    DAEMON_PROBE_COMMAND as DAEMON_PROBE_COMMAND,
    DOCKER_STATES as DOCKER_STATES,
    LOOPBACK_ADDRESSES as LOOPBACK_ADDRESSES,
    NODE_SERVICE_METHODS as NODE_SERVICE_METHODS,
    STATUS_PROBE_TIMEOUT as STATUS_PROBE_TIMEOUT,
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
from spark_pulse.tools.ssh import SSHClient, SSHError, SSHErrorType, SSHResult

DEFAULT_IMAGE_SIZE = 26_843_545_600


def _flag_value(parts: list[str], flag: str) -> str:
    """Value following ``flag`` in an argv list, or ""."""
    if flag in parts:
        index = parts.index(flag)
        if index + 1 < len(parts):
            return parts[index + 1].strip("'\"")
    return ""


def _container_id(host: str, name: str) -> str:
    """A stable, full-length container id for ``name`` on ``host``.

    Docker's ids are 64 hex characters and ``docker run -d`` prints all of
    them; a 12-character stand-in would have hidden the truncation the real
    ``docker ps`` applies unless asked for ``--no-trunc``.
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{host}/{name}").hex * 2


def _top_level_clauses(command: str) -> list[str]:
    """Split a command on ``&&`` that a shell would act on.

    Quote-aware, and it has to be: ``_apply_mods`` runs
    ``docker exec … bash -lc 'cd /mods/x && … bash run.sh'``, whose ``&&``
    belongs to the *inner* shell and is one argument as far as the outer one is
    concerned. Splitting on it blindly produces two halves of a quoted string
    and ``shlex`` refuses both.
    """
    clauses: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(command):
        character = command[index]
        if quote:
            current.append(character)
            if character == quote:
                quote = ""
        elif character in "'\"":
            quote = character
            current.append(character)
        elif command.startswith("&&", index):
            clauses.append("".join(current))
            current = []
            index += 2
            continue
        else:
            current.append(character)
        index += 1
    clauses.append("".join(current))
    return [clause.strip() for clause in clauses if clause.strip()]


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

    :attr:`fail_hosts` is the unreachable set, and it is a plain mutable set on
    purpose: a node can be made unreachable *during* an operation, which is the
    failure the gang semantics exist for. A host in it raises
    :class:`~spark_pulse.tools.ssh.SSHError`, exactly as ``OpenSSHClient`` does
    on ssh's own exit 255 — never a non-zero :class:`SSHResult`. That
    distinction is the whole contract: an ``SSHResult`` means the node answered
    and the outcome is definite, so returning one for a dead node would let
    ``docker inspect`` read as "no such container" and a rank that is still
    holding a GPU be confirmed gone on inference rather than on evidence.

    :attr:`daemon_down_hosts` is the other half of that contract, and it is a
    different failure entirely: the node answers SSH perfectly well and its
    Docker daemon does not answer *it*. Real docker exits 1 with ``Cannot
    connect to the Docker daemon`` — the same exit code a container that does
    not exist produces — so a host in this set is how a test reaches the one
    case where the exit status alone cannot tell "gone" from "unknown".
    """

    def __init__(
        self,
        images: dict[str, int] | None = None,
        fail_hosts: list[str] | None = None,
        daemon_down_hosts: list[str] | None = None,
    ):
        """Simulate docker over SSH.

        Args:
            images: Image references every host starts with, mapped to size in
                bytes.
            fail_hosts: Hosts that are unreachable. Mutate :attr:`fail_hosts`
                to make a node go away mid-operation.
            daemon_down_hosts: Hosts that are reachable but whose Docker
                daemon is not. Mutate :attr:`daemon_down_hosts` to kill a
                daemon mid-operation.
        """
        self.commands: list[dict[str, Any]] = []
        self.copies: list[dict[str, Any]] = []
        #: Per-host container environment, so the NCCL consistency check has
        #: something that can genuinely differ between nodes.
        self.env: dict[str, dict[str, str]] = {}
        #: Per-host directories a ``mkdir -p`` was asked for, in order.
        self.directories: dict[str, list[str]] = {}
        self._containers: dict[str, dict[str, dict[str, Any]]] = {}
        self._images: dict[str, dict[str, dict[str, Any]]] = {}
        self._seed_images = dict(images or {})
        #: Hosts that are unreachable right now. Mutable by design.
        self.fail_hosts: set[str] = set(fail_hosts or [])
        #: Hosts reachable over SSH whose Docker daemon is down. Mutable too.
        self.daemon_down_hosts: set[str] = set(daemon_down_hosts or [])

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

    def _raise_if_unreachable(self, host: str) -> None:
        """Fail the way the real transport does when the node is gone."""
        if host in self.fail_hosts:
            raise SSHError(
                error_type=SSHErrorType.NETWORK,
                host=host,
                message=f"unreachable: {host}",
                stderr=f"ssh: connect to host {host}: No route to host",
            )

    def exec(
        self,
        host: str,
        command: str,
        timeout: int = 30,
        batch_mode: bool = True,
    ) -> SSHResult:
        """Answer a docker CLI invocation for ``host``.

        ``&&`` is honoured rather than ignored. This used to keep only the text
        before the first ``&&`` and answer that, which meant the simulation
        could not see the difference between ``docker stop x`` and ``docker
        stop x && docker rm -f x`` — the difference that leaked an orphan on
        every peer teardown in production while every test here passed. Now
        each clause runs in turn, a non-zero clause short-circuits the rest the
        way a shell's ``&&`` does, and the outputs are concatenated.
        """
        self.commands.append({"host": host, "command": command, "timeout": timeout})
        self._raise_if_unreachable(host)

        stdout: list[str] = []
        result = SSHResult(returncode=0, stdout="", stderr="")
        for clause in _top_level_clauses(command):
            result = self._exec_one(host, clause)
            if result.stdout:
                stdout.append(result.stdout)
            if result.returncode != 0:
                return SSHResult(
                    returncode=result.returncode,
                    stdout="".join(stdout),
                    stderr=result.stderr,
                )
        return SSHResult(
            returncode=result.returncode, stdout="".join(stdout), stderr=result.stderr
        )

    def _exec_one(self, host: str, command: str) -> SSHResult:
        """Answer a single command — one clause of what :meth:`exec` was given."""
        # ``2>&1`` is a shell redirection, not an argument: the container
        # service appends it to ``docker logs`` so a peer's stderr reaches the
        # log pane. Only a *trailing* one is ours — a ``2>&1`` inside a quoted
        # ``bash -lc`` belongs to the inner shell and stays in the argument.
        merged = command.rstrip().endswith("2>&1")
        if merged:
            command = command.rstrip()[: -len("2>&1")]
        parts = shlex.split(command)
        if not parts:
            return SSHResult(returncode=0, stdout="", stderr="")
        if parts[:2] == ["mkdir", "-p"]:
            # Upstream creates the cache bind sources before ``docker run``
            # (launch-cluster.sh line 1104). Simulation records the paths
            # rather than making directories on a developer's machine.
            self.directories.setdefault(host, []).extend(parts[2:])
            return SSHResult(returncode=0, stdout="", stderr="")
        if parts[0] == "rm":
            # Staging cleanup after ``docker cp``; nothing was ever written.
            return SSHResult(returncode=0, stdout="", stderr="")
        if len(parts) < 2 or parts[0] != "docker":
            return SSHResult(returncode=127, stdout="", stderr=f"not docker: {command}")

        if host in self.daemon_down_hosts:
            # Exactly what the CLI does when the socket is not answering, exit
            # code included: 1, the same code `no such object` uses. Measured
            # on a GB10 running Docker 29.2.1, docs/rank-state-transport.md
            # §2.4. Every docker verb fails this way, `version` included —
            # which is what makes the daemon probe a real probe.
            return SSHResult(
                returncode=1,
                stdout="",
                stderr=(
                    "Cannot connect to the Docker daemon at "
                    "unix:///var/run/docker.sock. Is the docker daemon running?"
                ),
            )

        verb = parts[1]
        handler = getattr(self, f"_docker_{verb.replace('-', '_')}", None)
        if handler is None:
            return SSHResult(returncode=1, stdout="", stderr=f"unsupported: {command}")
        answer = handler(host, parts, command)
        if merged and answer.stderr and not answer.returncode:
            # ``2>&1``: the container's stderr arrives on stdout.
            return SSHResult(
                returncode=0, stdout=answer.stdout + answer.stderr, stderr=""
            )
        return answer

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
        """Record a file transfer to ``host``.

        A directory is **refused**, because ``scp`` without ``-r`` refuses one:
        ``not a regular file``. This class used to accept anything, which is
        why nothing noticed that ``copy_to_container`` staged mod directories
        through the non-recursive path — a mod with a subdirectory worked on
        the control node and failed on every peer, and the simulation agreed
        with the control node.
        """
        self._raise_if_unreachable(host)
        if os.path.isdir(local_path):
            raise RuntimeError(f"scp: {local_path}: not a regular file")
        self.copies.append(
            {
                "host": host,
                "local": local_path,
                "remote": remote_path,
                "recursive": False,
            }
        )

    def copy_dir(
        self,
        local_dir: str,
        host: str,
        remote_dir: str,
        timeout: int = 60,
    ) -> None:
        """Record a directory transfer to ``host``."""
        self._raise_if_unreachable(host)
        self.copies.append(
            {
                "host": host,
                "local": local_dir,
                "remote": remote_dir,
                "recursive": True,
            }
        )

    # ── docker verbs ─────────────────────────────────────────────────────

    def _docker_run(self, host: str, parts: list[str], _raw: str) -> SSHResult:
        name = _flag_value(parts, "--name")
        labels: dict[str, str] = {}
        for index, part in enumerate(parts):
            if part == "--label" and index + 1 < len(parts):
                key, _, value = parts[index + 1].strip("'\"").partition("=")
                labels[key] = value
        container_id = _container_id(host, name)
        record = {
            "ID": container_id,
            "Names": name,
            "Image": parts[-1],
            "State": "running",
            "Labels": ",".join(f"{k}={v}" for k, v in labels.items()),
        }
        self.containers_on(host)[name] = record
        # ``docker run -d`` prints the full 64-hex id, which is what the SDK
        # path's ``container.id`` is, so a cross-node id comparison is a
        # comparison of like with like.
        return SSHResult(returncode=0, stdout=f"{container_id}\n", stderr="")

    def _docker_ps(self, host: str, parts: list[str], _raw: str) -> SSHResult:
        wanted: list[str] = [
            parts[index + 1]
            for index, part in enumerate(parts[:-1])
            if part == "--filter" and parts[index + 1].startswith("label=")
        ]
        show_all = "--all" in parts or "-a" in parts
        truncate = "--no-trunc" not in parts
        records = []
        for record in self.containers_on(host).values():
            if not show_all and record["State"] != "running":
                continue
            labels = dict(
                pair.split("=", 1)
                for pair in record["Labels"].split(",")
                if "=" in pair
            )
            if all(_label_filter_matches(labels, term[6:]) for term in wanted):
                row = dict(record)
                if truncate:
                    # Without ``--no-trunc`` the CLI prints 12 hex characters.
                    row["ID"] = row["ID"][:12]
                records.append(row)
        lines = "\n".join(json.dumps(c) for c in records)
        return SSHResult(returncode=0, stdout=lines, stderr="")

    def _docker_stop(self, host: str, parts: list[str], _raw: str) -> SSHResult:
        """Stop a container — and *keep* it, which is what docker does.

        This used to delete the record, so simulation could not witness the
        difference between stopping a container and removing one. Production
        stopped without removing on every peer, the stopped container went on
        answering ``docker inspect``, ``_is_confirmed_gone`` never confirmed
        it, and every multi-node teardown leaked an orphan holding its ports —
        while this class quietly made the tests pass.
        """
        name = parts[-1]
        record = self.containers_on(host).get(name)
        if record is None:
            return SSHResult(returncode=1, stdout="", stderr="No such container")
        record["State"] = "exited"
        return SSHResult(returncode=0, stdout=name, stderr="")

    def _docker_rm(self, host: str, parts: list[str], _raw: str) -> SSHResult:
        """Remove a container. Without ``-f``, a running one is refused."""
        name = parts[-1]
        record = self.containers_on(host).get(name)
        if record is None:
            return SSHResult(
                returncode=1, stdout="", stderr=f"Error: No such container: {name}"
            )
        forced = "-f" in parts or "--force" in parts
        if record["State"] == "running" and not forced:
            return SSHResult(
                returncode=1,
                stdout="",
                stderr=(
                    f"Error response from daemon: cannot remove container "
                    f"{name}: container is running"
                ),
            )
        del self.containers_on(host)[name]
        return SSHResult(returncode=0, stdout=f"{name}\n", stderr="")

    def _docker_exec(self, host: str, parts: list[str], _raw: str) -> SSHResult:
        argv = [p for p in parts[2:] if not p.startswith("-")]
        name = argv[0] if argv else ""
        if name not in self.containers_on(host):
            return SSHResult(returncode=1, stdout="", stderr="No such container")
        if "-d" in parts or "--detach" in parts:
            # ``docker exec -d`` prints nothing and returns at once, which is
            # what the SDK path's detached branch also returns.
            return SSHResult(returncode=0, stdout="", stderr="")
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
        # Engines write most of their output to stderr, so the simulated log
        # has a line on each stream: a caller that reads only stdout loses
        # half of it, which is what the peer path used to do.
        return SSHResult(
            returncode=0,
            stdout=f"[simulated logs for {name}]",
            stderr=f"[simulated stderr for {name}]",
        )

    def _docker_version(self, host: str, _parts: list[str], _raw: str) -> SSHResult:
        """The daemon-liveness probe. Reached only when the daemon is up.

        A host in :attr:`daemon_down_hosts` never gets here — ``exec`` fails
        every verb for it — which is the whole point: this answers only when
        there is a daemon to answer.
        """
        _ = host
        return SSHResult(returncode=0, stdout="29.2.1\n", stderr="")

    def _docker_inspect(self, host: str, parts: list[str], raw: str) -> SSHResult:
        name = parts[-1]
        record = self.containers_on(host).get(name)
        if record is None:
            # The real message, so a test that asserts on the error text is
            # asserting against what the CLI actually says.
            return SSHResult(
                returncode=1, stdout="", stderr=f"Error: No such object: {name}"
            )
        state = {"Running": record["State"] == "running", "Status": record["State"]}
        body = json.dumps(state)
        if ".Id" in raw:
            # ``--format '{{json .State}}\t{{.Id}}'``: both facts, one inspect.
            body = f"{body}\t{record['ID']}"
        return SSHResult(returncode=0, stdout=body, stderr="")

    def _docker_image(self, host: str, parts: list[str], raw: str) -> SSHResult:
        if len(parts) < 3:
            return SSHResult(returncode=1, stdout="", stderr=f"unsupported: {raw}")
        if parts[2] in ("ls", "list"):
            return self._docker_image_ls(host, parts, raw)
        if parts[2] != "inspect":
            return SSHResult(returncode=1, stdout="", stderr=f"unsupported: {raw}")

        refs = [part for part in parts[3:] if not part.startswith("-")]
        # ``--format`` takes the token after it; drop that one.
        for index, part in enumerate(parts):
            if part == "--format" and index + 1 < len(parts):
                refs = [ref for ref in refs if ref != parts[index + 1]]
        store = self.images_on(host)
        by_id = {entry["Id"]: entry for entry in store.values()}
        found = []
        for ref in refs:
            entry = store.get(ref) or by_id.get(ref)
            if entry is None:
                return SSHResult(
                    returncode=1, stdout="", stderr=f"No such image: {ref}"
                )
            found.append(entry)
        if not found:
            return SSHResult(returncode=1, stdout="", stderr="No such image")
        if ".Id" in raw:
            return SSHResult(
                returncode=0,
                stdout="".join(f"{entry['Id']}\n" for entry in found),
                stderr="",
            )
        if ".Size" in raw:
            return SSHResult(
                returncode=0,
                stdout="".join(f"{entry['Size']}\n" for entry in found),
                stderr="",
            )
        return SSHResult(
            returncode=0,
            stdout="\n".join(json.dumps(entry) for entry in found),
            stderr="",
        )

    def _docker_image_ls(self, host: str, parts: list[str], _raw: str) -> SSHResult:
        """``docker image ls -q``: one id per line, repeated per tag."""
        quiet = "--quiet" in parts or "-q" in parts
        truncate = "--no-trunc" not in parts
        if not quiet:
            return self._docker_images(host, parts, _raw)
        lines = []
        for entry in self.images_on(host).values():
            image_id = entry["Id"]
            # Truncated, the CLI prints 12 hex with no ``sha256:`` prefix.
            lines.append(image_id.partition(":")[2][:12] if truncate else image_id)
        return SSHResult(returncode=0, stdout="\n".join(lines), stderr="")

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
    "DOCKER_STATES",
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
