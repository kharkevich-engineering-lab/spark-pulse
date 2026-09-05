"""The agent: enrollment, the session it holds, and certificate renewal.

The agent dials out. It never listens, which is what makes it work on a node
behind whatever the operator's network does, and what makes the control plane
need a fixed number of inbound ports rather than one per node.

Its whole life is one loop: dial, say Hello, hold the stream, execute what
arrives, heartbeat while nothing does, and redial when it drops. Everything
interesting is arranged so that a test can drive it in-process:
:class:`NodeAgent` is a plain object built from an identity, a target and an
executor, and :meth:`run_once` runs exactly one session and returns. Nothing
here needs a container, a daemon, or a second machine.

Three things are easy to get wrong and are handled explicitly:

* **All writes funnel through one queue.** gRPC forbids concurrent writes on
  one stream, and a command handler, the heartbeat and the progress reporter
  are three writers. One writer task drains the queue.
* **A command runs on a worker thread.** ``DockerService`` is synchronous and
  a pull takes minutes; running it on the event loop would stop the heartbeat
  and the node would be declared unreachable while it was busy succeeding.
* **Renewal happens over the authenticated channel**, at a jittered 50-80% of
  the certificate's life, and the new certificate takes effect on the next
  dial. A renewal is therefore never urgent: the window is weeks wide.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Any

import grpc

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.agent import agent_pb2_grpc as pb_grpc
from spark_pulse.agent import identity as ident
from spark_pulse.agent import keepalive
from spark_pulse.agent.executor import LocalExecutor
from spark_pulse.agent.facts import collect_facts
from spark_pulse.agent.store import AgentIdentity
from spark_pulse.version import __version__

logger = logging.getLogger(__name__)

__all__ = ["NodeAgent", "enroll", "HEARTBEAT_INTERVAL"]

#: How often an idle agent reports in. Below the hub's fifteen-second suspect
#: threshold with room for one lost beat.
HEARTBEAT_INTERVAL = 5.0

#: Reconnect backoff, in seconds. Capped low: a node that cannot reach the
#: control plane is useless, and there is no cost to trying often on a LAN.
RECONNECT_MIN = 1.0
RECONNECT_MAX = 15.0


# ── Enrollment (the client half) ────────────────────────────────────────────


async def enroll(
    target: str,
    token: str,
    *,
    trust_bundle_pem: bytes,
    trust_bundle_pin: str = "",
    directory: Path | str,
    requested_name: str = "",
    docker_service: Any | None = None,
    options: list[tuple[str, int]] | None = None,
) -> AgentIdentity:
    """Exchange a token for a certificate, and write the identity out.

    The three things an installer must deliver to the node are the token, the
    trust bundle and its pin. The pin is checked *before* the bundle is used
    as a root of trust, so a substituted bundle is caught by the node rather
    than trusted by it; and it is checked again against what the server
    returns, so a control plane that answers with a different bundle than the
    one it was reached over is refused.

    The key is generated here and stays here. Only the CSR is sent — the
    opposite of NVIDIA's ``discover-sparks``, which copies one private key to
    every node and makes any single Spark a key to all of them.
    """
    if trust_bundle_pin and ident.spki_pin(trust_bundle_pem) != trust_bundle_pin:
        raise RuntimeError(
            "the trust bundle does not match the pin it came with; refusing to "
            "enroll against it"
        )
    pair = ident.build_csr()
    credentials = grpc.ssl_channel_credentials(root_certificates=trust_bundle_pem)
    async with grpc.aio.secure_channel(
        target, credentials, options=options or keepalive.client_options()
    ) as channel:
        stub = pb_grpc.EnrollmentStub(channel)
        issued = await stub.Enroll(
            pb.EnrollRequest(
                token=token,
                csr_pem=pair.csr_pem,
                requested_name=requested_name,
                facts=collect_facts(docker_service),
            )
        )
    if trust_bundle_pin and issued.trust_bundle_spki != trust_bundle_pin:
        raise RuntimeError(
            "the control plane returned a trust bundle that does not match the "
            "pin the installer supplied"
        )
    identity = AgentIdentity(
        directory=Path(directory),
        node_id=issued.node_id,
        key_pem=pair.key_pem,
        certificate_pem=issued.certificate_pem,
        trust_bundle_pem=issued.trust_bundle_pem,
        trust_bundle_pin=issued.trust_bundle_spki,
        cluster_id=issued.cluster_id,
        spiffe_id=issued.spiffe_id,
        epoch=issued.epoch,
        not_before=float(issued.not_before_unix),
        not_after=float(issued.not_after_unix),
    )
    identity.save()
    return identity


# ── The agent ───────────────────────────────────────────────────────────────


class NodeAgent:
    """One node's agent. Dials the control plane and holds one stream."""

    def __init__(
        self,
        identity: AgentIdentity,
        target: str,
        *,
        executor: LocalExecutor | None = None,
        enrollment_target: str = "",
        docker_service: Any | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        options: list[tuple[str, int]] | None = None,
        rng: random.Random | None = None,
    ):
        self.identity = identity
        self.target = target
        self.enrollment_target = enrollment_target
        self.executor = executor or LocalExecutor(docker_service)
        self.heartbeat_interval = heartbeat_interval
        self.options = options or keepalive.client_options()
        self._rng = rng or random.Random()
        self._outbox: asyncio.Queue[pb.AgentMessage] = asyncio.Queue()
        self._cancelled: set[str] = set()
        self._running: dict[str, asyncio.Task] = {}
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()
        self.cluster_id = identity.cluster_id
        self.sessions = 0

    # ── Channel ──────────────────────────────────────────────────────────

    def _credentials(self) -> grpc.ChannelCredentials:
        return grpc.ssl_channel_credentials(
            root_certificates=self.identity.trust_bundle_pem,
            private_key=self.identity.key_pem,
            certificate_chain=self.identity.certificate_pem,
        )

    @property
    def node_id(self) -> str:
        return self.identity.node_id

    async def wait_connected(self, timeout: float = 10.0) -> None:
        """Block until the session is established. For tests and for startup."""
        await asyncio.wait_for(self._connected.wait(), timeout)

    # ── Running ──────────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """Dial, hold, redial. Returns only when :meth:`stop` is called."""
        delay = RECONNECT_MIN
        while not self._stop.is_set():
            try:
                await self.run_once()
                delay = RECONNECT_MIN
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("agent session to %s ended: %s", self.target, exc)
                # Jittered so a rack of Sparks that lost the control plane
                # together does not come back in lockstep.
                await asyncio.sleep(delay * self._rng.uniform(0.5, 1.5))
                delay = min(delay * 2, RECONNECT_MAX)

    async def run_once(self) -> None:
        """Hold exactly one session, and return when it ends."""
        async with grpc.aio.secure_channel(
            self.target, self._credentials(), options=self.options
        ) as channel:
            stub = pb_grpc.NodeSessionStub(channel)
            call = stub.Session()
            await call.write(
                pb.AgentMessage(
                    hello=pb.Hello(
                        node_id=self.identity.node_id,
                        agent_version=__version__,
                        facts=collect_facts(self.executor.docker_or_none),
                        known_epoch=self.executor.epoch,
                    )
                )
            )
            writer = asyncio.create_task(self._write_loop(call), name="agent-writer")
            beater = asyncio.create_task(self._heartbeat_loop(), name="agent-beat")
            renewer = asyncio.create_task(self._renew_loop(channel), name="agent-renew")
            try:
                await self._read_loop(call)
            finally:
                self._connected.clear()
                for task in (writer, beater, renewer):
                    task.cancel()
                for task in list(self._running.values()):
                    task.cancel()
                self._running.clear()
                await asyncio.gather(writer, beater, renewer, return_exceptions=True)

    async def stop(self) -> None:
        self._stop.set()

    # ── The three loops ──────────────────────────────────────────────────

    async def _write_loop(self, call) -> None:
        """The only writer. gRPC forbids concurrent writes on one stream."""
        while True:
            message = await self._outbox.get()
            await call.write(message)

    async def _heartbeat_loop(self) -> None:
        seq = 0
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            seq += 1
            self._outbox.put_nowait(
                pb.AgentMessage(
                    heartbeat=pb.Heartbeat(
                        seq=seq,
                        sent_unix=int(time.time()),
                        facts=collect_facts(self.executor.docker_or_none),
                    )
                )
            )

    async def _renew_loop(self, channel: grpc.aio.Channel) -> None:
        """Renew at a jittered 50-80% of the certificate's life.

        Over the channel already authenticated by the *current* certificate,
        which is what makes renewal need no token and no operator. The new
        certificate is written to disk and takes effect on the next dial; the
        window is weeks wide, so that is never urgent.
        """
        if not self.identity.not_after:
            return
        while True:
            delay = ident.renewal_delay(
                self.identity.not_before_dt,
                self.identity.not_after_dt,
                rng=self._rng,
            )
            await asyncio.sleep(max(delay, 1.0))
            try:
                pair = ident.build_csr()
                stub = pb_grpc.EnrollmentStub(channel)
                issued = await stub.Renew(
                    pb.RenewRequest(
                        csr_pem=pair.csr_pem,
                        facts=collect_facts(self.executor.docker_or_none),
                    )
                )
                self.identity.key_pem = pair.key_pem
                self.identity.update_from(issued)
                logger.info("renewed certificate for %s", self.identity.node_id)
            except Exception as exc:
                logger.warning("certificate renewal failed: %s", exc)
                await asyncio.sleep(min(RECONNECT_MAX, 60.0))

    async def _read_loop(self, call) -> None:
        while True:
            message = await call.read()
            if message == grpc.aio.EOF:
                return
            body = message.WhichOneof("body")
            if body == "welcome":
                self.cluster_id = message.welcome.cluster_id
                self.executor.note_epoch(message.welcome.epoch)
                self.sessions += 1
                self._connected.set()
            elif body == "command":
                self._start_command(message.command)
            elif body == "cancel":
                # Only for a command actually running here. A cancel for one
                # that already finished is dropped rather than remembered, so
                # the set cannot grow for the life of the process.
                if message.cancel.command_id in self._running:
                    self._cancelled.add(message.cancel.command_id)

    # ── Commands ─────────────────────────────────────────────────────────

    def _start_command(self, command: pb.Command) -> None:
        task = asyncio.create_task(
            self._run_command(command), name=f"agent-cmd-{command.command_id}"
        )
        self._running[command.command_id] = task
        task.add_done_callback(lambda _: self._running.pop(command.command_id, None))

    async def _run_command(self, command: pb.Command) -> None:
        loop = asyncio.get_running_loop()
        command_id = command.command_id

        def progress(event: dict) -> None:
            from spark_pulse.agent import codec

            loop.call_soon_threadsafe(
                self._outbox.put_nowait,
                pb.AgentMessage(
                    progress=pb.Progress(
                        command_id=command_id, pull=codec.encode_pull_progress(event)
                    )
                ),
            )

        def cancelled() -> bool:
            return command_id in self._cancelled

        try:
            # On a worker thread: DockerService is synchronous, and a pull
            # takes minutes. Running it here would stop the heartbeat and the
            # node would be reported unreachable while it was busy working.
            result = await asyncio.to_thread(
                self.executor.execute, command, progress=progress, cancel=cancelled
            )
        except asyncio.CancelledError:
            # The stream went away mid-command. No result is sent, which is
            # exactly right: the caller learns "unreachable, unknown" rather
            # than a failure we would be inventing.
            raise
        finally:
            self._cancelled.discard(command_id)
        self._outbox.put_nowait(pb.AgentMessage(result=result))
