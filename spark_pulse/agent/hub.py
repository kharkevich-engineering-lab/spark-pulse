"""Which nodes are connected, and how the control plane talks to them.

The hub is the control plane's half of the transport. It owns one
:class:`AgentConnection` per connected agent, correlates commands with the
results that come back, and — the part that matters — is the single place
where "no result" is turned into :class:`~spark_pulse.agent.errors.NodeUnreachable`.

Three properties are enforced here rather than hoped for:

**A command with no answer is unreachable, never failed.** Every path that can
end without a result — the node was never connected, the stream dropped, the
deadline passed, the control plane is shutting down — resolves the pending
future with ``NodeUnreachable``. There is no code path in this file that
manufactures a failure for a node that did not answer, and that is checked by
a test.

**Liveness is the command channel.** The agent dials in and holds one stream,
so heartbeat liveness and command-channel liveness are the same fact. There is
no separate probe that can disagree with the thing it is probing.

**Three states, not two** (§3.3). ``healthy`` when a heartbeat arrived within
:data:`SUSPECT_AFTER`; ``unknown`` between there and :data:`DEAD_AFTER`;
``dead`` beyond it. The middle state is the one every system in the survey
grew afterwards and the one most expensive to retrofit, so it is here from the
start. Note what the hub does *not* do with it: nothing. It reports. Acting on
silence — and specifically never tearing down a gang because the control plane
lost contact with a node while rank 0 is still serving — is the caller's
decision, on the caller's timings.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.agent.errors import NodeUnreachable, UnreachableReason

logger = logging.getLogger(__name__)

__all__ = [
    "SUSPECT_AFTER",
    "DEAD_AFTER",
    "DEFAULT_COMMAND_TIMEOUT",
    "Liveness",
    "AgentHub",
    "AgentConnection",
    "ConnectedNode",
]

#: A node that has not been heard from for this long is *suspect*. Silent:
#: nothing is torn down, nothing is reported as broken (§3.3's table).
SUSPECT_AFTER = 15.0

#: And beyond this, *unreachable*. Still no action on the workload — evidence
#: of death acts in seconds, inference from silence waits minutes.
DEAD_AFTER = 60.0

#: How long a command waits for its result before the outcome is declared
#: unknown. Deliberately generous: a pull of a 40 GB image is a command, and a
#: deadline shorter than the work turns a slow success into a false unknown.
#: Callers with a tighter budget pass their own.
DEFAULT_COMMAND_TIMEOUT = 900.0


class Liveness(str, Enum):
    """Three states, because two is never enough."""

    HEALTHY = "healthy"
    UNKNOWN = "unknown"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class ConnectedNode:
    """A snapshot of one connected agent, for the API and for operators."""

    node_id: str
    connected_at: float
    last_seen: float
    liveness: Liveness
    agent_version: str
    facts: pb.NodeFacts


class AgentConnection:
    """One agent's stream, from the control plane's side.

    Owns an outbound queue (the servicer's writer drains it) and the pending
    results keyed by command id. Every future it holds is resolved exactly
    once, either with a result that arrived or with ``NodeUnreachable``.
    """

    def __init__(self, node_id: str, *, agent_version: str = "", facts=None):
        self.node_id = node_id
        self.agent_version = agent_version
        self.facts = facts or pb.NodeFacts()
        self.connected_at = time.monotonic()
        self.last_seen = self.connected_at
        self.outbox: asyncio.Queue[pb.ControlMessage] = asyncio.Queue()
        self.closed = asyncio.Event()
        self._pending: dict[str, asyncio.Future[pb.CommandResult]] = {}
        self._progress: dict[str, Callable[[dict[str, Any]], None]] = {}

    # ── Bookkeeping ──────────────────────────────────────────────────────

    def touch(self, facts: pb.NodeFacts | None = None) -> None:
        self.last_seen = time.monotonic()
        if facts is not None and facts.ByteSize():
            self.facts = facts

    def liveness(self, *, now: float | None = None) -> Liveness:
        age = (now if now is not None else time.monotonic()) - self.last_seen
        if age < SUSPECT_AFTER:
            return Liveness.HEALTHY
        if age < DEAD_AFTER:
            return Liveness.UNKNOWN
        return Liveness.DEAD

    def snapshot(self) -> ConnectedNode:
        return ConnectedNode(
            node_id=self.node_id,
            connected_at=self.connected_at,
            last_seen=self.last_seen,
            liveness=self.liveness(),
            agent_version=self.agent_version,
            facts=self.facts,
        )

    # ── Commands ─────────────────────────────────────────────────────────

    def send(
        self,
        command: pb.Command,
        *,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> asyncio.Future[pb.CommandResult]:
        """Queue a command and return the future its result will land in."""
        future: asyncio.Future[pb.CommandResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[command.command_id] = future
        if progress is not None:
            self._progress[command.command_id] = progress
        self.outbox.put_nowait(pb.ControlMessage(command=command))
        return future

    def cancel_command(self, command_id: str) -> None:
        """Ask the agent to stop a command that is still running."""
        self.outbox.put_nowait(
            pb.ControlMessage(cancel=pb.Cancel(command_id=command_id))
        )

    def deliver(self, result: pb.CommandResult) -> None:
        """Hand a result to whoever is waiting for it."""
        self._progress.pop(result.command_id, None)
        future = self._pending.pop(result.command_id, None)
        if future is None or future.done():
            # A result for a command nobody is waiting for: the caller gave up
            # first. Dropped rather than logged as an error — it is the normal
            # consequence of a deadline, not a fault.
            return
        future.set_result(result)

    def deliver_progress(self, message: pb.Progress) -> None:
        callback = self._progress.get(message.command_id)
        if callback is None:
            return
        from spark_pulse.agent import codec

        try:
            callback(codec.decode_pull_progress(message.pull))
        except Exception as exc:  # pragma: no cover — a caller's callback
            logger.warning("progress callback for %s raised: %s", self.node_id, exc)

    def abandon(self, reason: UnreachableReason, detail: str = "") -> None:
        """Fail every command still in flight as *unknown*.

        Called when the stream ends, for any reason at all. This is the one
        moment where an in-flight operation's outcome becomes unknowable, and
        the only honest thing to say about it is that it is unknown — so that
        is the only thing said. A caller that receives this must not release
        the node's GPU or ports: the container may still be running.
        """
        self.closed.set()
        pending, self._pending = self._pending, {}
        self._progress.clear()
        for future in pending.values():
            if not future.done():
                future.set_exception(NodeUnreachable(self.node_id, reason, detail))

    def discard(self, command_id: str) -> None:
        """Stop waiting for a command, without resolving anything."""
        self._pending.pop(command_id, None)
        self._progress.pop(command_id, None)

    @property
    def in_flight(self) -> int:
        return len(self._pending)


class AgentHub:
    """The control plane's registry of connected agents.

    The internal API the rest of the control plane calls is :meth:`call`,
    plus :meth:`connected`, :meth:`liveness` and :meth:`nodes`. Everything
    higher level — the typed, per-operation façade — is in
    ``spark_pulse.agent.operations``, which is built on top of this and adds
    no transport of its own.
    """

    def __init__(
        self,
        *,
        cluster_id: str = "",
        epoch: int = 1,
        default_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ):
        self.cluster_id = cluster_id or str(uuid.uuid4())
        #: Bumped once per control-plane start, and carried on every command.
        #: An agent refuses anything older, so a command from a control plane
        #: that has been replaced cannot act even if it is still in flight.
        self.epoch = epoch
        self.default_timeout = default_timeout
        self._connections: dict[str, AgentConnection] = {}
        self._listeners: list[Callable[[str, bool], None]] = []

    # ── Membership ───────────────────────────────────────────────────────

    def attach(self, connection: AgentConnection) -> AgentConnection | None:
        """Register a connection, returning the one it displaced, if any.

        A node reconnecting before the control plane noticed the old stream had
        gone is normal — a network blip does exactly this — so the new stream
        wins and the old one is abandoned. Everything in flight on the old
        stream becomes unknown, which it is.
        """
        previous = self._connections.get(connection.node_id)
        self._connections[connection.node_id] = connection
        if previous is not None and previous is not connection:
            previous.abandon(
                UnreachableReason.DISCONNECTED,
                "displaced by a newer connection from the same node",
            )
        self._notify(connection.node_id, True)
        return previous

    def detach(self, connection: AgentConnection) -> None:
        """Remove a connection and fail everything it still held."""
        current = self._connections.get(connection.node_id)
        if current is connection:
            del self._connections[connection.node_id]
        connection.abandon(UnreachableReason.DISCONNECTED, "the agent stream ended")
        self._notify(connection.node_id, False)

    def get(self, node_id: str) -> AgentConnection | None:
        return self._connections.get(node_id)

    def is_connected(self, node_id: str) -> bool:
        return node_id in self._connections

    def connected(self) -> list[str]:
        return sorted(self._connections)

    def nodes(self) -> list[ConnectedNode]:
        return [c.snapshot() for c in self._connections.values()]

    def liveness(self, node_id: str) -> Liveness:
        """Healthy, unknown or dead. A node that never connected is dead."""
        connection = self._connections.get(node_id)
        return connection.liveness() if connection else Liveness.DEAD

    def on_change(self, listener: Callable[[str, bool], None]) -> None:
        """Register ``listener(node_id, connected)``, called on every change."""
        self._listeners.append(listener)

    def _notify(self, node_id: str, connected: bool) -> None:
        for listener in list(self._listeners):
            try:
                listener(node_id, connected)
            except Exception as exc:  # pragma: no cover — a caller's callback
                logger.warning("hub listener raised: %s", exc)

    # ── The internal API ─────────────────────────────────────────────────

    def new_command(self, **op: Any) -> pb.Command:
        """A command with a fresh id and this control plane's epoch."""
        return pb.Command(command_id=str(uuid.uuid4()), epoch=self.epoch, **op)

    async def call(
        self,
        node_id: str,
        command: pb.Command,
        *,
        timeout: float | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> pb.CommandResult:
        """Run one command on one node.

        Returns the :class:`CommandResult` the node sent — which may carry a
        ``failure``, and that is a *definite* failure on a reachable node.

        Raises :class:`NodeUnreachable`, and only that, when no result
        arrives. There is no third thing this can do.
        """
        connection = self._connections.get(node_id)
        if connection is None:
            raise NodeUnreachable(
                node_id, UnreachableReason.NOT_CONNECTED, "no agent is connected"
            )
        deadline = self.default_timeout if timeout is None else timeout
        if command.HasField("timeout_seconds"):
            deadline = command.timeout_seconds
        else:
            command.timeout_seconds = deadline
        future = connection.send(command, progress=progress)
        try:
            return await asyncio.wait_for(future, timeout=deadline)
        except asyncio.TimeoutError:
            connection.cancel_command(command.command_id)
            # The command may still be running on the node. That is the point:
            # a deadline bounds our waiting, not the node's working, so the
            # outcome is unknown rather than failed.
            raise NodeUnreachable(
                node_id,
                UnreachableReason.TIMED_OUT,
                f"no result within {deadline:g}s",
            ) from None
        except asyncio.CancelledError:
            connection.cancel_command(command.command_id)
            raise
        finally:
            connection.discard(command.command_id)

    async def shutdown(self) -> None:
        """Abandon every connection, reporting every in-flight call unknown."""
        for connection in list(self._connections.values()):
            connection.abandon(
                UnreachableReason.SHUTTING_DOWN, "the control plane is shutting down"
            )
        self._connections.clear()
