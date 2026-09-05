"""The running control plane, findable from synchronous code.

Everything the agent transport needs at run time — the server, its hub, the
control node's own agent, and the event loop all three live on — is one object
here, held once per process. Synchronous callers reach it through
:func:`current`; that is the whole reason the module exists.

**Why a process-wide handle rather than dependency injection.** The callers
that need a node service are not endpoints. They are ``reconcile_all()`` at
startup, the orchestrator deep inside a deploy, the image cache, the health
validator — reached through four or five frames of synchronous code that a
router does not thread anything through. Passing the runtime down all of them
would mean changing every signature between the router and the docker call,
which is the change this design exists to avoid making twice. So it is looked
up, in exactly one function, and every test that needs a different one uses
:func:`use` to install it for the duration.

**Why the node id join lives here.** A machine has one identity: the id the
node registry minted for it. Enrolment is told that id rather than minting a
second one, so ``NodeRecord.id``, the enrolment ledger's key and the agent's
own ``node_id`` are the same string. :meth:`ControlPlaneRuntime.node_id_for`
is where that is relied on, and it is the only place — if the join ever has to
change, it changes once.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from contextlib import contextmanager

from spark_pulse.agent.hub import AgentHub, Liveness
from spark_pulse.agent.local import CONTROL_NODE_NAME, LocalAgent, start_local_agent
from spark_pulse.agent.server import ControlPlaneServer
from spark_pulse.agent.sync_service import AgentNodeService

if TYPE_CHECKING:  # pragma: no cover — typing only
    from spark_pulse.tools.node_service import Node

logger = logging.getLogger(__name__)

__all__ = [
    "ControlPlaneRuntime",
    "current",
    "set_current",
    "use",
    "start_runtime",
    "stop_runtime",
]


class ControlPlaneRuntime:
    """The server, the hub, the control node's agent, and their loop."""

    def __init__(
        self,
        server: ControlPlaneServer,
        loop: asyncio.AbstractEventLoop,
        *,
        local: LocalAgent | None = None,
    ):
        self.server = server
        self.loop = loop
        self.local = local

    @property
    def hub(self) -> AgentHub:
        return self.server.hub

    @property
    def control_node_id(self) -> str:
        """The control node's own identity, or "" before its agent is up."""
        return self.local.node_id if self.local is not None else ""

    # ── Resolution ───────────────────────────────────────────────────────

    def node_id_for(self, node: Node) -> str:
        """The agent identity behind a :class:`Node`, or "" if there is none.

        Three ways in, in order of how much they can be trusted:

        1. ``node.is_self`` — the control node, whose agent this process runs.
           No lookup: the runtime holds the id directly, so this answer cannot
           be wrong even if the registry file is missing.
        2. ``node.id`` already being an enrolled identity. Callers that got
           their ``Node`` from the registry are here.
        3. The address, matched against the registry. Callers holding an IP
           out of a container label are here, and this is the weakest of the
           three — an address is not an identity and can be reassigned — so
           it is last rather than first.
        """
        if node.is_self:
            return self.control_node_id
        if node.id and self.server.ledger.get(node.id) is not None:
            return node.id
        return self._by_address(node.address)

    def _by_address(self, address: str) -> str:
        """The enrolled identity of whatever machine ``address`` names."""
        if not address:
            return ""
        try:
            from spark_pulse.tools import node_registry

            for record in node_registry.list_nodes():
                if record.address == address:
                    return record.id
        except Exception as exc:  # pragma: no cover — the registry is advisory
            logger.debug("could not consult the node registry for %s: %s", address, exc)
        return ""

    def service_for(self, node: Node) -> AgentNodeService:
        """The synchronous container service bound to ``node``.

        Raises :class:`LookupError` when the node has no agent. That is a
        deliberate refusal rather than a fallback: there is no second way to
        reach a node any more, and quietly substituting the control plane's
        own docker daemon for a node we cannot reach is the exact bug
        ``docs/transport-reexamined.md`` §5.1 recorded thirteen instances of.
        """
        node_id = self.node_id_for(node)
        if not node_id:
            raise LookupError(
                f"{node.label} has no enrolled agent, so there is no way to "
                "reach it. Install one with the node installer."
            )
        return AgentNodeService(self.hub, node_id, self.loop, label=node.label)

    def liveness(self, node: Node) -> Liveness:
        """Healthy, unknown or dead — for a node that may not be enrolled."""
        node_id = self.node_id_for(node)
        return self.hub.liveness(node_id) if node_id else Liveness.DEAD


# ── The process-wide handle ──────────────────────────────────────────────────

_current: ControlPlaneRuntime | None = None


def current() -> ControlPlaneRuntime | None:
    """The running control plane, or ``None`` before startup."""
    return _current


def set_current(runtime: ControlPlaneRuntime | None) -> None:
    """Install (or clear) the process-wide runtime."""
    global _current
    _current = runtime


@contextmanager
def use(runtime: ControlPlaneRuntime | None) -> Iterator[ControlPlaneRuntime | None]:
    """Install ``runtime`` for the duration of the block, then put back what was.

    Restores rather than clears, so nesting works and a test that forgets to
    tear down does not silently disarm the next one.
    """
    previous = _current
    set_current(runtime)
    try:
        yield runtime
    finally:
        set_current(previous)


# ── Lifecycle ────────────────────────────────────────────────────────────────


async def start_runtime(
    *,
    directory: Path | str,
    node_id: str = "",
    name: str = CONTROL_NODE_NAME,
    host: str = "0.0.0.0",
    loopback: str = "127.0.0.1",
    docker_service: Any | None = None,
    session_port: int | None = None,
    enrollment_port: int | None = None,
    wait: float | None = 10.0,
    install: bool = True,
    local_agent: bool = True,
) -> ControlPlaneRuntime:
    """Start the listeners and the control node's own agent.

    The enrollment listener is **not** started here. It is opened only for the
    seconds an install needs it (``bootstrap.enrollment_window``), so a token
    endpoint is not reachable for the life of the process.

    ``local_agent`` is the one thing a caller has to decide. In production it
    is not optional: every node service goes through an agent, *including this
    machine's*, so a control plane that cannot start its own cannot reach its
    own Docker and is not a control plane — a failure there unwinds the whole
    startup.

    In simulation it is skipped entirely. The resolver is the mock and never
    consults any of this, so the agent would be started, waited for, and never
    asked anything — and *waiting* is the problem: every test that builds an
    app paid the full connect timeout, which is how a suite goes from two
    minutes to ten. The listeners still come up, so the shape is the same; it
    is the child process that does not.

    The transport is not left unexercised by that. It is covered end to end,
    against the real binary, by ``tests/test_agent_rust_interop.py``.
    """
    ports: dict[str, int] = {}
    if session_port is not None:
        ports["session_port"] = session_port
    if enrollment_port is not None:
        ports["enrollment_port"] = enrollment_port
    server = ControlPlaneServer(directory=directory, host=host, **ports)
    await server.start()
    runtime = ControlPlaneRuntime(server, asyncio.get_running_loop())
    if install:
        set_current(runtime)
    if not local_agent:
        logger.info("not starting an agent for this machine (simulation)")
        return runtime
    try:
        runtime.local = await start_local_agent(
            server,
            docker_service=docker_service,
            host=loopback,
            name=name,
            node_id=node_id,
            wait=wait,
        )
    except Exception:
        # A control plane whose own agent will not start is not a control
        # plane: every node service, including this machine's, goes through
        # it. Unwind rather than leave a half-started server listening.
        if install:
            set_current(None)
        await server.stop()
        raise
    return runtime


async def stop_runtime(runtime: ControlPlaneRuntime | None = None) -> None:
    """Stop the control node's agent and both listeners."""
    runtime = runtime or _current
    if runtime is None:
        return
    if _current is runtime:
        set_current(None)
    if runtime.local is not None:
        try:
            await runtime.local.stop()
        except Exception as exc:  # pragma: no cover — shutdown is best effort
            logger.warning("the control node's agent did not stop cleanly: %s", exc)
    await runtime.server.stop()
