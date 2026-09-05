"""The control node runs an agent too, reached exactly like any other node.

This file is short on purpose. It contains no transport, no alternative code
path and no local branch — it mints a token for the control node, runs the
*ordinary* enrollment against loopback, and starts the *ordinary* agent
dialling loopback. That is the whole thing.

§7 of the plan is why. Every orchestrator surveyed treats one node as a
degenerate configuration rather than a special case, and none of them selects
a local implementation per call: Swarm's manager runs its own agent over a
loopback transport speaking the identical protocol, k3s aims the ordinary
remote join path at localhost, and Nomad seeds the client's server list with
the local address so a colocated client goes over loopback like any other. The
one system that had a distinct single-node path deleted it.

The cost of getting this wrong is already documented in this repository. The
previous boundary had a host argument defaulting to empty meaning "local", and
the contract test written to catch drift hardcoded a remote address — so the
local branch of the remote service was never exercised, and thirteen call
sites queried the control node while claiming to reach a worker. There is no
local branch here to leave untested.

A caller that wants the control node's containers asks the hub for its node
id, exactly as it would for a peer. If a reader of this file cannot tell which
node is the control node from the code that talks to it, the file has worked.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from spark_pulse.agent.executor import LocalExecutor
from spark_pulse.agent.node_agent import NodeAgent, enroll
from spark_pulse.agent.server import ControlPlaneServer
from spark_pulse.agent.store import AgentIdentity

logger = logging.getLogger(__name__)

__all__ = ["LocalAgent", "start_local_agent", "CONTROL_NODE_NAME"]

#: The operator-facing label for the control node. A *label*: identity is the
#: server-minted uuid, the same as for every peer, so a rename does not
#: re-enroll it and two clusters do not collide on a name.
CONTROL_NODE_NAME = "control"


class LocalAgent:
    """The control node's own agent, and the task running it."""

    def __init__(self, agent: NodeAgent, task: asyncio.Task):
        self.agent = agent
        self.task = task

    @property
    def node_id(self) -> str:
        return self.agent.node_id

    async def stop(self) -> None:
        await self.agent.stop()
        self.task.cancel()
        try:
            await self.task
        except (asyncio.CancelledError, Exception):
            pass


async def start_local_agent(
    server: ControlPlaneServer,
    *,
    directory: Path | str | None = None,
    docker_service: Any | None = None,
    host: str = "127.0.0.1",
    name: str = CONTROL_NODE_NAME,
    heartbeat_interval: float | None = None,
    wait: float | None = 10.0,
) -> LocalAgent:
    """Enroll (once) and run the control node's agent against loopback.

    Idempotent across restarts: an identity already on disk is reused, so a
    control plane that restarts does not re-enroll itself and does not
    accumulate a new uuid per start. That is the "detect an existing identity
    and either converge or refuse loudly" rule of §3.1 applied to ourselves —
    and applying it to ourselves is how it stays honest.
    """
    directory = Path(directory) if directory else server.directory / "local-agent"
    identity = AgentIdentity.load(directory)
    if identity is None or server.ledger.get(identity.node_id) is None:
        # Either never enrolled, or enrolled against a CA/ledger that is gone —
        # a fresh control-plane state directory, say. Both need a new identity,
        # and both go through the same enrollment a remote node goes through.
        token = server.mint_token(name)
        identity = await enroll(
            server.enrollment_target(host),
            token,
            trust_bundle_pem=server.trust_bundle_pem,
            trust_bundle_pin=server.trust_bundle_pin,
            directory=directory,
            requested_name=name,
            docker_service=docker_service,
        )
        logger.info("control node enrolled as %s", identity.node_id)

    kwargs: dict[str, Any] = {}
    if heartbeat_interval is not None:
        kwargs["heartbeat_interval"] = heartbeat_interval
    agent = NodeAgent(
        identity,
        server.session_target(host),
        executor=LocalExecutor(docker_service),
        **kwargs,
    )
    task = asyncio.create_task(agent.run_forever(), name="local-node-agent")
    if wait:
        await agent.wait_connected(wait)
    return LocalAgent(agent, task)
