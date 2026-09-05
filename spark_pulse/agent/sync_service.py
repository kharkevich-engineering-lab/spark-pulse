"""The synchronous face of :class:`NodeOperations`.

The control plane's callers — the orchestrator, reconciliation, the health
validator, the image cache — are synchronous, and run on the AnyIO worker
threads FastAPI hands to sync endpoints and ``run_in_threadpool``. The agent
transport is asynchronous and lives on the app's event loop. This file is the
bridge between them, and it is the *only* one: every other module keeps the
shape it already has.

**Why a bridge rather than two implementations.** A second, synchronous client
speaking the same protocol is exactly the arrangement
``docs/transport-reexamined.md`` §5.1 measured the cost of — thirty semantic
divergences between two hand-written services, three of them live bugs. There
is one client. :class:`AgentNodeService` builds no commands, parses no
results, and knows nothing about the wire; it forwards to
:class:`~spark_pulse.agent.operations.NodeOperations` and waits.

**Why it refuses to run on the loop thread.** ``run_coroutine_threadsafe``
blocks the calling thread until the coroutine finishes. Called from the thread
running the loop, that thread is the one that has to *make* it finish, so it
hangs — silently, with no traceback, until something times out. A hang that
takes an hour to diagnose is worse than an exception that names the mistake,
so calling from a thread with a running loop raises immediately and says to
await :class:`NodeOperations` directly.

**Three outcomes, still three.** ``NodeUnreachable`` and ``NodeOperationError``
cross this boundary untouched, because the distinction between them is the
reason the transport exists. The one thing translated is a small, explicit set
of failures that are part of the container service's contract — a cancelled
pull is ``PullCancelled`` here just as it is on the node — so a caller written
against ``DockerService`` behaves identically against a remote node.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from concurrent.futures import Future, TimeoutError as FutureTimeout
from typing import Any, Callable, Coroutine

from spark_pulse.agent.errors import NodeOperationError
from spark_pulse.agent.hub import DEFAULT_COMMAND_TIMEOUT, AgentHub
from spark_pulse.agent.operations import NodeOperations
from spark_pulse.tools.docker import (
    PULL_PROGRESS_INTERVAL,
    ContainerInfo,
    ContainerMetadata,
    ExecResult,
    PullCancelled,
    PullStalled,
)

logger = logging.getLogger(__name__)

__all__ = ["AgentNodeService", "CONTRACT_EXCEPTIONS", "RESULT_MARGIN"]

#: Remote failures that are part of the container service's published
#: contract, and the local class each is re-raised as. Callers already catch
#: these by type — ``native_runtime`` catches ``PullCancelled`` in two places
#: and ``images`` in a third — so a remote pull that is cancelled has to look
#: the same as a local one or those handlers stop firing.
#:
#: Everything absent from this table stays a :class:`NodeOperationError`. That
#: is deliberate: a table that grew to cover every exception would be a second
#: copy of the node's error taxonomy, drifting from it.
CONTRACT_EXCEPTIONS: dict[str, type[BaseException]] = {
    "PullCancelled": PullCancelled,
    "PullStalled": PullStalled,
}

#: Added to a command's own deadline before the *thread* stops waiting. The
#: hub already resolves every call within its deadline, so this margin is only
#: ever spent if the hub itself is wedged — in which case a thread that waits
#: forever is a leaked worker, and this turns it into an error.
RESULT_MARGIN = 30.0


class AgentNodeService:
    """Container operations on one node, over its agent, synchronously.

    Satisfies :class:`~spark_pulse.tools.node_service.NodeService`. Built for
    one node at construction; no method takes a node.
    """

    def __init__(
        self,
        hub: AgentHub,
        node_id: str,
        loop: asyncio.AbstractEventLoop,
        *,
        timeout: float | None = None,
        label: str = "",
    ):
        self.ops = NodeOperations(hub, node_id, timeout=timeout)
        self.node_id = node_id
        self.loop = loop
        self.label = label or node_id

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<AgentNodeService {self.label}>"

    # ── The bridge ───────────────────────────────────────────────────────

    def _run(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        """Run ``coro`` on the control plane's loop and wait for its value.

        The coroutine is closed rather than leaked if the guard below fires,
        so a misuse does not also produce a "never awaited" warning pointing
        somewhere unhelpful.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            coro.close()
            raise RuntimeError(
                f"AgentNodeService.{coro.__qualname__.split('.')[-1]} was called from "
                "a thread with a running event loop, which would deadlock. Await "
                "spark_pulse.agent.operations.NodeOperations directly instead, or "
                "call this from a worker thread (asyncio.to_thread)."
            )
        future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, self.loop)
        deadline = (
            (self.ops.timeout or DEFAULT_COMMAND_TIMEOUT)
            if timeout is None
            else timeout
        )
        try:
            return future.result(deadline + RESULT_MARGIN)
        except NodeOperationError as exc:
            translated = CONTRACT_EXCEPTIONS.get(exc.error_type)
            if translated is None:
                raise
            raise translated(exc.error_message) from None
        except FutureTimeout:
            # Not NodeUnreachable: the hub guarantees it resolves within the
            # command deadline, so arriving here means the hub did not, and
            # saying "the node is unreachable" would blame the wrong machine.
            future.cancel()
            raise RuntimeError(
                f"the control plane's event loop did not answer within "
                f"{deadline + RESULT_MARGIN:g}s for {self.label}"
            ) from None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def run_container(
        self,
        image: str,
        name: str,
        env_vars: dict[str, str],
        metadata: ContainerMetadata,
        **kwargs: Any,
    ) -> ContainerInfo:
        """Build and start a container carrying spark-pulse labels."""
        return self._run(
            self.ops.run_container(image, name, env_vars, metadata, **kwargs)
        )

    def ensure_directories(self, paths: Iterable[str]) -> list[str]:
        """Create bind-mount sources on the node. Returns the ones that failed."""
        return self._run(self.ops.ensure_directories(paths))

    def stop_container(self, name: str, timeout: int = 30) -> bool:
        """Stop and remove a container by name."""
        return self._run(self.ops.stop_container(name, timeout))

    def get_container_status(self, name: str) -> dict[str, Any]:
        """Return ``{status, running, id, state, error}`` for a container."""
        return self._run(self.ops.get_container_status(name))

    def exec_in_container(
        self,
        container: str | Any,
        command: str | list[str],
        detach: bool = False,
        timeout: int | None = None,
    ) -> ExecResult:
        """Execute a command inside a running container."""
        name = getattr(container, "name", container)
        return self._run(
            self.ops.exec_in_container(str(name), command, detach, timeout)
        )

    def get_logs(self, name: str, tail: int = 200) -> str:
        """Return the tail of a container's logs."""
        return self._run(self.ops.get_logs(name, tail))

    # ── Copying ──────────────────────────────────────────────────────────

    def copy_to_container(
        self,
        container: str,
        local_path: str,
        remote_path: str,
        timeout: int = 120,
    ) -> bool:
        """Copy a file (or a directory) into a container on the node."""
        return self._run(
            self.ops.copy_to_container(container, local_path, remote_path, timeout)
        )

    # ── Inspection ───────────────────────────────────────────────────────

    def list_managed_containers(
        self, labels: dict[str, str] | None = None
    ) -> list[ContainerInfo]:
        """Every spark-pulse managed container on the node, filtered by label."""
        return self._run(self.ops.list_managed_containers(labels))

    def get_container_by_deployment(self, deployment: str) -> ContainerInfo | None:
        """The container carrying a deployment label, or None."""
        return self._run(self.ops.get_container_by_deployment(deployment))

    def get_container_by_recipe(self, recipe: str) -> list[ContainerInfo]:
        """Every container carrying a recipe label."""
        return self._run(self.ops.get_container_by_recipe(recipe))

    # ── Images ───────────────────────────────────────────────────────────

    def image_exists(self, ref: str) -> bool:
        """Whether the image reference resolves on the node."""
        return self._run(self.ops.image_exists(ref))

    def image_info(self, ref: str) -> dict[str, Any] | None:
        """``{id, size_bytes, created, repo_tags, repo_digests}`` or None."""
        return self._run(self.ops.image_info(ref))

    def list_images(self) -> list[dict[str, Any]]:
        """Every image on the node, shaped like :meth:`image_info`."""
        return self._run(self.ops.list_images())

    def pull_image(
        self,
        ref: str,
        progress: Any | None = None,
        interval: float = PULL_PROGRESS_INTERVAL,
        cancel: Callable[[], bool] | None = None,
        stall_timeout: float | None = None,
    ) -> dict[str, Any]:
        """Pull an image onto the node, reporting aggregated progress.

        ``progress`` is invoked on the control plane's **event loop** thread,
        not this one. Every current callback publishes an event or updates a
        dict, which is safe; a callback that blocks would stall the loop, so
        it must not.
        """
        return self._run(
            self.ops.pull_image(
                ref,
                progress=progress,
                interval=interval,
                stall_timeout=stall_timeout,
                cancel=cancel,
            )
        )

    def remove_image(self, ref: str, force: bool = False) -> bool:
        """Remove an image from the node. False when it was not there."""
        return self._run(self.ops.remove_image(ref, force))

    # ── Beyond the container service ─────────────────────────────────────

    def get_facts(self) -> Any:
        """Ask the node to describe itself, now, rather than reading a cache.

        Not part of :class:`NodeService`: nothing that holds a service
        generically calls it. It is here because a caller that has already
        resolved a node should not have to reach past the service it was
        handed to ask that node a question.
        """
        return self._run(self.ops.get_facts())
