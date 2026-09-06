"""The control node runs an agent too, and it is the same binary.

This file is short on purpose. It contains no transport, no alternative code
path and no local branch — it mints a token for the control node, runs the
*ordinary* enrolment against loopback, and starts the *ordinary* agent
process dialling loopback. That is the whole thing.

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

**It is a child process, not an in-process object**, and that is the whole
point of the arrangement rather than an implementation detail: the control
node runs the identical binary its peers run, so there is exactly one agent
implementation in the system and no way for a "local" one to drift from the
one that ships.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from spark_pulse.agent.bundle import host_binary
from spark_pulse.agent.server import ControlPlaneServer
from spark_pulse.agent.store import AgentIdentity

logger = logging.getLogger(__name__)

__all__ = ["LocalAgent", "start_local_agent", "CONTROL_NODE_NAME"]

#: The operator-facing label for the control node. A *label*: identity is the
#: server-minted uuid, the same as for every peer, so a rename does not
#: re-enroll it and two clusters do not collide on a name.
CONTROL_NODE_NAME = "control"

#: How long enrolment is given before it is called a failure. It is one CSR
#: signature over loopback; ten seconds is already generous.
ENROLL_TIMEOUT = 30.0


class LocalAgent:
    """The control node's own agent, and the process running it."""

    def __init__(self, process: asyncio.subprocess.Process, node_id: str):
        self.process = process
        self._node_id = node_id

    @property
    def node_id(self) -> str:
        return self._node_id

    async def stop(self) -> None:
        """Terminate, and wait.

        SIGTERM rather than a kill, so the agent closes its stream and the hub
        reads a clean end rather than a node that vanished — the same signal
        systemd sends, exercised on every shutdown here.
        """
        if self.process.returncode is not None:
            return
        try:
            self.process.terminate()
        except ProcessLookupError:  # pragma: no cover — it already exited
            return
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except asyncio.TimeoutError:  # pragma: no cover — it should stop
            logger.warning("the control node's agent did not stop; killing it")
            self.process.kill()
            await self.process.wait()


async def start_local_agent(
    server: ControlPlaneServer,
    *,
    directory: Path | str | None = None,
    docker_service: Any | None = None,
    host: str = "127.0.0.1",
    name: str = CONTROL_NODE_NAME,
    node_id: str = "",
    heartbeat_interval: float | None = None,
    wait: float | None = 10.0,
    binary: Path | None = None,
) -> LocalAgent:
    """Enroll (once) and run the control node's agent against loopback.

    Idempotent across restarts: an identity already on disk is reused, so a
    control plane that restarts does not re-enroll itself and does not
    accumulate a new uuid per start. That is the "detect an existing identity
    and either converge or refuse loudly" rule of §3.1 applied to ourselves —
    and applying it to ourselves is how it stays honest.

    ``node_id`` is the control node's *registry* id. Passing it is what keeps
    this machine from having two identities — one the node registry minted at
    startup, one enrolment would have minted a moment later — which would then
    have to be joined by address, and address is exactly the key that stops
    being unique the moment anything is renumbered.
    """
    del docker_service, heartbeat_interval  # the agent finds its own Docker
    directory = Path(directory) if directory else server.directory / "local-agent"
    executable = Path(binary) if binary else host_binary()

    identity = AgentIdentity.load(directory)
    stale = identity is not None and bool(node_id) and identity.node_id != node_id
    if identity is None or server.ledger.get(identity.node_id) is None or stale:
        # Never enrolled; enrolled against a CA/ledger that is gone (a fresh
        # control-plane state directory, say); or holding an identity the node
        # registry no longer agrees with. All three need a new identity, and
        # all three go through the same enrollment a remote node goes through.
        #
        # Re-enrolling *ourselves* is safe in a way re-enrolling a peer is not:
        # the identity lives on this disk, the transport is loopback, and no
        # other machine has to be told. That asymmetry is why re-enrolment is
        # automatic here and never automatic there.
        if identity is not None:
            identity.destroy()
        minted = await _enroll(server, executable, directory, host, name, node_id)
        logger.info("control node enrolled as %s", minted)
        identity = AgentIdentity.load(directory)
        if identity is None:  # pragma: no cover — enrolment wrote nothing
            raise RuntimeError("the agent reported enrolment but wrote no identity")

    process = await asyncio.create_subprocess_exec(
        str(executable),
        "--control",
        server.session_target(host),
        "--dir",
        str(directory),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    agent = LocalAgent(process, identity.node_id)
    if wait:
        await _wait_connected(server, identity.node_id, agent, wait)
    return agent


async def _enroll(
    server: ControlPlaneServer,
    executable: Path,
    directory: Path,
    host: str,
    name: str,
    node_id: str,
) -> str:
    """Spend a token under ``--enroll-only``, exactly as the installer does."""
    from spark_pulse.agent.bootstrap import enrollment_window

    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    bundle = directory / "ca.bootstrap.pem"
    bundle.write_bytes(server.trust_bundle_pem)
    token_file = directory / "token"
    # A file, not an argument: an argument is visible in `ps(1)` to every user.
    # 0600 from the moment it exists, for the same reason — the SSH installer
    # uploads its token at 0600 and this is the same secret with the same life,
    # so it does not get to be the one written at the umask's mercy.
    fd = os.open(str(token_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(server.mint_token(name, node_id=node_id) + "\n")
    try:
        async with enrollment_window(server):
            process = await asyncio.create_subprocess_exec(
                str(executable),
                "--control",
                server.session_target(host),
                "--enroll-target",
                server.enrollment_target(host),
                "--token-file",
                str(token_file),
                "--trust-bundle",
                str(bundle),
                "--pin",
                server.trust_bundle_pin,
                "--dir",
                str(directory),
                "--name",
                name,
                "--enroll-only",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(
                process.communicate(), timeout=ENROLL_TIMEOUT
            )
    finally:
        token_file.unlink(missing_ok=True)
        bundle.unlink(missing_ok=True)
    if process.returncode != 0:
        raise RuntimeError(
            "the control node could not enroll its own agent: "
            f"{err.decode(errors='replace').strip()[:600]}"
        )
    return out.decode().strip()


async def _wait_connected(
    server: ControlPlaneServer, node_id: str, agent: LocalAgent, timeout: float
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if server.hub.is_connected(node_id):
            return
        if agent.process.returncode is not None:
            raise RuntimeError(
                f"the control node's agent exited with {agent.process.returncode} "
                "before it connected"
            )
        await asyncio.sleep(0.05)
    await agent.stop()
    raise RuntimeError(f"the control node's agent ({node_id}) never connected")
