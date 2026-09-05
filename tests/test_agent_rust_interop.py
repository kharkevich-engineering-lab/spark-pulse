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
            pytest.fail(
                "SPARK_PULSE_REQUIRE_RUST is set but there is no cargo on PATH"
            )
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
        "--control", server.session_target(),
        "--enroll-target", server.enrollment_target(),
        "--token-file", str(token_file),
        "--trust-bundle", str(overrides.pop("bundle_path", bundle)),
        "--pin", overrides.pop("pin", server.trust_bundle_pin),
        "--dir", str(directory),
        "--name", name,
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
