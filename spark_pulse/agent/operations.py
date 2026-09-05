"""The internal API: ask node X to do something, get an answer or "unknown".

:class:`NodeOperations` is bound to one node at construction and takes no node
argument on any method — the same shape as ``NodeService``, and for the same
reason. ``docs/transport-reexamined.md`` recorded what the old shape cost: a
host argument with an empty-string default meaning "this node" was passed by
thirteen call sites, and every one of them silently queried the control node
while claiming to reach a worker. A node identity that can be omitted will be
omitted.

Each method returns exactly what ``DockerService`` returns, because the agent
called ``DockerService`` and the codec carried its return value. Each raises
:class:`~spark_pulse.agent.errors.NodeOperationError` when the node ran the
operation and it failed, and
:class:`~spark_pulse.agent.errors.NodeUnreachable` when no answer came back.
Those three are the whole vocabulary.

These are coroutines. The control plane's synchronous ``NodeService`` callers
get a sync adapter over this in a later step — that adapter is the *only* new
code that task needs, because the operations, the shapes and the error
vocabulary are all settled here.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import logging
import os
import tarfile
from pathlib import Path
from typing import Any, Callable, Iterable

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.agent import codec
from spark_pulse.agent.errors import NodeOperationError
from spark_pulse.agent.hub import AgentHub
from spark_pulse.tools.docker import ContainerInfo, ContainerMetadata, ExecResult

logger = logging.getLogger(__name__)

__all__ = ["NodeOperations", "NODE_OPERATIONS", "CANCEL_POLL_INTERVAL"]

#: How often a ``cancel`` callback is polled while a pull is in flight. A
#: teardown wants the pull to stop promptly; the callback is cheap, and the
#: node stops on the first Cancel it sees.
CANCEL_POLL_INTERVAL = 0.5

#: Every operation the protocol carries, in the order they appear below. The
#: fifteen ``NodeService`` methods plus ``copy_dir_to_container`` and
#: ``get_facts``; a test asserts this equals ``NODE_SERVICE_METHODS`` plus
#: those two, so an operation added to one and not the other fails.
NODE_OPERATIONS: tuple[str, ...] = (
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
    "copy_dir_to_container",
    "get_facts",
)


class NodeOperations:
    """Container operations on exactly one node, over its agent."""

    def __init__(self, hub: AgentHub, node_id: str, *, timeout: float | None = None):
        self.hub = hub
        self.node_id = node_id
        self.timeout = timeout

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<NodeOperations {self.node_id}>"

    # ── The one place a result is unwrapped ──────────────────────────────

    async def _call(
        self,
        command: pb.Command,
        expect: str,
        *,
        timeout: float | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        """Send, wait, and turn the payload into a value or an exception.

        ``NodeUnreachable`` propagates untouched from the hub. It is never
        caught here and never converted into a failure — a caller must be able
        to tell "the container is gone" from "we do not know", because
        releasing a GPU on the second one is how two gangs end up fighting
        over one device (§3.3).
        """
        result = await self.hub.call(
            self.node_id,
            command,
            timeout=self.timeout if timeout is None else timeout,
            progress=progress,
        )
        outcome = result.WhichOneof("outcome")
        if outcome == "failure":
            raise NodeOperationError(
                self.node_id, result.failure.type, result.failure.message
            )
        if outcome != expect:
            raise NodeOperationError(
                self.node_id,
                "ProtocolError",
                f"expected a {expect} outcome, got {outcome}",
            )
        return getattr(result, outcome)

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def run_container(
        self,
        image: str,
        name: str,
        env_vars: dict[str, str],
        metadata: ContainerMetadata,
        **kwargs: Any,
    ) -> ContainerInfo:
        """Build and start a container carrying spark-pulse labels.

        Every keyword is optional in the wire sense as well as the Python one:
        one the caller omits is not sent, and the node's ``DockerService``
        applies its own default. A default therefore exists in exactly one
        place in the system.
        """
        command = self.hub.new_command(
            run_container=codec.encode_run_container(
                image, name, env_vars, metadata, **kwargs
            )
        )
        ref = await self._call(command, "container")
        return codec.decode_container_info(ref.container)

    async def ensure_directories(self, paths: Iterable[str]) -> list[str]:
        """Create bind-mount sources on the node. Returns the ones that failed."""
        command = self.hub.new_command(
            ensure_directories=pb.EnsureDirectories(paths=[str(p) for p in paths])
        )
        return list((await self._call(command, "strings")).values)

    async def stop_container(self, name: str, timeout: int | None = None) -> bool:
        message = pb.StopContainer(name=name)
        codec.set_optional(message, "timeout", timeout)
        command = self.hub.new_command(stop_container=message)
        return (await self._call(command, "boolean")).value

    async def get_container_status(self, name: str) -> dict[str, Any]:
        command = self.hub.new_command(
            get_container_status=pb.GetContainerStatus(name=name)
        )
        return codec.decode_container_status(await self._call(command, "status"))

    async def exec_in_container(
        self,
        container: str,
        command: str | list[str],
        detach: bool | None = None,
        timeout: int | None = None,
    ) -> ExecResult:
        message = pb.ExecInContainer(
            container=container, command=codec.encode_cmd(command)
        )
        codec.set_optional(message, "detach", detach)
        codec.set_optional(message, "timeout", timeout)
        request = self.hub.new_command(exec_in_container=message)
        return codec.decode_exec_result(await self._call(request, "exec"))

    async def get_logs(self, name: str, tail: int | None = None) -> str:
        message = pb.GetLogs(name=name)
        codec.set_optional(message, "tail", tail)
        command = self.hub.new_command(get_logs=message)
        return (await self._call(command, "text")).value

    # ── Copying ──────────────────────────────────────────────────────────

    async def copy_to_container(
        self,
        container: str,
        local_path: str,
        remote_path: str,
        timeout: int | None = None,
    ) -> bool:
        """Copy a file from the control node into a container on this node.

        The file is read here and its bytes travel as payload — there is no
        second hop and no shared filesystem assumed. Its permission bits go
        with it, because the thing most often copied this way is a serve
        script that has to be executable when it lands.

        Bulk artifacts do not come this way. A model or an image is fetched
        once by the control node and distributed through the registry (§3.4);
        this channel carries scripts and configuration.
        """
        source = Path(local_path)
        if source.is_dir():
            return await self.copy_dir_to_container(
                container, local_path, remote_path, timeout
            )
        message = pb.CopyToContainer(
            container=container,
            remote_path=remote_path,
            content=source.read_bytes(),
            source_name=source.name,
        )
        codec.set_optional(message, "mode", os.stat(source).st_mode & 0o7777)
        codec.set_optional(message, "timeout", timeout)
        command = self.hub.new_command(copy_to_container=message)
        return (await self._call(command, "boolean")).value

    async def copy_dir_to_container(
        self,
        container: str,
        local_path: str,
        remote_path: str,
        timeout: int | None = None,
    ) -> bool:
        """Copy a directory tree into a container on this node."""
        source = Path(local_path)
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as raw:
            with tarfile.open(fileobj=raw, mode="w|") as archive:
                for entry in sorted(source.rglob("*")):
                    archive.add(entry, arcname=str(entry.relative_to(source)))
        message = pb.CopyDirToContainer(
            container=container,
            remote_path=remote_path,
            tar_gz=buffer.getvalue(),
            source_name=source.name,
        )
        codec.set_optional(message, "timeout", timeout)
        command = self.hub.new_command(copy_dir_to_container=message)
        return (await self._call(command, "boolean")).value

    # ── Inspection ───────────────────────────────────────────────────────

    async def list_managed_containers(
        self, labels: dict[str, str] | None = None
    ) -> list[ContainerInfo]:
        command = self.hub.new_command(
            list_managed_containers=pb.ListManagedContainers(labels=labels or {})
        )
        listing = await self._call(command, "containers")
        return [codec.decode_container_info(c) for c in listing.containers]

    async def get_container_by_deployment(
        self, deployment: str
    ) -> ContainerInfo | None:
        command = self.hub.new_command(
            get_container_by_deployment=pb.GetContainerByDeployment(
                deployment=deployment
            )
        )
        ref = await self._call(command, "container")
        return codec.decode_container_info(ref.container) if ref.found else None

    async def get_container_by_recipe(self, recipe: str) -> list[ContainerInfo]:
        command = self.hub.new_command(
            get_container_by_recipe=pb.GetContainerByRecipe(recipe=recipe)
        )
        listing = await self._call(command, "containers")
        return [codec.decode_container_info(c) for c in listing.containers]

    # ── Images ───────────────────────────────────────────────────────────

    async def image_exists(self, ref: str) -> bool:
        command = self.hub.new_command(image_exists=pb.ImageExists(ref=ref))
        return (await self._call(command, "boolean")).value

    async def image_info(self, ref: str) -> dict[str, Any] | None:
        command = self.hub.new_command(image_info=pb.ImageInfo(ref=ref))
        found = await self._call(command, "image")
        return codec.decode_image_info(found.image) if found.found else None

    async def list_images(self) -> list[dict[str, Any]]:
        command = self.hub.new_command(list_images=pb.ListImages())
        listing = await self._call(command, "images")
        return [codec.decode_image_info(i) for i in listing.images]

    async def pull_image(
        self,
        ref: str,
        progress: Callable[[dict[str, Any]], None] | None = None,
        interval: float | None = None,
        stall_timeout: float | None = None,
        timeout: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Pull an image onto the node, streaming progress back as it goes.

        ``progress`` is invoked on the control plane's event loop with the same
        dicts ``DockerService`` produces locally, delivered as Progress
        messages on the same stream the command went out on. A progress
        message is never an outcome: the pull is finished when — and only when
        — the result arrives.

        ``cancel`` is polled here and, when it turns true, a Cancel is sent to
        the node — and then we **keep waiting**. That is the whole subtlety:
        the node answers a cancelled pull with a ``PullCancelled`` failure,
        which is a definite outcome, and giving up on the await instead would
        downgrade it to unknown. A caller that cancels a pull knows the pull
        did not happen; it should not have to guess.
        """
        message = pb.PullImage(ref=ref, want_progress=progress is not None)
        codec.set_optional(message, "interval", interval)
        codec.set_optional(message, "stall_timeout", stall_timeout)
        command = self.hub.new_command(pull_image=message)
        call = asyncio.ensure_future(
            self._call(command, "pull", timeout=timeout, progress=progress)
        )
        watcher = (
            None
            if cancel is None
            else asyncio.ensure_future(
                self._watch_for_cancel(command.command_id, cancel, interval)
            )
        )
        try:
            outcome = await call
        finally:
            if watcher is not None:
                watcher.cancel()
        return codec.decode_pull_outcome(outcome)

    async def _watch_for_cancel(
        self,
        command_id: str,
        cancel: Callable[[], bool],
        interval: float | None,
    ) -> None:
        """Poll ``cancel`` and send one Cancel to the node when it fires.

        One, not one per tick: the agent records the id and the second message
        would be about a command it has already stopped.
        """
        period = min(interval or CANCEL_POLL_INTERVAL, CANCEL_POLL_INTERVAL)
        while True:
            await asyncio.sleep(period)
            try:
                fired = bool(cancel())
            except Exception as exc:  # pragma: no cover — a caller's callback
                logger.warning("cancel callback for %s raised: %s", self.node_id, exc)
                return
            if fired:
                self.hub.cancel_command(self.node_id, command_id)
                return

    async def remove_image(self, ref: str, force: bool | None = None) -> bool:
        message = pb.RemoveImage(ref=ref)
        codec.set_optional(message, "force", force)
        command = self.hub.new_command(remove_image=message)
        return (await self._call(command, "boolean")).value

    # ── Facts ────────────────────────────────────────────────────────────

    async def get_facts(self) -> pb.NodeFacts:
        """Ask the node to describe itself, now, rather than reading a cache."""
        command = self.hub.new_command(get_facts=pb.GetFacts())
        return await self._call(command, "facts")
