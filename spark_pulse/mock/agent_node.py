"""A simulated node: the node half of the protocol, under a caller's control.

**This is not a second agent.** The agent that ships is one static Rust binary,
and `tests/test_agent_rust_interop.py` drives *that* against a real control
plane and a real Docker daemon. What this module provides is the opposite
direction — a counterparty that speaks the node half so the control plane's
hub, servicer and ledger can be tested against something that **misbehaves on
purpose**: a node that claims an identity its certificate does not entitle it
to, one that opens a session without a certificate at all, one that accepts a
command and never answers, one that vanishes mid-operation.

The real agent cannot do any of those, which is the point. A test that needs a
node to drop a connection halfway through a pull cannot ask a correct agent to
do it, and keeping a second *real* agent alive so tests have something
controllable is the arrangement ``docs/transport-reexamined.md`` §5.1 measured
the cost of. A fake counterparty is the ordinary way to test a client, and it
is never installed on anything.

It lives in ``mock/`` rather than in ``tests/`` because two things need it: the
control-plane tests, and :mod:`spark_pulse.mock.bootstrap_node`, whose
simulated machines have to answer an installer the way a node would. One
simulated client, not two.

What is deliberately *not* here: anything about Docker. This answers commands
with whatever the caller scripted, so an assertion about container behaviour
made against it would be an assertion about the script. Those live in the
interop suite, against a real daemon.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import grpc

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.agent import agent_pb2_grpc as pb_grpc
from spark_pulse.agent import identity as ident
from spark_pulse.agent.store import AgentIdentity
from spark_pulse.mock.docker import MockDockerClient
from spark_pulse.version import __version__

#: A handler returns the result to send, or ``None`` to accept the command and
#: never answer it — which is how "the node is working on it" and "the node
#: went quiet" are both produced.
Handler = Callable[[pb.Command], "pb.CommandResult | None"]


def facts(**overrides: Any) -> pb.NodeFacts:
    """Plausible facts. Only the fields the control plane reads are set."""
    values: dict[str, Any] = {
        "hostname": "stub-node",
        "boot_id": "boot-1",
        "machine_id": "machine-1",
        "kernel": "Linux 6.11.0",
        # The current version, not a marker: a simulated node is running the
        # *current* agent, and the doctor reports a version difference as
        # needing a reinstall. Answering "stub" would make every simulated node
        # look out of date, which is a finding about the fixture rather than
        # about the node.
        "agent_version": __version__,
        "cpu_count": 20,
        "memory_bytes": 128 * 1024**3,
        "hardware_fingerprint": "fingerprint-1",
        # A simulated node has a Docker daemon that answers. An *empty*
        # docker_version is how the doctor is told a node's daemon is not
        # running, so leaving it blank would make every simulated node report a
        # broken daemon — a fact about the fixture, not about the node. A test
        # that wants a dead daemon says so by overriding it.
        "docker_version": MockDockerClient().version()["Version"],
    }
    values.update(overrides)
    return pb.NodeFacts(**values)


def answer_facts(command: pb.Command) -> pb.CommandResult:
    """The default handler: answer `get_facts`, refuse everything else.

    Refuse *definitely* — a `CommandFailure`, from a reachable node — because
    silence means something entirely different and a stub that was silent by
    accident would make every unreachable-path test pass for the wrong reason.
    """
    result = pb.CommandResult(command_id=command.command_id)
    if command.WhichOneof("op") == "get_facts":
        result.facts.CopyFrom(facts())
    else:
        result.failure.CopyFrom(
            pb.CommandFailure(
                type="NotImplementedError",
                message=f"the stub was not scripted for {command.WhichOneof('op')}",
            )
        )
    return result


async def enroll_at(
    target: str,
    token: str,
    *,
    trust_bundle_pem: bytes,
    trust_bundle_pin: str = "",
    directory,
    name: str = "",
) -> AgentIdentity:
    """Exchange a token for a certificate over a control plane's enrolment port.

    The client half of enrolment. The only other implementation of it is inside
    the Rust binary, and a test that needs to enrol *badly* — a mismatched pin,
    a spent token, a node that already has an identity — needs a client it can
    aim. Dialled by address rather than handed a server object, because that is
    what a node has: an installer gives it a target, a bundle file and a pin.
    """
    from pathlib import Path

    if trust_bundle_pin and ident.spki_pin(trust_bundle_pem) != trust_bundle_pin:
        raise RuntimeError(
            "the trust bundle does not match the pin it came with; refusing to "
            "enroll against it"
        )
    pair = ident.build_csr()
    credentials = grpc.ssl_channel_credentials(root_certificates=trust_bundle_pem)
    async with grpc.aio.secure_channel(target, credentials) as channel:
        issued = await pb_grpc.EnrollmentStub(channel).Enroll(
            pb.EnrollRequest(
                token=token, csr_pem=pair.csr_pem, requested_name=name, facts=facts()
            )
        )
    if trust_bundle_pin and issued.trust_bundle_spki != trust_bundle_pin:
        raise RuntimeError(
            "the control plane returned a trust bundle that does not match the "
            "pin the installer supplied"
        )
    return AgentIdentity(
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


async def enroll(
    server, name: str, directory, *, token: str = "", pin: str | None = None
) -> AgentIdentity:
    """Enrol against a :class:`ControlPlaneServer` this process is holding."""
    return await enroll_at(
        server.enrollment_target(),
        token or server.mint_token(name),
        trust_bundle_pem=server.trust_bundle_pem,
        trust_bundle_pin=server.trust_bundle_pin if pin is None else pin,
        directory=directory,
        name=name,
    )


class AgentStub:
    """One session, held open, answering whatever the test tells it to."""

    def __init__(
        self,
        identity: AgentIdentity,
        target: str,
        *,
        handler: Handler = answer_facts,
        claim_node_id: str | None = None,
        present_certificate: bool = True,
    ):
        self.identity = identity
        self.target = target
        self.handler = handler
        #: What the Hello claims. Differing from the certificate is how the
        #: "a certificate entitles its holder to exactly one identity" rule is
        #: tested from the only side that can violate it.
        self.claim_node_id = claim_node_id or identity.node_id
        self.present_certificate = present_certificate
        self.welcome: pb.Welcome | None = None
        self.commands: list[pb.Command] = []
        self.cancels: list[str] = []
        self.progress_sent: list[pb.Progress] = []
        self._outbox: asyncio.Queue[pb.AgentMessage] = asyncio.Queue()
        self._connected = asyncio.Event()
        #: Set when the session ends, whether by Welcome-then-EOF or by the
        #: server refusing it. `connect` waits on *both* this and the Welcome,
        #: so a refusal surfaces as the refusal rather than as a timeout —
        #: which is the difference between a test that says "PERMISSION_DENIED,
        #: and here is why" and one that says "something took ten seconds".
        self._ended = asyncio.Event()
        self._error: BaseException | None = None
        self._channel: Any = None
        self._call: Any = None
        self._task: asyncio.Task | None = None

    @property
    def node_id(self) -> str:
        return self.identity.node_id

    def _credentials(self) -> grpc.ChannelCredentials:
        if not self.present_certificate:
            return grpc.ssl_channel_credentials(
                root_certificates=self.identity.trust_bundle_pem
            )
        return grpc.ssl_channel_credentials(
            root_certificates=self.identity.trust_bundle_pem,
            private_key=self.identity.key_pem,
            certificate_chain=self.identity.certificate_pem,
        )

    async def connect(self, timeout: float = 10.0) -> AgentStub:
        """Open the session and wait for the Welcome."""
        self._channel = grpc.aio.secure_channel(self.target, self._credentials())
        stub = pb_grpc.NodeSessionStub(self._channel)
        call = self._call = stub.Session()
        await call.write(
            pb.AgentMessage(
                hello=pb.Hello(
                    node_id=self.claim_node_id,
                    agent_version=__version__,
                    facts=facts(),
                    known_epoch=0,
                )
            )
        )
        self._task = asyncio.create_task(self._run(call), name=f"stub-{self.node_id}")
        welcome = asyncio.ensure_future(self._connected.wait())
        ended = asyncio.ensure_future(self._ended.wait())
        try:
            await asyncio.wait(
                {welcome, ended},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            welcome.cancel()
            ended.cancel()
        if self._error is not None:
            raise self._error
        if not self._connected.is_set():
            raise TimeoutError(f"{self.node_id} was neither welcomed nor refused")
        return self

    async def _run(self, call) -> None:
        writer = asyncio.create_task(self._write_loop(call), name="stub-writer")
        try:
            while True:
                message = await call.read()
                if message == grpc.aio.EOF:
                    return
                body = message.WhichOneof("body")
                if body == "welcome":
                    self.welcome = message.welcome
                    self._connected.set()
                elif body == "command":
                    self.commands.append(message.command)
                    reply = self.handler(message.command)
                    if reply is not None:
                        self._outbox.put_nowait(pb.AgentMessage(result=reply))
                elif body == "cancel":
                    self.cancels.append(message.cancel.command_id)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — replayed to the caller
            self._error = exc
        finally:
            writer.cancel()
            self._ended.set()

    async def _write_loop(self, call) -> None:
        while True:
            await call.write(await self._outbox.get())

    def send(self, message: pb.AgentMessage) -> None:
        """Put anything on the stream. For heartbeats and progress events."""
        self._outbox.put_nowait(message)

    def beat(self, seq: int = 1, **fact_overrides: Any) -> None:
        """A heartbeat, optionally carrying different facts than the Hello did —
        which is how a reimage is detected rather than inferred."""
        self.send(
            pb.AgentMessage(
                heartbeat=pb.Heartbeat(
                    seq=seq, sent_unix=0, facts=facts(**fact_overrides)
                )
            )
        )

    def report_progress(self, command_id: str, **fields: Any) -> None:
        progress = pb.Progress(command_id=command_id, pull=pb.PullProgress(**fields))
        self.progress_sent.append(progress)
        self.send(pb.AgentMessage(progress=progress))

    def abandon(self) -> None:
        """Drop the session *synchronously*, the way losing power does.

        Cancelling the reader ends the gRPC call, which is what the control
        plane sees as the node going away — the channel close after it is
        housekeeping. It matters that this needs no ``await``: it is called
        from the cancellation path of the task holding a simulated unit, and a
        coroutine that is being cancelled cannot reliably await anything, so a
        teardown that needed to would leave the node connected after its unit
        was stopped.
        """
        if self._call is not None:
            # The *call* is what the control plane is holding. Cancelling the
            # reader task alone leaves the stream open, so the node stays
            # connected after its unit has been stopped — which is exactly the
            # state a doctor test needs to be able to produce.
            call, self._call = self._call, None
            call.cancel()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._channel is not None:
            channel, self._channel = self._channel, None
            asyncio.ensure_future(channel.close())

    async def close(self) -> None:
        """Drop the session and wait for the reader to finish."""
        if self._call is not None:
            call, self._call = self._call, None
            call.cancel()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass
            self._task = None
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
