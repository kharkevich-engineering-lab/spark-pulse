"""The Rust agent, against the real Python control plane.

The point of these tests is that *nothing here is a stand-in*. A real
`ControlPlaneServer` with a real CA listens on a real loopback port; the
binary under test is the one that ships to a node; the TLS handshake, the
CSR, the certificate and the pin all cross a language boundary. Every one of
those is a place where two implementations can agree in a unit test and
disagree on the wire, and the only way to know is to run them against each
other.

This is also where the cost of the rewrite is kept honest. The Rust agent is
a second implementation of the node half of the protocol — the Python one is
being retired, but until it is gone both exist — and a second implementation
that is not driven by the same tests as the first is exactly the arrangement
`docs/transport-reexamined.md` §5.1 measured at thirty divergences. So the
Rust agent is tested through the control plane's own API, not through a Rust
mock of it.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from spark_pulse.agent import identity as ident

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parent.parent
CRATE = REPO / "agent"


def _cargo() -> str | None:
    return shutil.which("cargo")


@pytest.fixture(scope="session")
def rust_agent() -> Path:
    """The agent binary, built from source.

    Built rather than located, so a stale binary cannot pass a test the
    current source would fail. Skipped only when there is no Rust toolchain at
    all — never when the build fails, because a build failure is the finding.
    """
    cargo = _cargo()
    if cargo is None:
        # A skip here is how a cross-language suite quietly stops running. The
        # CI job that *does* have a toolchain sets this, so a missing cargo
        # there is a broken job rather than a machine without Rust.
        if os.environ.get("SPARK_PULSE_REQUIRE_RUST"):
            pytest.fail("SPARK_PULSE_REQUIRE_RUST is set but there is no cargo on PATH")
        pytest.skip("no cargo on PATH; the Rust agent cannot be built here")
    build = subprocess.run(
        [cargo, "build", "--quiet"],
        cwd=CRATE,
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        pytest.fail(f"the Rust agent does not build:\n{build.stderr[-4000:]}")
    binary = CRATE / "target" / "debug" / "spark-pulse-agent"
    assert binary.is_file(), f"{binary} was not produced by the build"
    return binary


async def run_agent(
    binary: Path, *args: str, timeout: int = 30
) -> subprocess.CompletedProcess:
    """Run the agent binary without blocking the control plane.

    ``asyncio.to_thread``, and it matters: the control plane under test is a
    ``grpc.aio`` server on *this* event loop, so a synchronous
    ``subprocess.run`` here blocks the loop that has to answer the child's
    enrolment request. The child then waits for a server that cannot run and
    the test waits for the child — a deadlock that looks exactly like a broken
    agent, and is not one.
    """
    return await asyncio.to_thread(
        subprocess.run,
        [str(binary), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


async def enroll_with_rust(
    binary: Path, server, tmp_path: Path, *, name: str = "spark-rust", **overrides
) -> subprocess.CompletedProcess:
    """Enrol one node using the Rust binary, as the installer would."""
    directory = overrides.pop("directory", tmp_path / "identity")
    bundle = tmp_path / "ca.pem"
    bundle.write_bytes(server.trust_bundle_pem)
    token_file = tmp_path / "token"
    token_file.write_text(overrides.pop("token", server.mint_token(name)) + "\n")
    args = [
        "--control",
        server.session_target(),
        "--enroll-target",
        server.enrollment_target(),
        "--token-file",
        str(token_file),
        "--trust-bundle",
        str(overrides.pop("bundle_path", bundle)),
        "--pin",
        overrides.pop("pin", server.trust_bundle_pin),
        "--dir",
        str(directory),
        "--name",
        name,
        "--enroll-only",
    ]
    args.extend(overrides.pop("extra", []))
    assert not overrides, f"unused overrides: {overrides}"
    return await run_agent(binary, *args)


# ── The binary itself ───────────────────────────────────────────────────────


async def test_help_exits_zero(rust_agent):
    """`--help` is what the installer runs to verify a freshly shipped binary.

    It has to load the whole program and exit 0, because that is the check
    standing between "unpacked" and "enrolled" — a binary for the wrong
    architecture fails here, named, with nothing yet started.
    """
    result = await run_agent(rust_agent, "--help")
    assert result.returncode == 0, result.stderr
    assert "--enroll-only" in result.stdout
    assert "--token-file" in result.stdout


# ── Enrolment, across the language boundary ─────────────────────────────────


async def test_the_rust_agent_enrolls_against_the_python_control_plane(
    rust_agent, agent_server, tmp_path
):
    """The whole handshake: TLS, CSR, certificate, pin — in two languages."""
    result = await enroll_with_rust(rust_agent, agent_server, tmp_path)
    assert result.returncode == 0, result.stderr

    node_id = result.stdout.strip()
    assert node_id, "--enroll-only prints the node id and nothing else"

    # The control plane agrees that this node exists and that it minted it.
    entry = agent_server.ledger.get(node_id)
    assert entry is not None
    assert entry.name == "spark-rust"


async def test_the_identity_it_writes_is_the_one_python_reads(
    rust_agent, agent_server, tmp_path
):
    """Both agents must be able to adopt an identity the other created.

    Same four files, same JSON keys. A node whose agent is replaced keeps its
    uuid and its certificate; if this drifts, an upgrade silently becomes a
    re-enrolment and the control plane ends up with two records for one
    machine.
    """
    from spark_pulse.agent.store import AgentIdentity

    result = await enroll_with_rust(rust_agent, agent_server, tmp_path)
    assert result.returncode == 0, result.stderr
    directory = tmp_path / "identity"

    loaded = AgentIdentity.load(directory)
    assert loaded is not None
    assert loaded.node_id == result.stdout.strip()
    assert loaded.trust_bundle_pin == agent_server.trust_bundle_pin
    assert loaded.certificate_pem.startswith(b"-----BEGIN CERTIFICATE-----")
    assert b"PRIVATE KEY" in loaded.key_pem
    assert loaded.spiffe_id.endswith(loaded.node_id)

    # The pin the Rust agent recorded is the pin Python computes over the very
    # bundle it stored — checked here rather than trusted, because this is the
    # value that decides which authority the node will believe tomorrow.
    assert ident.spki_pin(loaded.trust_bundle_pem) == loaded.trust_bundle_pin


async def test_the_certificate_names_the_node_it_was_issued_for(
    rust_agent, agent_server, tmp_path
):
    """The SPIFFE URI SAN is what the session listener authorises against."""
    result = await enroll_with_rust(rust_agent, agent_server, tmp_path)
    assert result.returncode == 0, result.stderr
    node_id = result.stdout.strip()

    from cryptography import x509

    cert = x509.load_pem_x509_certificate(
        (tmp_path / "identity" / "node.crt").read_bytes()
    )
    assert ident.spiffe_id_of(cert) == ident.node_spiffe_id(node_id)
    # And the reverse direction: the id the session listener will read back out
    # of this certificate is the id the node believes it has.
    assert ident.node_id_from_spiffe(ident.spiffe_id_of(cert)) == node_id


async def test_the_private_key_never_leaves_the_node(
    rust_agent, agent_server, tmp_path
):
    """Only the CSR crosses the wire, and the key is 0600 the moment it exists.

    NVIDIA's `discover-sparks` copies one shared *private* key to every node,
    which makes any single Spark a key to all of them. This asserts the
    opposite property for the implementation that replaced it.
    """
    result = await enroll_with_rust(rust_agent, agent_server, tmp_path)
    assert result.returncode == 0, result.stderr

    key_path = tmp_path / "identity" / "node.key"
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "identity").stat().st_mode & 0o777 == 0o700

    # The key is this node's alone: the control plane never saw it, so it
    # cannot appear anywhere in the CA's own state.
    key = key_path.read_bytes()
    for path in (agent_server.directory).rglob("*"):
        if path.is_file():
            assert key not in path.read_bytes(), f"the node key reached {path}"


# ── The refusals ────────────────────────────────────────────────────────────


async def test_a_substituted_trust_bundle_is_refused_before_it_is_trusted(
    rust_agent, agent_server, tmp_path
):
    """The pin's whole purpose, checked on the node rather than on the wire.

    An attacker who can put a different CA in front of the node cannot also
    change the pin the installer carried over SSH, so the mismatch is caught
    *before* the bundle is used as a root of trust.
    """
    from spark_pulse.agent.identity import CertificateAuthority

    other = CertificateAuthority.load_or_create(tmp_path / "impostor")
    substituted = tmp_path / "impostor.pem"
    substituted.write_bytes(other.certificate_pem)

    result = await enroll_with_rust(
        rust_agent, agent_server, tmp_path, bundle_path=substituted
    )

    assert result.returncode != 0
    assert "does not match the pin" in result.stderr
    assert not (tmp_path / "identity").exists(), "nothing may be written on refusal"


async def test_a_token_that_was_already_used_is_refused_with_the_reason(
    rust_agent, agent_server, tmp_path
):
    """An operator debugging a failed install needs which, not just "no"."""
    token = agent_server.mint_token("spark-rust")
    first = await enroll_with_rust(rust_agent, agent_server, tmp_path, token=token)
    assert first.returncode == 0, first.stderr

    second = await enroll_with_rust(
        rust_agent,
        agent_server,
        tmp_path,
        token=token,
        directory=tmp_path / "second",
    )
    assert second.returncode != 0
    assert "already used" in second.stderr


async def test_an_identity_plus_a_token_is_refused_loudly(
    rust_agent, agent_server, tmp_path
):
    """k0s silently ignores the token here, which is why re-enrolment there
    needs a full reset. This names the directory and says what to delete."""
    first = await enroll_with_rust(rust_agent, agent_server, tmp_path)
    assert first.returncode == 0, first.stderr
    node_id = first.stdout.strip()

    again = await enroll_with_rust(rust_agent, agent_server, tmp_path)

    assert again.returncode != 0
    assert node_id in again.stderr
    assert "--rotate" in again.stderr
    # And the identity it already had is untouched.
    meta = json.loads((tmp_path / "identity" / "identity.json").read_text())
    assert meta["node_id"] == node_id


async def test_rotate_destroys_the_identity_and_enrolls_again(
    rust_agent, agent_server, tmp_path
):
    """The explicit form of the choice the refusal above hands to the operator."""
    first = await enroll_with_rust(rust_agent, agent_server, tmp_path)
    assert first.returncode == 0, first.stderr

    rotated = await enroll_with_rust(
        rust_agent, agent_server, tmp_path, extra=["--rotate"]
    )

    assert rotated.returncode == 0, rotated.stderr
    assert rotated.stdout.strip() != first.stdout.strip()


async def test_a_partial_identity_directory_is_an_error_not_a_fresh_node(
    rust_agent, agent_server, tmp_path
):
    """Half an identity is a failed install. Answering "never enrolled" to it
    would enrol the machine a second time and orphan the first uuid."""
    result = await enroll_with_rust(rust_agent, agent_server, tmp_path)
    assert result.returncode == 0, result.stderr
    (tmp_path / "identity" / "node.crt").unlink()

    again = await enroll_with_rust(rust_agent, agent_server, tmp_path)

    assert again.returncode != 0
    assert "partial agent identity" in again.stderr
    assert "node.crt" in again.stderr


# ── The session ─────────────────────────────────────────────────────────────


class RunningAgent:
    """A Rust agent held open against the control plane, as a real one is."""

    def __init__(self, process, node_id: str):
        self.process = process
        self.node_id = node_id

    async def stop(self) -> int:
        """Ask it to stop the way systemd does, and wait."""
        if self.process.returncode is None:
            self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except asyncio.TimeoutError:  # pragma: no cover — it should stop
            self.process.kill()
            await self.process.wait()
        return self.process.returncode


async def start_agent(
    binary: Path, server, tmp_path: Path, *, name: str = "spark-rust"
) -> RunningAgent:
    """Enrol, then run — which is what the installer arranges on a node.

    Enrolment happens under `--enroll-only` while the installer watches, and
    the long-running process is then started with no token at all, so a
    restart of the unit can never be the refused identity-plus-token case.
    """
    enrolled = await enroll_with_rust(binary, server, tmp_path, name=name)
    assert enrolled.returncode == 0, enrolled.stderr
    node_id = enrolled.stdout.strip()

    process = await asyncio.create_subprocess_exec(
        str(binary),
        "--control",
        server.session_target(),
        "--dir",
        str(tmp_path / "identity"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    agent = RunningAgent(process, node_id)

    for _ in range(200):  # 10s, in 50ms steps
        if server.hub.is_connected(node_id):
            return agent
        if process.returncode is not None:
            _, err = await process.communicate()
            pytest.fail(f"the agent exited early:\n{err.decode()[-3000:]}")
        await asyncio.sleep(0.05)
    await agent.stop()
    pytest.fail(f"{node_id} never appeared in the hub")


async def test_the_agent_connects_and_the_hub_sees_it_healthy(
    rust_agent, agent_server, tmp_path
):
    """Liveness *is* the command channel: one stream, one fact about it."""
    from spark_pulse.agent.hub import Liveness

    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        assert agent_server.hub.connected() == [agent.node_id]
        assert agent_server.hub.liveness(agent.node_id) is Liveness.HEALTHY

        snapshot = agent_server.hub.nodes()[0]
        assert snapshot.node_id == agent.node_id
        assert snapshot.agent_version
        # The facts arrived on the Hello, before any command was sent.
        assert snapshot.facts.hostname
        assert snapshot.facts.kernel
    finally:
        await agent.stop()


async def test_it_keeps_beating_so_the_node_does_not_go_suspect(
    rust_agent, agent_server, tmp_path
):
    """A node is *suspect* at 15s of silence. This one must not be."""
    from spark_pulse.agent.hub import Liveness

    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        first = agent_server.hub._connections[agent.node_id].last_seen
        # Two heartbeat intervals plus slack.
        await asyncio.sleep(11)
        later = agent_server.hub._connections[agent.node_id].last_seen
        assert later > first, "no heartbeat arrived in eleven seconds"
        assert agent_server.hub.liveness(agent.node_id) is Liveness.HEALTHY
    finally:
        await agent.stop()


async def test_the_control_plane_can_ask_it_to_describe_itself(
    rust_agent, agent_server, tmp_path
):
    """A whole command round trip, through the control plane's own API.

    `NodeOperations` is what the rest of spark-pulse calls; driving the Rust
    agent through it rather than through a bespoke client is what makes this a
    test of the thing that ships.
    """
    from spark_pulse.agent.operations import NodeOperations

    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        facts = await NodeOperations(
            agent_server.hub, agent.node_id, timeout=20
        ).get_facts()

        assert facts.hostname
        assert facts.agent_version
        assert facts.cpu_count > 0
        assert facts.memory_bytes > 0
        # The fingerprint is what reimage detection compares on every beat, so
        # an agent that could not compute one would make every node look new.
        assert facts.hardware_fingerprint
    finally:
        await agent.stop()


async def test_a_failed_operation_is_definite_and_not_a_silence(
    rust_agent, agent_server, tmp_path, daemon
):
    """The distinction the whole transport exists to keep.

    Exec'ing into a container that is not there is a *definite* failure from a
    reachable node — `NodeOperationError` — and never `NodeUnreachable`, which
    means the outcome is unknown and would leave a caller free to release a
    GPU that is still in use. Confusing those two is the expensive direction.
    """
    from spark_pulse.agent.errors import NodeOperationError, NodeUnreachable
    from spark_pulse.agent.operations import NodeOperations

    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        ops = NodeOperations(agent_server.hub, agent.node_id, timeout=30)
        with pytest.raises(NodeOperationError) as caught:
            await ops.exec_in_container("no-such-container-anywhere", ["true"])

        assert caught.value.error_type == "RuntimeError"
        assert not isinstance(caught.value, NodeUnreachable)
        # And the node is still there. A refused command is not a lost node.
        assert agent_server.hub.is_connected(agent.node_id)

        # The mirror image: a *missing* container's logs are a string, not an
        # error, because every caller displays them and a 404 dump helps
        # nobody. Same node, same call shape, deliberately different answer.
        assert "not found" in await ops.get_logs("no-such-container-anywhere")
    finally:
        await agent.stop()


async def test_a_command_from_a_superseded_control_plane_is_refused(
    rust_agent, agent_server, tmp_path
):
    """Fencing happens at the resource, not at a leader election.

    The agent that owns the Docker daemon is the thing that refuses, so a
    command issued by a control plane that has since been replaced cannot act
    even if it is still in flight somewhere.
    """
    from spark_pulse.agent import agent_pb2 as pb

    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        # The hub's epoch reached the agent on the Welcome. Anything older than
        # that is a message from a control plane that has been superseded.
        stale = pb.Command(
            command_id="stale-1",
            epoch=max(agent_server.hub.epoch - 1, 0),
            get_facts=pb.GetFacts(),
        )
        assert (
            stale.epoch < agent_server.hub.epoch
        ), "the fixture needs an epoch there is something older than"

        result = await agent_server.hub.call(agent.node_id, stale, timeout=20)

        assert result.WhichOneof("outcome") == "failure"
        assert result.failure.type == "StaleEpochError"
        assert "older than" in result.failure.message
        # Fencing is a refusal, not a disconnection: the node stays connected
        # and will happily serve the *current* control plane's commands.
        assert agent_server.hub.is_connected(agent.node_id)
    finally:
        await agent.stop()


async def test_sigterm_ends_the_session_cleanly(rust_agent, agent_server, tmp_path):
    """systemd sends SIGTERM on stop and on an upgrade restart.

    An agent that ignored it would be killed, and the control plane would read
    a node that was told to stop as a node that vanished — which is the state
    that holds a rank's GPU rather than releasing it.
    """
    agent = await start_agent(rust_agent, agent_server, tmp_path)
    code = await agent.stop()

    assert code == 0, "SIGTERM should be a clean exit, not a kill"
    for _ in range(100):
        if not agent_server.hub.is_connected(agent.node_id):
            break
        await asyncio.sleep(0.05)
    assert not agent_server.hub.is_connected(agent.node_id)


# ── Against a real Docker daemon ────────────────────────────────────────────
#
# The agent's read, exec, copy and teardown paths, driven through the control
# plane, against a container the *Python* service's own label code created.
# That direction is deliberate: `prepare_labels` runs in Python and
# `labels::from_labels` runs in Rust, so a disagreement about the label
# vocabulary — the source of truth reconciliation rebuilds everything from —
# fails here rather than on a node.
#
# The container is created through the raw SDK rather than through
# `DockerService.run_container`, because that always requests a GPU and a test
# machine has none. The *shaping* of that request is covered by
# `create_config`'s unit tests in `agent/src/executor/containers.rs`.

TEST_IMAGE = "docker.io/library/busybox:latest"


def _real_docker():
    """The real Docker SDK client, or None. Never the simulation one."""
    try:
        import docker as docker_sdk

        client = docker_sdk.from_env()
        client.ping()
        return client
    except Exception:
        return None


@pytest.fixture(scope="session")
def daemon():
    client = _real_docker()
    if client is None:
        pytest.skip("no Docker daemon reachable; the executor cannot be exercised")
    try:
        client.images.get(TEST_IMAGE)
    except Exception:
        try:
            client.images.pull(TEST_IMAGE)
        except Exception as exc:  # pragma: no cover — no registry access
            pytest.skip(f"cannot obtain {TEST_IMAGE}: {exc}")
    return client


@pytest.fixture
def managed_container(daemon):
    """A running container carrying real spark-pulse labels."""
    import importlib

    real_docker = importlib.import_module("spark_pulse.tools.docker")
    name = f"spark-pulse-rusttest-{os.getpid()}"
    metadata = real_docker.ContainerMetadata(
        deployment="dep-rust",
        recipe="qwen3",
        image=TEST_IMAGE,
        generation=2,
        rank=1,
        world_size=3,
    )
    labels = real_docker.prepare_labels(metadata, name, TEST_IMAGE)

    for stale in daemon.containers.list(all=True, filters={"name": name}):
        stale.remove(force=True)
    container = daemon.containers.run(
        TEST_IMAGE,
        name=name,
        command=["sleep", "600"],
        labels=labels,
        detach=True,
    )
    try:
        yield container, metadata, labels
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass


async def test_the_agent_reads_back_the_labels_python_wrote(
    rust_agent, agent_server, tmp_path, managed_container
):
    """The label vocabulary, across two implementations of it.

    Container labels are the source of truth reconciliation rebuilds the whole
    view of a cluster from. If Rust's `from_labels` and Python's
    `prepare_labels` disagree about one key, a control plane restart loses a
    deployment — or invents one.
    """
    from spark_pulse.agent.operations import NodeOperations

    container, metadata, labels = managed_container
    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        ops = NodeOperations(agent_server.hub, agent.node_id, timeout=30)
        found = await ops.list_managed_containers()

        mine = [c for c in found if c.name == container.name]
        assert (
            mine
        ), f"the agent did not see {container.name} among {[c.name for c in found]}"
        info = mine[0]

        assert info.metadata.deployment == "dep-rust"
        assert info.metadata.recipe == "qwen3"
        assert info.metadata.generation == 2
        assert info.metadata.rank == 1
        assert info.metadata.world_size == 3
        assert info.status == "running"
        # Every label Python stamped came back byte for byte.
        assert dict(info.labels) == labels
    finally:
        await agent.stop()


async def test_finding_a_container_by_deployment_and_by_recipe(
    rust_agent, agent_server, tmp_path, managed_container
):
    from spark_pulse.agent.operations import NodeOperations

    container, _metadata, _labels = managed_container
    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        ops = NodeOperations(agent_server.hub, agent.node_id, timeout=30)

        found = await ops.get_container_by_deployment("dep-rust")
        assert found is not None and found.name == container.name
        assert await ops.get_container_by_deployment("no-such-deployment") is None

        by_recipe = await ops.get_container_by_recipe("qwen3")
        assert [c.name for c in by_recipe] == [container.name]
    finally:
        await agent.stop()


async def test_status_exec_logs_and_teardown(
    rust_agent, agent_server, tmp_path, managed_container
):
    """The lifecycle a deploy actually drives, end to end through the agent."""
    from spark_pulse.agent.operations import NodeOperations

    container, _metadata, _labels = managed_container
    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        ops = NodeOperations(agent_server.hub, agent.node_id, timeout=60)

        status = await ops.get_container_status(container.name)
        assert status["status"] == "running"
        assert status["running"] is True
        assert status["id"]

        result = await ops.exec_in_container(container.name, ["echo", "hello"])
        assert result.returncode == 0
        assert "hello" in result.stdout

        failed = await ops.exec_in_container(container.name, ["false"])
        assert failed.returncode != 0, "a non-zero exit must survive the round trip"

        # `stop_container` stops **and removes**. A container that is merely
        # stopped still owns its name and its ports, so the next deploy of the
        # same rank collides with the corpse of the last one.
        assert await ops.stop_container(container.name) is True
        gone = await ops.get_container_status(container.name)
        assert gone["status"] == "missing"
        assert gone["running"] is False

        # And stopping something that is not there is False, not an error.
        assert await ops.stop_container(container.name) is False
    finally:
        await agent.stop()


async def test_copying_a_file_in_needs_no_docker_cli_on_the_node(
    rust_agent, agent_server, tmp_path, managed_container
):
    """The bytes travel as payload and land with their permission bits.

    The Python service shells out to `docker cp`, so a node needs the Docker
    *CLI* installed beside the daemon. This speaks the Engine API's archive
    endpoint over the same socket everything else uses — the executable bit
    matters because what is most often copied this way is a serve script.
    """
    from spark_pulse.agent.operations import NodeOperations

    container, _metadata, _labels = managed_container
    script = tmp_path / "serve.sh"
    script.write_text("#!/bin/sh\necho served\n")
    script.chmod(0o755)

    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        ops = NodeOperations(agent_server.hub, agent.node_id, timeout=60)
        assert await ops.copy_to_container(container.name, str(script), "/tmp/serve.sh")

        listing = await ops.exec_in_container(
            container.name, ["ls", "-l", "/tmp/serve.sh"]
        )
        assert listing.returncode == 0
        assert "rwxr-xr-x" in listing.stdout, listing.stdout
        ran = await ops.exec_in_container(container.name, ["/tmp/serve.sh"])
        assert ran.returncode == 0
        assert "served" in ran.stdout
    finally:
        await agent.stop()


async def test_copying_a_directory_keeps_its_shape(
    rust_agent, agent_server, tmp_path, managed_container
):
    """A mod with a subdirectory — the shape that used to be lost on a peer."""
    from spark_pulse.agent.operations import NodeOperations

    container, _metadata, _labels = managed_container
    root = tmp_path / "mod"
    (root / "templates").mkdir(parents=True)
    (root / "run.sh").write_text("#!/bin/sh\necho mod\n")
    (root / "templates" / "chat.jinja").write_text("{{ messages }}\n")

    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        ops = NodeOperations(agent_server.hub, agent.node_id, timeout=60)
        assert await ops.copy_to_container(container.name, str(root), "/tmp/mod")

        found = await ops.exec_in_container(
            container.name, ["sh", "-c", "cd /tmp/mod && find . -type f | sort"]
        )
        assert found.returncode == 0
        assert found.stdout.split() == ["./run.sh", "./templates/chat.jinja"]
    finally:
        await agent.stop()


async def test_image_presence_inspection_and_listing(
    rust_agent, agent_server, tmp_path, daemon
):
    from spark_pulse.agent.operations import NodeOperations

    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        ops = NodeOperations(agent_server.hub, agent.node_id, timeout=60)

        assert await ops.image_exists(TEST_IMAGE) is True
        assert await ops.image_exists("ghcr.io/example/never-pulled:1") is False

        info = await ops.image_info(TEST_IMAGE)
        assert info is not None
        assert info["id"].startswith("sha256:")
        assert info["size_bytes"] > 0
        assert (
            any(
                TEST_IMAGE.endswith(tag.split("/")[-1]) or tag in TEST_IMAGE
                for tag in info["repo_tags"]
            )
            or info["repo_tags"]
        )

        assert await ops.image_info("ghcr.io/example/never-pulled:1") is None

        listing = await ops.list_images()
        assert any(i["id"] == info["id"] for i in listing)
    finally:
        await agent.stop()


async def test_ensure_directories_creates_them_and_reports_what_it_could_not(
    rust_agent, agent_server, tmp_path
):
    """Docker invents a missing bind source **owned by root**, and every path
    here is one of the login user's caches — so this runs before the container
    does. A failure is a warning, not an error: docker will still start."""
    from spark_pulse.agent.operations import NodeOperations

    wanted = tmp_path / "cache" / "vllm"
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")

    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        ops = NodeOperations(agent_server.hub, agent.node_id, timeout=30)
        failed = await ops.ensure_directories([str(wanted), str(blocker / "under")])

        assert wanted.is_dir()
        assert failed == [str(blocker / "under")]
        # Empty and whitespace-only paths are skipped rather than failed.
        assert await ops.ensure_directories(["", "   "]) == []
    finally:
        await agent.stop()


PULL_IMAGE = "docker.io/library/hello-world:latest"


async def test_a_pull_reports_aggregated_progress_and_a_final_outcome(
    rust_agent, agent_server, tmp_path, daemon
):
    """Progress is aggregated across layers, and it is never an outcome.

    A caller wants one number, not one per layer — a 40 GB engine image has
    dozens reporting independently. And the pull is finished when the *result*
    arrives, not when a progress event says 100: the last event is emitted
    unthrottled precisely so the two agree, but only one of them is the answer.
    """
    from spark_pulse.agent.operations import NodeOperations

    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        ops = NodeOperations(agent_server.hub, agent.node_id, timeout=180)
        seen: list[dict] = []

        outcome = await ops.pull_image(PULL_IMAGE, progress=seen.append, interval=0.0)

        assert outcome["ref"] == PULL_IMAGE
        assert outcome["repository"] == "docker.io/library/hello-world"
        assert outcome["tag"] == "latest"
        assert outcome["percent"] == 100.0
        assert outcome["id"].startswith("sha256:")
        assert outcome["size_bytes"] > 0

        assert seen, "want_progress was set, so events must arrive"
        assert seen[-1]["percent"] == 100.0
        assert seen[-1]["status"] == "pull complete"
        assert all(event["ref"] == PULL_IMAGE for event in seen)

        # The pull actually landed the image, which is the only thing the
        # caller was really asking for.
        assert await ops.image_exists(PULL_IMAGE) is True
    finally:
        await agent.stop()


async def test_a_pull_of_something_that_does_not_exist_fails_definitely(
    rust_agent, agent_server, tmp_path, daemon
):
    """A registry saying no is a definite failure, not an unknown outcome."""
    from spark_pulse.agent.errors import NodeOperationError, NodeUnreachable
    from spark_pulse.agent.operations import NodeOperations

    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        ops = NodeOperations(agent_server.hub, agent.node_id, timeout=120)
        with pytest.raises(NodeOperationError) as caught:
            await ops.pull_image("docker.io/library/spark-pulse-no-such-image:0")

        assert not isinstance(caught.value, NodeUnreachable)
        assert "failed" in caught.value.error_message
        assert agent_server.hub.is_connected(agent.node_id)
    finally:
        await agent.stop()


async def test_a_pull_cancelled_before_it_starts_never_reaches_the_node(
    rust_agent, agent_server, tmp_path, daemon
):
    """Already withdrawn is refused here, deterministically, with no bytes sent.

    Sending it anyway and chasing it with a Cancel is strictly worse: it starts
    work nobody wants, and whether the Cancel wins the race decides what the
    caller sees. `PullCancelled` is what `native_runtime` and `images` catch by
    type to record a teardown as a teardown rather than a deployment failure.
    """
    from spark_pulse.tools.docker import PullCancelled
    from spark_pulse.agent.errors import NodeOperationError
    from spark_pulse.agent.operations import NodeOperations

    agent = await start_agent(rust_agent, agent_server, tmp_path)
    try:
        ops = NodeOperations(agent_server.hub, agent.node_id, timeout=60)
        with pytest.raises(NodeOperationError) as caught:
            await ops.pull_image(PULL_IMAGE, cancel=lambda: True)
        assert caught.value.error_type == "PullCancelled"

        # And through the synchronous face the control plane really holds, the
        # type a caller catches is the local one.
        from spark_pulse.agent.sync_service import AgentNodeService

        service = AgentNodeService(
            agent_server.hub, agent.node_id, asyncio.get_running_loop(), timeout=60
        )
        with pytest.raises(PullCancelled):
            await asyncio.to_thread(
                service.pull_image, PULL_IMAGE, None, 0.0, lambda: True
            )
    finally:
        await agent.stop()
