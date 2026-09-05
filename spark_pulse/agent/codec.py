"""The one place protocol messages and Python shapes meet.

Both directions of every conversion live here, next to each other, because the
failure this whole package exists to prevent is two halves of one contract
drifting apart in different files. ``docs/transport-reexamined.md`` counted
thirty semantic divergences across fifteen methods between two implementations
of ``NodeService``; the agent's answer is that there is now one implementation
of every operation and one file that translates it.

The rule the encoders follow, without exception: **only encode what the caller
actually passed.** Every kwarg whose Python default is not the proto3 zero
value is an ``optional`` field, and an encoder leaves it unset when the caller
did not supply it. The decoder then builds a kwargs dict from present fields
only and lets ``DockerService`` apply its own default. No default value is
written down twice, so no default can drift.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.tools.docker import ContainerInfo, ContainerMetadata, ExecResult

__all__ = [
    "encode_cmd",
    "decode_cmd",
    "encode_metadata",
    "decode_metadata",
    "encode_container_info",
    "decode_container_info",
    "encode_container_status",
    "decode_container_status",
    "encode_exec_result",
    "decode_exec_result",
    "encode_image_info",
    "decode_image_info",
    "encode_pull_outcome",
    "decode_pull_outcome",
    "encode_pull_progress",
    "decode_pull_progress",
    "set_optional",
]


def set_optional(message: Any, field: str, value: Any) -> None:
    """Assign ``field`` only when ``value`` is not None.

    This one line is the whole presence discipline. Callers pass their kwargs
    straight through, and a ``None`` — "the caller said nothing" — leaves the
    field unset rather than writing a zero the receiver would read as an
    instruction.
    """
    if value is not None:
        setattr(message, field, value)


def _present(message: Any, field: str) -> bool:
    return message.HasField(field)


def _opt(message: Any, field: str) -> Any:
    """The value of an optional field, or None when it was not set."""
    return getattr(message, field) if message.HasField(field) else None


# ── Commands ────────────────────────────────────────────────────────────────


def encode_cmd(command: str | list[str] | tuple[str, ...] | None) -> pb.Cmd | None:
    """A shell string or an argv list, keeping which one it was.

    ``DockerService.exec_in_container`` and ``run_container`` both behave
    differently for a string than for a list — a string goes to a shell, a list
    does not — so flattening one into the other across the wire would change
    what runs.
    """
    if command is None:
        return None
    if isinstance(command, str):
        return pb.Cmd(shell=command)
    return pb.Cmd(argv=pb.Argv(parts=[str(part) for part in command]))


def decode_cmd(cmd: pb.Cmd) -> str | list[str] | None:
    form = cmd.WhichOneof("form")
    if form == "shell":
        return cmd.shell
    if form == "argv":
        return list(cmd.argv.parts)
    return None


# ── Container metadata ──────────────────────────────────────────────────────


def encode_metadata(metadata: ContainerMetadata | None) -> pb.ContainerMetadata:
    metadata = metadata or ContainerMetadata()
    out = pb.ContainerMetadata(
        deployment=metadata.deployment,
        recipe=metadata.recipe,
        image=metadata.image,
        generation=metadata.generation,
        rank=metadata.rank,
        cluster=metadata.cluster,
        role=metadata.role,
        node_rank=metadata.node_rank,
        head_ip=metadata.head_ip,
        ray_enabled=metadata.ray_enabled,
    )
    set_optional(out, "mode", metadata.mode)
    set_optional(out, "created_at", metadata.created_at)
    set_optional(out, "memory_limit_gb", metadata.memory_limit_gb)
    set_optional(out, "shm_size_gb", metadata.shm_size_gb)
    set_optional(out, "privileged", metadata.privileged)
    set_optional(out, "world_size", metadata.world_size)
    return out


def decode_metadata(message: pb.ContainerMetadata) -> ContainerMetadata:
    kwargs: dict[str, Any] = {
        "deployment": message.deployment,
        "recipe": message.recipe,
        "image": message.image,
        "generation": message.generation,
        "rank": message.rank,
        "cluster": message.cluster,
        "role": message.role,
        "node_rank": message.node_rank,
        "head_ip": message.head_ip,
        "ray_enabled": message.ray_enabled,
    }
    for field in (
        "mode",
        "created_at",
        "memory_limit_gb",
        "shm_size_gb",
        "privileged",
        "world_size",
    ):
        if _present(message, field):
            kwargs[field] = getattr(message, field)
    return ContainerMetadata(**kwargs)


# ── Containers ──────────────────────────────────────────────────────────────


def encode_container_info(info: ContainerInfo) -> pb.ContainerInfo:
    return pb.ContainerInfo(
        id=info.id or "",
        name=info.name or "",
        status=info.status or "",
        image=info.image or "",
        metadata=encode_metadata(info.metadata),
        labels=dict(info.labels or {}),
    )


def decode_container_info(message: pb.ContainerInfo) -> ContainerInfo:
    return ContainerInfo(
        id=message.id,
        name=message.name,
        status=message.status,
        image=message.image,
        metadata=decode_metadata(message.metadata),
        labels=dict(message.labels),
    )


def encode_container_status(status: dict[str, Any]) -> pb.ContainerStatus:
    """``DockerService.get_container_status``'s dict, verbatim.

    ``state`` is Docker's own State object with no schema of ours, so it
    crosses as JSON rather than being remodelled. ``id`` and ``error`` are
    genuinely nullable in the source dict and stay nullable here — a missing
    container has ``id=None``, and turning that into ``""`` would make
    ``status["id"] is None`` false on a remote node and true on the local one,
    which is precisely the divergence class being deleted.
    """
    message = pb.ContainerStatus(
        status=str(status.get("status") or ""),
        running=bool(status.get("running")),
        state_json=json.dumps(status.get("state") or {}, default=str),
    )
    set_optional(message, "id", status.get("id"))
    set_optional(message, "error", status.get("error"))
    return message


def decode_container_status(message: pb.ContainerStatus) -> dict[str, Any]:
    try:
        state = json.loads(message.state_json) if message.state_json else {}
    except json.JSONDecodeError:
        state = {}
    return {
        "status": message.status,
        "running": message.running,
        "id": _opt(message, "id"),
        "state": state,
        "error": _opt(message, "error"),
    }


def encode_exec_result(result: ExecResult) -> pb.ExecOutcome:
    return pb.ExecOutcome(
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def decode_exec_result(message: pb.ExecOutcome) -> ExecResult:
    return ExecResult(
        returncode=message.returncode,
        stdout=message.stdout,
        stderr=message.stderr,
    )


# ── Images ──────────────────────────────────────────────────────────────────


def encode_image_info(info: dict[str, Any]) -> pb.ImageInfoValue:
    message = pb.ImageInfoValue(
        id=str(info.get("id") or ""),
        size_bytes=int(info.get("size_bytes") or 0),
        repo_tags=[str(t) for t in info.get("repo_tags") or []],
        repo_digests=[str(d) for d in info.get("repo_digests") or []],
    )
    created = info.get("created")
    set_optional(message, "created", None if created is None else str(created))
    return message


def decode_image_info(message: pb.ImageInfoValue) -> dict[str, Any]:
    return {
        "id": message.id,
        "size_bytes": message.size_bytes,
        "created": _opt(message, "created"),
        "repo_tags": list(message.repo_tags),
        "repo_digests": list(message.repo_digests),
    }


def encode_pull_outcome(result: dict[str, Any]) -> pb.PullOutcome:
    return pb.PullOutcome(
        ref=str(result.get("ref") or ""),
        repository=str(result.get("repository") or ""),
        tag=str(result.get("tag") or ""),
        bytes_done=int(result.get("bytes_done") or 0),
        bytes_total=int(result.get("bytes_total") or 0),
        percent=float(result.get("percent") or 0.0),
        id=str(result.get("id") or ""),
        size_bytes=int(result.get("size_bytes") or 0),
    )


def decode_pull_outcome(message: pb.PullOutcome) -> dict[str, Any]:
    return {
        "ref": message.ref,
        "repository": message.repository,
        "tag": message.tag,
        "bytes_done": message.bytes_done,
        "bytes_total": message.bytes_total,
        "percent": message.percent,
        "id": message.id,
        "size_bytes": message.size_bytes,
    }


def encode_pull_progress(event: dict[str, Any]) -> pb.PullProgress:
    return pb.PullProgress(
        ref=str(event.get("ref") or ""),
        status=str(event.get("status") or ""),
        layers=int(event.get("layers") or 0),
        bytes_done=int(event.get("bytes_done") or 0),
        bytes_total=int(event.get("bytes_total") or 0),
        percent=float(event.get("percent") or 0.0),
    )


def decode_pull_progress(message: pb.PullProgress) -> dict[str, Any]:
    return {
        "ref": message.ref,
        "status": message.status,
        "layers": message.layers,
        "bytes_done": message.bytes_done,
        "bytes_total": message.bytes_total,
        "percent": message.percent,
    }


# ── Building a RunContainer ─────────────────────────────────────────────────

#: ``run_container`` kwargs that are ``optional`` scalars in the proto, paired
#: with nothing else: the encoder walks this list and the decoder walks it
#: back, so adding a kwarg is one edit in one place.
RUN_OPTIONAL_FIELDS: tuple[str, ...] = (
    "privileged",
    "memory_limit_gb",
    "shm_size_gb",
    "pids_limit",
    "nofile_limit",
    "entrypoint_clear",
    "detach",
    "network_host",
    "ipc_host",
    "auto_remove",
)

#: Repeated and map kwargs. These carry no presence bit because
#: ``DockerService`` reads every one of them as ``x or []`` / ``x or {}``, so
#: None and empty are already the same call and a presence bit would be a
#: distinction the receiver could not honour.
RUN_COLLECTION_FIELDS: tuple[str, ...] = (
    "cache_dirs",
    "port_mappings",
    "devices",
    "cap_add",
    "mounts",
    "ulimits",
)


def encode_run_container(
    image: str,
    name: str,
    env_vars: dict[str, str],
    metadata: ContainerMetadata,
    **kwargs: Any,
) -> pb.RunContainer:
    """Encode a ``run_container`` call, keeping unspecified kwargs unspecified."""
    message = pb.RunContainer(
        image=image,
        name=name,
        env_vars={str(k): str(v) for k, v in (env_vars or {}).items()},
        metadata=encode_metadata(metadata),
    )
    for field in RUN_OPTIONAL_FIELDS:
        set_optional(message, field, kwargs.get(field))
    for field in RUN_COLLECTION_FIELDS:
        value = kwargs.get(field)
        if not value:
            continue
        if isinstance(value, dict):
            getattr(message, field).update({str(k): str(v) for k, v in value.items()})
        else:
            getattr(message, field).extend(str(item) for item in value)
    command = encode_cmd(kwargs.get("command"))
    if command is not None:
        message.command.CopyFrom(command)
    unknown = set(kwargs) - set(RUN_OPTIONAL_FIELDS) - set(RUN_COLLECTION_FIELDS)
    unknown.discard("command")
    if unknown:
        # Loud rather than silent: a kwarg the protocol does not carry would
        # otherwise be dropped on the floor and take effect on the local node
        # while doing nothing on a peer — one implementation, two behaviours.
        raise TypeError(f"run_container kwargs not carried by the protocol: {unknown}")
    return message


def decode_run_container(message: pb.RunContainer) -> dict[str, Any]:
    """The kwargs for ``DockerService.run_container``, present fields only."""
    kwargs: dict[str, Any] = {
        "image": message.image,
        "name": message.name,
        "env_vars": dict(message.env_vars),
        "metadata": decode_metadata(message.metadata),
    }
    for field in RUN_OPTIONAL_FIELDS:
        if _present(message, field):
            kwargs[field] = getattr(message, field)
    for field in ("cache_dirs", "port_mappings", "devices", "cap_add"):
        values = list(getattr(message, field))
        if values:
            kwargs[field] = values
    for field in ("mounts", "ulimits"):
        values = dict(getattr(message, field))
        if values:
            kwargs[field] = values
    if _present(message, "command"):
        kwargs["command"] = decode_cmd(message.command)
    return kwargs


# ── Failures ────────────────────────────────────────────────────────────────


def encode_failure(exc: BaseException) -> pb.CommandFailure:
    """An exception, as payload.

    The class name travels so the caller can react to a kind of failure
    without matching on a message — the substring matching that got
    unreachable-versus-failed wrong three times.
    """
    return pb.CommandFailure(type=type(exc).__name__, message=str(exc))


def guarded(fn: Callable[[], Any]) -> tuple[Any, pb.CommandFailure | None]:
    """Run ``fn``, returning either its value or an encoded failure.

    No ``Exception`` escapes: a raised error here is a definite outcome on a
    reachable node, and letting it become a transport error would make it
    indistinguishable from the node having gone away. ``BaseException`` is
    deliberately *not* caught — a ``KeyboardInterrupt`` or a ``SystemExit`` is
    the agent being shut down, not the operation failing.
    """
    try:
        return fn(), None
    except Exception as exc:
        return None, encode_failure(exc)
