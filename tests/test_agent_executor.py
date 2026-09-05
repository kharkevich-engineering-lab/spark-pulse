"""The codec and the executor, with no socket and no daemon.

These are the two places a per-node divergence could still be born — the codec
by writing a default down a second time, the executor by interpreting a result
instead of forwarding it — so they are tested directly rather than only
through the transport.
"""

from __future__ import annotations

import pytest

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.agent import codec
from spark_pulse.agent.executor import LocalExecutor
from spark_pulse.mock.docker import MockDockerClient, MockDockerService
from spark_pulse.tools.docker import ContainerInfo, ContainerMetadata, ExecResult


@pytest.fixture
def executor():
    return LocalExecutor(MockDockerService(MockDockerClient()))


METADATA = ContainerMetadata(deployment="dep-1", recipe="r", image="img:1")


# ── The presence discipline ─────────────────────────────────────────────────


def test_an_omitted_kwarg_is_not_sent_at_all():
    """ "The caller said nothing" must not become "the caller said zero".

    Every default therefore exists in exactly one place — DockerService — and
    cannot drift, because there is nowhere for a second copy to live.
    """
    message = codec.encode_run_container("img:1", "c1", {}, METADATA)
    decoded = codec.decode_run_container(message)
    assert set(decoded) == {"image", "name", "env_vars", "metadata"}
    for field in codec.RUN_OPTIONAL_FIELDS:
        assert not message.HasField(field)


def test_a_falsy_kwarg_that_was_given_is_sent():
    """privileged=False is an instruction, not an omission."""
    message = codec.encode_run_container(
        "img:1", "c1", {}, METADATA, privileged=False, shm_size_gb=0.0
    )
    decoded = codec.decode_run_container(message)
    assert decoded["privileged"] is False
    assert decoded["shm_size_gb"] == 0.0


def test_a_kwarg_the_protocol_does_not_carry_is_loud():
    """Silently dropping it would work locally and do nothing on a peer."""
    with pytest.raises(TypeError, match="not carried by the protocol"):
        codec.encode_run_container("img:1", "c1", {}, METADATA, invented=True)


def test_collections_round_trip_and_empty_means_absent():
    message = codec.encode_run_container(
        "img:1",
        "c1",
        {"A": "1"},
        METADATA,
        cache_dirs=["/a", "/b"],
        mounts={"/h": "/c"},
        ulimits={"memlock": "-1"},
        devices=["/dev/infiniband"],
        cap_add=["IPC_LOCK"],
        port_mappings=["8000:8000"],
    )
    decoded = codec.decode_run_container(message)
    assert decoded["cache_dirs"] == ["/a", "/b"]
    assert decoded["mounts"] == {"/h": "/c"}
    assert decoded["ulimits"] == {"memlock": "-1"}
    assert decoded["devices"] == ["/dev/infiniband"]
    assert decoded["cap_add"] == ["IPC_LOCK"]
    assert decoded["port_mappings"] == ["8000:8000"]
    # DockerService reads each of these as `x or []`, so None and empty are
    # already the same call and no presence bit is carried for them.
    assert "cache_dirs" not in codec.decode_run_container(
        codec.encode_run_container("img:1", "c1", {}, METADATA, cache_dirs=[])
    )


# ── Round trips ─────────────────────────────────────────────────────────────


def test_metadata_round_trips_including_its_nullables():
    original = ContainerMetadata(
        deployment="d",
        recipe="r",
        image="i",
        mode="solo",
        created_at=None,
        memory_limit_gb=None,
        generation=3,
        rank=1,
        world_size=2,
        cluster="c",
        role="worker",
        node_rank=1,
        head_ip="10.0.0.1",
        ray_enabled=False,
    )
    assert codec.decode_metadata(codec.encode_metadata(original)) == original


def test_a_command_keeps_argv_and_shell_apart():
    assert codec.decode_cmd(codec.encode_cmd("echo hi")) == "echo hi"
    assert codec.decode_cmd(codec.encode_cmd(["echo", "hi"])) == ["echo", "hi"]
    assert codec.encode_cmd(None) is None


def test_container_status_keeps_its_nulls():
    original = {
        "status": "missing",
        "running": False,
        "id": None,
        "state": {},
        "error": "Container 'x' not found",
    }
    assert codec.decode_container_status(codec.encode_container_status(original)) == (
        original
    )


def test_container_status_carries_dockers_state_opaquely():
    state = {"Status": "running", "ExitCode": 0, "Health": {"Status": "healthy"}}
    encoded = codec.encode_container_status(
        {"status": "running", "running": True, "id": "abc", "state": state}
    )
    assert codec.decode_container_status(encoded)["state"] == state


def test_exec_result_and_image_info_round_trip():
    result = ExecResult(returncode=2, stdout="out", stderr="err")
    assert codec.decode_exec_result(codec.encode_exec_result(result)) == result
    info = {
        "id": "sha256:abc",
        "size_bytes": 12,
        "created": "2026-01-01",
        "repo_tags": ["a:1"],
        "repo_digests": [],
    }
    assert codec.decode_image_info(codec.encode_image_info(info)) == info
    info["created"] = None
    assert codec.decode_image_info(codec.encode_image_info(info))["created"] is None


def test_container_info_round_trips():
    original = ContainerInfo(
        id="abc",
        name="c1",
        status="running",
        image="img:1",
        metadata=METADATA,
        labels={"spark-pulse.managed": "true"},
    )
    assert codec.decode_container_info(codec.encode_container_info(original)) == (
        original
    )


# ── The executor ────────────────────────────────────────────────────────────


def _command(**op) -> pb.Command:
    return pb.Command(command_id="cmd-1", epoch=1, **op)


def test_the_executor_runs_and_reports(executor):
    result = executor.execute(
        _command(run_container=codec.encode_run_container("img:1", "c1", {}, METADATA))
    )
    assert result.WhichOneof("outcome") == "container"
    assert result.container.container.name == "c1"

    status = executor.execute(
        _command(get_container_status=pb.GetContainerStatus(name="c1"))
    )
    assert status.status.running is True


def test_an_exception_becomes_a_failure_payload_not_a_raise(executor):
    """Reachable and definite. A transport error could not say that."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("docker daemon is not running")

    executor.docker.list_images = boom
    result = executor.execute(_command(list_images=pb.ListImages()))
    assert result.WhichOneof("outcome") == "failure"
    assert result.failure.type == "RuntimeError"
    assert "daemon is not running" in result.failure.message


def test_a_command_with_no_operation_is_a_failure_not_a_crash(executor):
    result = executor.execute(pb.Command(command_id="cmd-1"))
    assert result.failure.type == "ValueError"


def test_a_stale_epoch_is_refused_at_the_resource(executor):
    """Fencing is at the thing that owns the Docker daemon, not at an election."""
    executor.note_epoch(9)
    result = executor.execute(
        _command(get_container_status=pb.GetContainerStatus(name="c1"))
    )
    assert result.failure.type == "StaleEpochError"
    assert "newer control plane" in result.failure.message


def test_a_newer_epoch_is_adopted(executor):
    executor.execute(
        pb.Command(
            command_id="c",
            epoch=12,
            get_container_status=pb.GetContainerStatus(name="c1"),
        )
    )
    assert executor.epoch == 12


def test_the_executor_does_not_build_a_docker_client_to_report_facts(executor):
    """A daemon that is down must not stop a heartbeat."""
    bare = LocalExecutor()
    assert bare.docker_or_none is None
    result = bare.execute(_command(get_facts=pb.GetFacts()))
    assert result.facts.hostname
    assert bare.docker_or_none is None


def test_copy_stages_bytes_and_reuses_the_one_copy_routine(executor, tmp_path):
    seen = {}

    def record(container, local_path, remote_path, **kwargs):
        seen["args"] = (container, remote_path, kwargs)
        seen["content"] = open(local_path, "rb").read()
        return True

    executor.docker.copy_to_container = record
    result = executor.execute(
        _command(
            copy_to_container=pb.CopyToContainer(
                container="c1",
                remote_path="/opt/serve.sh",
                content=b"#!/bin/sh\n",
                source_name="serve.sh",
                mode=0o755,
                timeout=42,
            )
        )
    )
    assert result.boolean.value is True
    assert seen["content"] == b"#!/bin/sh\n"
    assert seen["args"] == ("c1", "/opt/serve.sh", {"timeout": 42})


def test_pull_progress_reaches_the_callback(executor):
    events: list[dict] = []
    result = executor.execute(
        _command(pull_image=pb.PullImage(ref="ghcr.io/x/y:1", want_progress=True)),
        progress=events.append,
    )
    assert result.pull.percent == 100.0
    assert events


def test_a_cancelled_pull_fails_definitely(executor):
    result = executor.execute(
        _command(pull_image=pb.PullImage(ref="ghcr.io/x/y:1")),
        cancel=lambda: True,
    )
    assert result.WhichOneof("outcome") == "failure"
    assert result.failure.type == "PullCancelled"


# ── The entry point ─────────────────────────────────────────────────────────


def test_an_existing_identity_plus_a_token_is_refused_loudly(tmp_path, capsys):
    """§3.1: converge or refuse loudly. Never silently ignore the token."""
    from spark_pulse.agent import identity as ident
    from spark_pulse.agent.__main__ import main
    from spark_pulse.agent.store import AgentIdentity

    ca = ident.CertificateAuthority.load_or_create(tmp_path / "ca")
    pair = ident.build_csr()
    issued = ca.issue_node_certificate(pair.csr_pem, "abc-123")
    AgentIdentity(
        directory=tmp_path / "agent",
        node_id="abc-123",
        key_pem=pair.key_pem,
        certificate_pem=issued.certificate_pem,
        trust_bundle_pem=ca.trust_bundle_pem,
        trust_bundle_pin=ca.trust_bundle_pin,
    ).save()

    code = main(
        [
            "--control",
            "127.0.0.1:8110",
            "--dir",
            str(tmp_path / "agent"),
            "--token",
            "some-token",
        ]
    )
    assert code == 2
    message = capsys.readouterr().err
    assert "already enrolled as abc-123" in message
    assert "--rotate" in message
    # Nothing was changed on disk.
    assert AgentIdentity.load(tmp_path / "agent").node_id == "abc-123"


def test_enrolling_without_the_things_it_needs_says_which(tmp_path, capsys):
    from spark_pulse.agent.__main__ import main

    code = main(["--control", "127.0.0.1:8110", "--dir", str(tmp_path / "agent")])
    assert code == 2
    message = capsys.readouterr().err
    assert "--token" in message and "--trust-bundle" in message
