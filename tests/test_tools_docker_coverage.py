"""What ``DockerService`` actually asks the daemon for, and how it fails.

The contract suite proves the three container services agree with each other.
This file goes one level down on the local one: the exact ``containers.run``
call a deployment's requirements turn into, the state read back out of a
container, and what happens when the daemon says no or is not there at all.

Everything runs against an in-memory client injected into ``DockerService``,
so no daemon, no network and no sleeping. The errors raised at the boundary
are the **real** ``docker.errors`` classes, because the string-matching
fallbacks in this module are exactly the kind of thing a hand-rolled
exception hides — the mock's ``NotFound("Container x not found")`` matches a
``"not found" in str(exc)`` test that the SDK's
``NotFound("... No such container: x")`` does not.
"""

from __future__ import annotations

import importlib
import logging
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import docker as docker_sdk
import pytest

from spark_pulse.tools.docker import (
    ContainerInfo,
    ContainerMetadata,
    DockerService,
    ExecResult,
    _close_quietly,
    _decode,
    _labels_match,
    _watched_chunks,
    split_ref,
)
from spark_pulse.tools.labels import (
    CREATED_AT_LABEL,
    DEPLOYMENT_LABEL,
    MANAGED_LABEL,
    NAME_LABEL,
    RECIPE_LABEL,
)

# pytest-env forces SIMULATION_MODE=1, so the package attribute
# ``spark_pulse.tools.docker`` is the mock re-export. The module singleton and
# its convenience wrappers under test live in the real module.
real_docker = importlib.import_module("spark_pulse.tools.docker")

IMAGE = "ghcr.io/example/engine:1.4.0"
GB = 1024 * 1024 * 1024


# ── An in-memory docker client that records what it was asked for ───────────


class _FakeExec:
    """A stand-in for ``ExecResult`` as the SDK returns it."""

    def __init__(self, exit_code: int = 0, output: Any = (b"", None)):
        self.exit_code = exit_code
        self.output = output


class _FakeContainer:
    def __init__(self, image: str, name: str, labels: dict[str, str] | None):
        self.id = f"id-{name}"
        self.name = name
        self.status = "running"
        self.image = image
        self.labels = dict(labels or {})
        self.attrs = {"State": {"Status": "running", "Running": True, "Pid": 41}}
        self.exec_result = _FakeExec(output=(b"hello\n", b"warn\n"))
        self.exec_calls: list[dict[str, Any]] = []
        self.log_bytes: Any = b""
        self.log_tails: list[int] = []
        self.stopped_with: int | None = None
        self.removed_force: bool | None = None

    def stop(self, timeout: int = 10) -> None:
        self.stopped_with = timeout
        self.status = "exited"

    def remove(self, force: bool = False) -> None:
        self.removed_force = force

    def exec_run(self, command, demux=False, detach=False, **_kw):
        self.exec_calls.append({"command": command, "demux": demux, "detach": detach})
        return self.exec_result

    def logs(self, tail: int = 200, **_kw):
        self.log_tails.append(tail)
        return self.log_bytes


class _FakeContainers:
    """``client.containers``: a dict, plus knobs to make the daemon misbehave."""

    def __init__(self):
        self.store: dict[str, _FakeContainer] = {}
        # (image, kwargs, labels-as-handed-over) — the labels are snapshotted
        # because run_container mutates the dict it passed after the call.
        self.run_calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.run_error: BaseException | None = None
        self.get_error: BaseException | None = None
        self.honour_filters = True

    def run(self, image: str, **kwargs: Any) -> _FakeContainer:
        self.run_calls.append((image, kwargs, dict(kwargs.get("labels") or {})))
        if self.run_error is not None:
            raise self.run_error
        container = _FakeContainer(image, kwargs.get("name", ""), kwargs.get("labels"))
        self.store[container.name] = container
        return container

    def get(self, name: str) -> _FakeContainer:
        if self.get_error is not None:
            raise self.get_error
        if name not in self.store:
            raise docker_sdk.errors.NotFound(f"No such container: {name}")
        return self.store[name]

    def list(self, all=False, filters=None):  # noqa: A002 — mirrors the SDK kwarg
        found = list(self.store.values())
        if not all:
            found = [c for c in found if c.status == "running"]
        if not self.honour_filters:
            return found
        raw = (filters or {}).get("label") or []
        for term in [raw] if isinstance(raw, str) else list(raw):
            key, sep, value = term.partition("=")
            if sep:
                found = [c for c in found if c.labels.get(key) == value]
            else:
                found = [c for c in found if key in c.labels]
        return found


class _FakeImage:
    def __init__(self, ref: str, size: int = 7):
        self.id = f"sha256:{ref}"
        self.tags = [ref]
        self.attrs = {
            "Id": self.id,
            "Size": size,
            "Created": "2026-02-03T04:05:06Z",
            "RepoTags": [ref],
            "RepoDigests": [f"{ref.split(':')[0]}@{self.id}"],
        }


class _FakeImages:
    def __init__(self):
        self.store: dict[str, _FakeImage] = {IMAGE: _FakeImage(IMAGE, size=26 * GB)}
        self.get_error: BaseException | None = None
        self.list_error: BaseException | None = None
        self.remove_error: BaseException | None = None
        self.remove_calls: list[tuple[str, bool]] = []
        self.get_calls: list[str] = []

    def get(self, ref: str) -> _FakeImage:
        self.get_calls.append(ref)
        if self.get_error is not None:
            raise self.get_error
        if ref not in self.store:
            raise docker_sdk.errors.ImageNotFound(f"No such image: {ref}")
        return self.store[ref]

    def list(self, **_kw):
        if self.list_error is not None:
            raise self.list_error
        return list(self.store.values())

    def remove(self, ref: str, force: bool = False, **_kw) -> None:
        self.remove_calls.append((ref, force))
        if self.remove_error is not None:
            raise self.remove_error
        if ref not in self.store:
            raise docker_sdk.errors.ImageNotFound(f"No such image: {ref}")
        del self.store[ref]


class _FakeAPI:
    """``client.api``: only ``pull``, which is all this module uses it for."""

    def __init__(self):
        self.chunks: Any = []
        self.calls: list[dict[str, Any]] = []

    def pull(self, repository, tag="latest", stream=True, decode=True, **_kw):
        self.calls.append(
            {"repository": repository, "tag": tag, "stream": stream, "decode": decode}
        )
        return iter(self.chunks) if isinstance(self.chunks, list) else self.chunks


class _FakeClient:
    def __init__(self):
        self.containers = _FakeContainers()
        self.images = _FakeImages()
        self.api = _FakeAPI()


@pytest.fixture
def client() -> _FakeClient:
    return _FakeClient()


@pytest.fixture
def service(client: _FakeClient) -> DockerService:
    return DockerService(client=client)


def _metadata(deployment: str = "dep", **overrides) -> ContainerMetadata:
    fields = {"deployment": deployment, "recipe": "qwen3", "image": IMAGE}
    fields.update(overrides)
    return ContainerMetadata(**fields)


def _run(service: DockerService, client: _FakeClient, **overrides):
    """Start a container and hand back ``(ContainerInfo, run kwargs, labels)``."""
    params: dict[str, Any] = {
        "image": IMAGE,
        "name": "rank-0",
        "env_vars": {"VLLM_MODEL": "qwen"},
        "metadata": _metadata(),
    }
    params.update(overrides)
    info = service.run_container(**params)
    _image, kwargs, labels = client.containers.run_calls[-1]
    return info, kwargs, labels


# ── The run_container argument builder ──────────────────────────────────────


class TestRunContainerDefaults:
    """What every managed container asks for, whatever the recipe wants."""

    def test_the_defaults_reach_the_daemon_verbatim(self, service, client):
        info, kwargs, _labels = _run(service, client)

        assert client.containers.run_calls[0][0] == IMAGE
        assert kwargs["name"] == "rank-0"
        assert kwargs["detach"] is True
        assert kwargs["environment"] == {"VLLM_MODEL": "qwen"}
        assert kwargs["privileged"] is True
        assert kwargs["remove"] is True
        assert kwargs["pids_limit"] == 4096
        assert kwargs["shm_size"] == "64g"
        assert kwargs["volumes"] == {}
        # A rank is one member of a sharded gang; a rebooting node must not
        # resurrect it into a deployment that was torn down.
        assert kwargs["restart_policy"] == {"Name": "no"}
        # Every GPU, always.
        assert [dict(r) for r in kwargs["device_requests"]] == [
            {
                "Driver": "",
                "Count": -1,
                "DeviceIDs": [],
                "Capabilities": [["gpu"]],
                "Options": {},
            }
        ]
        assert isinstance(info, ContainerInfo)
        assert (info.id, info.name, info.status, info.image) == (
            "id-rank-0",
            "rank-0",
            "running",
            IMAGE,
        )

    def test_no_memory_limit_means_no_memory_kwargs(self, service, client):
        """An unset limit must not become an accidental zero."""
        _info, kwargs, _labels = _run(service, client, memory_limit_gb=None)

        assert "mem_limit" not in kwargs
        assert "memswap_limit" not in kwargs

    def test_a_memory_limit_carries_ten_gb_of_swap_headroom(self, service, client):
        _info, kwargs, _labels = _run(service, client, memory_limit_gb=110)

        assert kwargs["mem_limit"] == 110 * GB
        assert kwargs["memswap_limit"] == 120 * GB

    def test_the_container_is_labelled_with_its_identity_and_its_name(
        self, service, client
    ):
        info, kwargs, labels = _run(
            service, client, name="spark-pulse-dep-r0-g1", metadata=_metadata("dep")
        )

        assert labels[MANAGED_LABEL] == "true"
        assert labels[DEPLOYMENT_LABEL] == "dep"
        assert labels[RECIPE_LABEL] == "qwen3"
        assert labels[NAME_LABEL] == "spark-pulse-dep-r0-g1"
        # The labels the daemon got are the labels the caller is told about.
        assert info.labels == labels

    def test_the_creation_time_is_stamped_on_the_container_not_just_returned(
        self, service, client
    ):
        """The timestamp has to survive the process that started the container.

        Reconciliation rebuilds deployments from labels alone, so a
        ``created_at`` that only ever existed on the returned object means
        every restart reports the container as brand new.
        """
        info, _kwargs, labels = _run(service, client)

        assert labels[CREATED_AT_LABEL], "the daemon was handed an empty created_at"
        assert labels[CREATED_AT_LABEL] == info.metadata.created_at
        # Readable back out of the labels alone.
        assert ContainerMetadata.from_labels(labels).created_at == (
            info.metadata.created_at
        )

    def test_a_caller_supplied_creation_time_is_kept(self, service, client):
        """The deploy planner stamps one time for the whole gang; keep it."""
        planned = "2026-01-01T00:00:00+00:00"

        info, _kwargs, labels = _run(
            service, client, metadata=_metadata(created_at=planned)
        )

        assert labels[CREATED_AT_LABEL] == planned
        assert info.metadata.created_at == planned


class TestRunContainerMounts:
    """Cache directories and explicit binds become one volume map."""

    def test_cache_dirs_mount_at_the_same_path(self, service, client):
        _info, kwargs, _labels = _run(
            service, client, cache_dirs=["/mnt/hf", "/mnt/models"]
        )

        assert kwargs["volumes"] == {
            "/mnt/hf": {"bind": "/mnt/hf", "mode": "rw"},
            "/mnt/models": {"bind": "/mnt/models", "mode": "rw"},
        }

    def test_explicit_mounts_are_added_and_win_over_a_cache_dir(self, service, client):
        _info, kwargs, _labels = _run(
            service,
            client,
            cache_dirs=["/mnt/hf"],
            mounts={"/mnt/hf": "/root/.cache/huggingface", "/etc/pulse": "/etc/pulse"},
        )

        assert kwargs["volumes"] == {
            "/mnt/hf": {"bind": "/root/.cache/huggingface", "mode": "rw"},
            "/etc/pulse": {"bind": "/etc/pulse", "mode": "rw"},
        }


class TestRunContainerUlimits:
    """``nofile`` is a named argument; everything else rides in ``ulimits``."""

    @staticmethod
    def _limits(kwargs) -> list[tuple[str, int, int]]:
        return [(u.name, u.soft, u.hard) for u in kwargs["ulimits"]]

    def test_nofile_comes_from_its_own_argument(self, service, client):
        _info, kwargs, _labels = _run(service, client, nofile_limit=65536)

        assert self._limits(kwargs) == [("nofile", 65536, 65536)]

    def test_extra_ulimits_take_a_soft_and_an_optional_hard_value(
        self, service, client
    ):
        _info, kwargs, _labels = _run(
            service,
            client,
            nofile_limit=1024,
            ulimits={"memlock": "-1", "stack": "67108864:134217728"},
        )

        assert self._limits(kwargs) == [
            ("nofile", 1024, 1024),
            ("memlock", -1, -1),
            ("stack", 67108864, 134217728),
        ]

    def test_a_ulimits_entry_cannot_shadow_nofile(self, service, client):
        """Two ``nofile`` ulimits is an error the daemon would reject."""
        _info, kwargs, _labels = _run(
            service, client, nofile_limit=1024, ulimits={"nofile": "8"}
        )

        assert self._limits(kwargs) == [("nofile", 1024, 1024)]


class TestRunContainerNetworking:
    """Host networking, and the ports that turn it off."""

    def test_no_ports_means_host_networking(self, service, client):
        _info, kwargs, _labels = _run(service, client)

        assert kwargs["network_mode"] == "host"
        assert "ports" not in kwargs

    def test_published_ports_drop_host_networking(self, service, client):
        _info, kwargs, _labels = _run(
            service, client, port_mappings=["8000:8000", "9001:9000"]
        )

        assert "network_mode" not in kwargs
        assert kwargs["ports"] == {"8000": 8000, "9000": 9001}

    def test_network_host_true_overrides_the_published_ports_rule(
        self, service, client
    ):
        _info, kwargs, _labels = _run(
            service, client, network_host=True, port_mappings=["8000:8000"]
        )

        assert kwargs["network_mode"] == "host"

    def test_network_host_false_keeps_the_container_off_the_host_network(
        self, service, client
    ):
        _info, kwargs, _labels = _run(service, client, network_host=False)

        assert "network_mode" not in kwargs


class TestRunContainerCapabilities:
    """Privileged grants everything; unprivileged still needs to pin memory."""

    def test_a_privileged_container_asks_for_no_extra_capabilities(
        self, service, client
    ):
        _info, kwargs, _labels = _run(service, client, privileged=True)

        assert kwargs["privileged"] is True
        assert "cap_add" not in kwargs

    def test_an_unprivileged_container_always_gets_ipc_lock(self, service, client):
        """Without it the engine cannot pin the pages it mmaps the weights into."""
        _info, kwargs, _labels = _run(service, client, privileged=False)

        assert kwargs["privileged"] is False
        assert kwargs["cap_add"] == ["IPC_LOCK"]

    def test_ipc_lock_is_added_to_the_requested_capabilities(self, service, client):
        _info, kwargs, _labels = _run(
            service, client, privileged=False, cap_add=["SYS_NICE"]
        )

        assert kwargs["cap_add"] == ["SYS_NICE", "IPC_LOCK"]

    def test_ipc_lock_is_not_asked_for_twice(self, service, client):
        _info, kwargs, _labels = _run(
            service, client, privileged=False, cap_add=["IPC_LOCK", "SYS_NICE"]
        )

        assert kwargs["cap_add"] == ["IPC_LOCK", "SYS_NICE"]


class TestRunContainerRuntimeShape:
    """Entrypoint, command, devices, IPC and auto-removal."""

    def test_the_entrypoint_is_cleared_by_default(self, service, client):
        """An engine image's entrypoint would ignore the command we hand it."""
        _info, kwargs, _labels = _run(service, client)

        assert kwargs["entrypoint"] == []

    def test_keeping_the_entrypoint_passes_none(self, service, client):
        _info, kwargs, _labels = _run(service, client, entrypoint_clear=False)

        assert kwargs["entrypoint"] is None

    def test_no_command_is_not_the_same_as_an_empty_one(self, service, client):
        _info, kwargs, _labels = _run(service, client, command=None)

        assert "command" not in kwargs

    def test_the_idle_command_the_native_runtime_starts_with(self, service, client):
        _info, kwargs, _labels = _run(service, client, command=["sleep", "infinity"])

        assert kwargs["command"] == ["sleep", "infinity"]

    def test_devices_are_exposed_read_write_and_mknod(self, service, client):
        _info, kwargs, _labels = _run(
            service, client, devices=["/dev/infiniband", "/dev/nvidia0"]
        )

        assert kwargs["devices"] == [
            "/dev/infiniband:/dev/infiniband:rwm",
            "/dev/nvidia0:/dev/nvidia0:rwm",
        ]

    def test_ipc_host_shares_the_host_namespace(self, service, client):
        _info, kwargs, _labels = _run(service, client, ipc_host=True)

        assert kwargs["ipc_mode"] == "host"

    def test_ipc_host_off_leaves_the_namespace_alone(self, service, client):
        _info, kwargs, _labels = _run(service, client, ipc_host=False)

        assert "ipc_mode" not in kwargs

    def test_auto_remove_off_keeps_the_logs_of_a_crash(self, service, client):
        _info, kwargs, _labels = _run(service, client, auto_remove=False)

        assert kwargs["remove"] is False


class TestRunContainerFailures:
    """A daemon that refuses has to fail legibly, not leak the SDK's types."""

    def test_a_missing_image_names_the_image(self, service, client):
        client.containers.run_error = docker_sdk.errors.ImageNotFound(
            "No such image: ghcr.io/example/engine:1.4.0"
        )

        with pytest.raises(RuntimeError) as caught:
            _run(service, client)

        assert str(caught.value) == f"Image not found: {IMAGE}"

    def test_an_api_error_is_reported_as_one(self, service, client):
        client.containers.run_error = docker_sdk.errors.APIError(
            "409 Conflict: name already in use"
        )

        with pytest.raises(RuntimeError) as caught:
            _run(service, client)

        assert "Docker API error" in str(caught.value)
        assert "name already in use" in str(caught.value)


# ── The client itself ───────────────────────────────────────────────────────


class TestDaemonAbsent:
    """Nothing is imported or connected until something is actually asked."""

    @staticmethod
    def _broken_module(exc: BaseException) -> MagicMock:
        module = MagicMock(name="docker-module")
        module.from_env.side_effect = exc
        return module

    def test_a_service_can_be_built_without_a_daemon(self):
        """Construction must never touch Docker — the CLI builds one at import."""
        module = self._broken_module(OSError("no socket"))

        with patch.dict(sys.modules, {"docker": module}):
            DockerService()

        module.from_env.assert_not_called()

    def test_the_error_says_docker_is_not_running_and_keeps_the_cause(self):
        service = DockerService()
        cause = OSError("Error while fetching server API version")

        with patch.dict(sys.modules, {"docker": self._broken_module(cause)}):
            with pytest.raises(RuntimeError) as caught:
                service.client

        assert "Docker daemon not available" in str(caught.value)
        assert "Error while fetching server API version" in str(caught.value)
        assert caught.value.__cause__ is cause
        assert service._import_error is cause

    def test_a_failed_connection_is_not_cached(self):
        """A daemon that comes back must be usable without a restart."""
        service = DockerService()
        working = MagicMock(name="docker-module")
        connected = MagicMock(name="DockerClient")
        working.from_env.return_value = connected

        with patch.dict(sys.modules, {"docker": self._broken_module(OSError("down"))}):
            with pytest.raises(RuntimeError):
                service.client

        with patch.dict(sys.modules, {"docker": working}):
            assert service.client is connected


# ── Images ──────────────────────────────────────────────────────────────────


class TestImageExists:
    def test_an_empty_reference_is_never_present(self, service, client):
        """No reference means no question to ask the daemon."""
        assert service.image_exists("") is False
        assert client.images.get_calls == []

    def test_a_present_image_is_reported_present(self, service):
        assert service.image_exists(IMAGE) is True

    def test_a_missing_image_is_reported_absent(self, service):
        assert service.image_exists("ghcr.io/example/engine:nope") is False

    def test_a_broken_daemon_reads_as_absent_rather_than_exploding(
        self, service, client, caplog
    ):
        """Presence is a hint on the deploy path; it must never be the failure."""
        client.images.get_error = docker_sdk.errors.APIError("500 Server Error")

        with caplog.at_level(logging.DEBUG, logger=real_docker.__name__):
            assert service.image_exists(IMAGE) is False

        assert "image_exists" in caplog.text


class TestImageInfo:
    def test_a_present_image_is_described(self, service):
        info = service.image_info(IMAGE)

        assert info == {
            "id": f"sha256:{IMAGE}",
            "size_bytes": 26 * GB,
            "created": "2026-02-03T04:05:06Z",
            "repo_tags": [IMAGE],
            "repo_digests": [f"ghcr.io/example/engine@sha256:{IMAGE}"],
        }

    def test_a_missing_image_is_none_not_an_error(self, service):
        assert service.image_info("ghcr.io/example/engine:nope") is None

    def test_a_broken_daemon_is_also_none(self, service, client):
        client.images.get_error = docker_sdk.errors.APIError("500 Server Error")

        assert service.image_info(IMAGE) is None


class TestListImages:
    def test_every_local_image_is_described_the_same_way_as_one(self, service):
        listed = service.list_images()

        assert listed == [service.image_info(IMAGE)]

    def test_a_daemon_that_cannot_list_is_an_error_not_an_empty_disk(
        self, service, client
    ):
        """Returning [] here would read as "nothing cached" and trigger a re-pull."""
        client.images.list_error = docker_sdk.errors.APIError("500 Server Error")

        with pytest.raises(RuntimeError) as caught:
            service.list_images()

        assert "could not list images" in str(caught.value)


class TestRemoveImage:
    def test_a_present_image_is_removed(self, service, client):
        assert service.remove_image(IMAGE) is True
        assert client.images.remove_calls == [(IMAGE, False)]
        assert service.image_exists(IMAGE) is False

    def test_force_is_passed_through(self, service, client):
        service.remove_image(IMAGE, force=True)

        assert client.images.remove_calls == [(IMAGE, True)]

    def test_removing_what_is_not_there_is_false_not_an_error(self, service):
        """Deleting twice is the same as deleting once."""
        assert service.remove_image("ghcr.io/example/engine:nope") is False

    def test_an_image_still_in_use_is_a_named_error(self, service, client):
        client.images.remove_error = docker_sdk.errors.APIError(
            "409 Conflict: image is being used by running container"
        )

        with pytest.raises(RuntimeError) as caught:
            service.remove_image(IMAGE)

        assert f"could not remove image {IMAGE}" in str(caught.value)
        assert "being used by running container" in str(caught.value)


# ── Pull plumbing ───────────────────────────────────────────────────────────


class TestPullEdgeCases:
    def test_a_pull_needs_a_reference(self, service):
        with pytest.raises(RuntimeError) as caught:
            service.pull_image("")

        assert "needs an image reference" in str(caught.value)

    def test_the_reference_is_split_for_the_low_level_api(self, service, client):
        client.api.chunks = [{"status": "Already exists", "id": "a"}]

        service.pull_image(IMAGE, stall_timeout=0)

        assert client.api.calls == [
            {
                "repository": "ghcr.io/example/engine",
                "tag": "1.4.0",
                "stream": True,
                "decode": True,
            }
        ]

    def test_chunks_that_are_not_dicts_are_ignored(self, service, client):
        """An undecoded line must not take the pull down."""
        client.api.chunks = [
            b"{}",
            "Downloading",
            None,
            {"status": "Already exists", "id": "cached"},
        ]

        result = service.pull_image(IMAGE, stall_timeout=0)

        assert result["percent"] == 100.0

    def test_a_fully_cached_image_completes_with_nothing_downloaded(
        self, service, client
    ):
        """ "Already exists" carries no byte counters — 0 of 0 is still done."""
        seen: list[dict] = []
        client.api.chunks = [
            {"status": "Pulling from ghcr.io/example/engine", "id": "1.4.0"},
            {"status": "Already exists", "id": "layer-a"},
            {"status": "Pull complete", "id": "layer-b"},
        ]

        result = service.pull_image(IMAGE, seen.append, interval=0, stall_timeout=0)

        assert (result["bytes_done"], result["bytes_total"]) == (0, 0)
        assert result["percent"] == 100.0
        assert seen[-1]["layers"] == 2
        assert seen[-1]["percent"] == 0.0  # nothing to divide by

    def test_the_summary_names_the_repository_the_tag_and_the_image(
        self, service, client
    ):
        client.api.chunks = [{"status": "Already exists", "id": "a"}]

        result = service.pull_image(IMAGE, stall_timeout=0)

        assert result["ref"] == IMAGE
        assert result["repository"] == "ghcr.io/example/engine"
        assert result["tag"] == "1.4.0"
        assert result["id"] == f"sha256:{IMAGE}"
        assert result["size_bytes"] == 26 * GB

    @pytest.mark.parametrize(
        "chunk, expected",
        [
            (
                {"error": "denied: requested access to the resource is denied"},
                "requested access to the resource is denied",
            ),
            (
                {"errorDetail": {"message": "unauthorized: authentication required"}},
                "unauthorized: authentication required",
            ),
        ],
        ids=["error-string", "errorDetail-dict"],
    )
    def test_an_error_chunk_fails_the_pull_with_the_registry_message(
        self, service, client, chunk, expected
    ):
        """A registry refusal arrives in-band, with HTTP 200 around it."""
        client.api.chunks = [{"status": "Pulling from x", "id": "1"}, chunk]

        with pytest.raises(RuntimeError) as caught:
            service.pull_image(IMAGE, stall_timeout=0)

        assert f"pull of {IMAGE} failed" in str(caught.value)
        assert expected in str(caught.value)

    def test_a_stream_that_breaks_mid_pull_is_reported_against_the_reference(
        self, service, client
    ):
        def _stream():
            yield {"status": "Downloading", "id": "a", "progressDetail": {"total": 2}}
            raise ConnectionResetError("peer closed the connection")

        client.api.chunks = _stream()

        with pytest.raises(RuntimeError) as caught:
            service.pull_image(IMAGE, stall_timeout=5)

        assert f"pull of {IMAGE} failed" in str(caught.value)
        assert "peer closed the connection" in str(caught.value)


class TestWatchedChunks:
    """The watchdog wrapper around a stream docker-py gives no timeout to."""

    class _Stream:
        def __init__(self, chunks, boom: BaseException | None = None):
            self._chunks = chunks
            self._boom = boom
            self.closed = False

        def __iter__(self):
            yield from self._chunks
            if self._boom is not None:
                raise self._boom

        def close(self):
            self.closed = True

    def test_every_chunk_arrives_and_the_socket_is_closed(self):
        stream = self._Stream([1, 2, 3])

        assert list(_watched_chunks(stream, 5)) == [1, 2, 3]
        assert stream.closed is True

    def test_abandoning_the_iteration_still_closes_the_socket(self):
        stream = self._Stream([1, 2, 3])

        chunks = _watched_chunks(stream, 5)
        assert next(chunks) == 1
        chunks.close()

        assert stream.closed is True

    def test_an_exception_on_the_reader_thread_is_replayed_to_the_caller(self):
        """The drain runs on a helper thread; its failure must not be swallowed."""
        boom = ConnectionResetError("peer closed the connection")
        stream = self._Stream([1], boom=boom)

        with pytest.raises(ConnectionResetError) as caught:
            list(_watched_chunks(stream, 5))

        assert caught.value is boom
        assert stream.closed is True

    def test_a_disabled_watchdog_iterates_the_stream_directly(self):
        """Zero opts out — that is what the in-memory fakes want."""
        stream = self._Stream([1, 2])

        assert list(_watched_chunks(stream, 0)) == [1, 2]

    def test_a_stream_with_nothing_to_close_is_fine(self):
        """``close`` is optional on an iterable; not having one is not an error."""
        _close_quietly(object())
        assert list(_watched_chunks(iter([1, 2]), 5)) == [1, 2]


# ── Reading container state back ────────────────────────────────────────────


class TestStopContainer:
    def test_stopping_waits_then_removes(self, service, client):
        _run(service, client, name="live")

        assert service.stop_container("live", timeout=7) is True

        container = client.containers.store["live"]
        assert container.stopped_with == 7
        assert container.removed_force is True

    def test_a_container_that_is_already_gone_is_false(self, service):
        """The SDK's NotFound, whose message does not contain "not found"."""
        assert service.stop_container("never-existed") is False

    def test_a_transport_that_says_not_found_is_also_false(self, service, client):
        """The mock and the docker CLI both report it as plain text."""
        client.containers.get_error = RuntimeError("Container ghost not found")

        assert service.stop_container("ghost") is False

    def test_a_daemon_failure_is_logged_and_reported_as_not_stopped(
        self, service, client, caplog
    ):
        """Teardown must not raise, but it must not lie about succeeding either."""
        client.containers.get_error = docker_sdk.errors.APIError("500 Server Error")

        with caplog.at_level(logging.ERROR, logger=real_docker.__name__):
            assert service.stop_container("wedged") is False

        assert "Failed to stop container wedged" in caplog.text


class TestGetContainerStatus:
    def test_a_running_container_reports_its_state(self, service, client):
        _run(service, client, name="live")

        status = service.get_container_status("live")

        assert status == {
            "status": "running",
            "running": True,
            "id": "id-live",
            "state": {"Status": "running", "Running": True, "Pid": 41},
            "error": None,
        }

    def test_a_stopped_container_is_not_running(self, service, client):
        _run(service, client, name="live")
        client.containers.store["live"].status = "exited"

        status = service.get_container_status("live")

        assert status["status"] == "exited"
        assert status["running"] is False

    def test_a_container_the_daemon_never_heard_of_is_missing(self, service):
        status = service.get_container_status("ghost")

        assert status["status"] == "missing"
        assert status["running"] is False
        assert status["id"] is None
        assert status["error"] == "Container 'ghost' not found"

    def test_a_transport_that_says_not_found_is_also_missing(self, service, client):
        client.containers.get_error = RuntimeError("Container ghost not found")

        assert service.get_container_status("ghost")["status"] == "missing"

    def test_an_api_error_is_reported_as_an_error_not_as_missing(self, service, client):
        """ "Missing" would make the health monitor reap a container that is fine."""
        client.containers.get_error = docker_sdk.errors.APIError("500 Server Error")

        status = service.get_container_status("live")

        assert status["status"] == "error"
        assert status["running"] is False
        assert "500 Server Error" in status["error"]

    def test_an_unrecognised_failure_is_raised_rather_than_guessed_at(
        self, service, client
    ):
        client.containers.get_error = ValueError("something else entirely")

        with pytest.raises(ValueError):
            service.get_container_status("live")


class TestExecInContainer:
    def test_stdout_and_stderr_are_demuxed_and_decoded(self, service, client):
        _run(service, client, name="live")

        result = service.exec_in_container("live", ["echo", "hello"])

        assert isinstance(result, ExecResult)
        assert (result.returncode, result.stdout, result.stderr) == (
            0,
            "hello\n",
            "warn\n",
        )
        assert result.ok is True
        assert client.containers.store["live"].exec_calls == [
            {"command": ["echo", "hello"], "demux": True, "detach": False}
        ]

    def test_a_failing_command_keeps_its_exit_code(self, service, client):
        _run(service, client, name="live")
        client.containers.store["live"].exec_result = _FakeExec(
            exit_code=127, output=(None, b"not found\n")
        )

        result = service.exec_in_container("live", "nope")

        assert result.returncode == 127
        assert result.ok is False
        assert result.stdout == ""
        assert result.stderr == "not found\n"

    def test_an_undemuxed_string_output_is_all_stdout(self, service, client):
        """Not every transport splits the streams; text is taken as-is."""
        _run(service, client, name="live")
        client.containers.store["live"].exec_result = _FakeExec(output="plain text\n")

        result = service.exec_in_container("live", "echo")

        assert result.stdout == "plain text\n"
        assert result.stderr == ""

    def test_a_detached_exec_returns_immediately(self, service, client):
        """There is no exit code to wait for; success means "it was started"."""
        _run(service, client, name="live")
        client.containers.store["live"].exec_result = _FakeExec(exit_code=99)

        result = service.exec_in_container("live", "serve.sh", detach=True)

        assert result == ExecResult(returncode=0, stdout="", stderr="")
        assert client.containers.store["live"].exec_calls[0]["detach"] is True

    def test_a_container_object_is_used_as_given(self, service, client):
        """The native runtime already holds the container it just started."""
        _run(service, client, name="live")
        container = client.containers.store["live"]
        client.containers.get_error = AssertionError("should not be looked up")

        assert service.exec_in_container(container, "true").ok


class TestGetLogs:
    def test_the_tail_is_decoded(self, service, client):
        _run(service, client, name="live")
        client.containers.store["live"].log_bytes = b"line one\nline two\n"

        assert service.get_logs("live", tail=50) == "line one\nline two\n"
        assert client.containers.store["live"].log_tails == [50]

    def test_a_missing_container_is_a_message_not_an_exception(self, service):
        """The logs endpoint has nothing better to show a user than this."""
        assert service.get_logs("ghost") == "Container 'ghost' not found"

    def test_a_transport_that_says_not_found_gets_the_same_message(
        self, service, client
    ):
        client.containers.get_error = RuntimeError("Container ghost not found")

        assert service.get_logs("ghost") == "Container 'ghost' not found"

    def test_a_daemon_failure_is_not_disguised_as_empty_logs(self, service, client):
        client.containers.get_error = docker_sdk.errors.APIError("500 Server Error")

        with pytest.raises(docker_sdk.errors.APIError):
            service.get_logs("live")


class TestCopyToContainer:
    """``docker cp`` is a subprocess — the SDK's put_archive wants a tar stream."""

    def test_a_successful_copy_runs_the_right_command(self, service):
        with patch.object(real_docker.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")

            assert service.copy_to_container("live", "/tmp/mod.py", "/opt/mod.py")

        assert run.call_args.args[0] == [
            "docker",
            "cp",
            "/tmp/mod.py",
            "live:/opt/mod.py",
        ]
        assert run.call_args.kwargs["timeout"] == 120
        assert run.call_args.kwargs["capture_output"] is True

    def test_a_failed_copy_is_false_and_logs_what_docker_said(self, service, caplog):
        with patch.object(real_docker.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 1, "", "no such file or directory\n"
            )

            with caplog.at_level(logging.ERROR, logger=real_docker.__name__):
                assert service.copy_to_container("live", "/tmp/x", "/opt/x") is False

        assert "no such file or directory" in caplog.text

    def test_a_copy_that_hangs_is_abandoned_not_waited_on_forever(
        self, service, caplog
    ):
        with patch.object(real_docker.subprocess, "run") as run:
            run.side_effect = subprocess.TimeoutExpired(["docker", "cp"], 5)

            with caplog.at_level(logging.ERROR, logger=real_docker.__name__):
                assert (
                    service.copy_to_container("live", "/tmp/x", "/opt/x", timeout=5)
                    is False
                )

        assert "timed out after 5s" in caplog.text


# ── Label-based discovery ───────────────────────────────────────────────────


class TestManagedListing:
    def test_only_managed_containers_are_asked_for(self, service, client):
        _run(service, client, name="ours")
        client.containers.run(IMAGE, name="theirs", labels={"other": "1"})

        listed = service.list_managed_containers()

        assert [c.name for c in listed] == ["ours"]

    def test_the_filter_is_re_checked_against_the_labels_that_came_back(
        self, service, client
    ):
        """The daemon's filter is an optimisation, not the decision.

        A transport that ignores ``filters`` — the docker CLI's ``--filter``
        is easy to get wrong over SSH — would otherwise return every managed
        container as a match for a cluster nobody asked about.
        """
        _run(service, client, name="head", metadata=_metadata("a", cluster="c1"))
        _run(service, client, name="solo", metadata=_metadata("b"))
        client.containers.honour_filters = False

        assert [c.name for c in service.list_managed_containers()] == ["head", "solo"]
        assert [
            c.name
            for c in service.list_managed_containers({"spark-pulse.cluster": "c1"})
        ] == ["head"]
        assert [
            c.name
            for c in service.list_managed_containers({"spark-pulse.cluster": "c2"})
        ] == []
        # An empty value means "carries this label at all".
        assert [
            c.name for c in service.list_managed_containers({"spark-pulse.cluster": ""})
        ] == ["head"]

    def test_a_deployment_is_found_by_its_label(self, service, client):
        _run(service, client, name="rank-0", metadata=_metadata("dep-a"))
        _run(service, client, name="other", metadata=_metadata("dep-b"))

        found = service.get_container_by_deployment("dep-a")

        assert found is not None
        assert found.name == "rank-0"
        assert service.get_container_by_deployment("dep-missing") is None

    def test_every_container_of_a_recipe_is_found(self, service, client):
        _run(service, client, name="r0", metadata=_metadata("d0", recipe="qwen3"))
        _run(service, client, name="r1", metadata=_metadata("d1", recipe="qwen3"))
        _run(service, client, name="other", metadata=_metadata("d2", recipe="llama"))

        found = service.get_container_by_recipe("qwen3")

        assert sorted(c.name for c in found) == ["r0", "r1"]
        assert service.get_container_by_recipe("nothing") == []


class TestContainerToInfo:
    """The SDK hands back an image object; the CLI and the mock hand back a string."""

    def _info(self, service, client, image: Any) -> ContainerInfo:
        container = client.containers.run(
            IMAGE, name="x", labels={MANAGED_LABEL: "true"}
        )
        container.image = image
        return service.list_managed_containers()[0]

    def test_a_tagged_image_object_reads_as_its_first_tag(self, service, client):
        image = MagicMock(spec=["tags", "id"])
        image.tags = [IMAGE, "engine:stale"]
        image.id = "sha256:deadbeef"

        assert self._info(service, client, image).image == IMAGE

    def test_an_untagged_image_object_falls_back_to_its_id(self, service, client):
        """A digest-pinned pull leaves no tags at all."""
        image = MagicMock(spec=["tags", "id"])
        image.tags = []
        image.id = "sha256:deadbeef"

        assert self._info(service, client, image).image == "sha256:deadbeef"

    def test_a_string_image_is_used_as_is(self, service, client):
        assert self._info(service, client, IMAGE).image == IMAGE


# ── Small pure helpers ──────────────────────────────────────────────────────


class TestHelpers:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            (None, ""),
            (b"bytes\n", "bytes\n"),
            (b"\xff invalid", "� invalid"),
            ("text", "text"),
            (17, "17"),
        ],
    )
    def test_decode(self, raw, expected):
        assert _decode(raw) == expected

    @pytest.mark.parametrize(
        "labels, wanted, expected",
        [
            ({"a": "1"}, None, True),
            ({"a": "1"}, {}, True),
            ({"a": "1"}, {"a": "1"}, True),
            ({"a": "1"}, {"a": "2"}, False),
            ({"a": "1"}, {"b": "1"}, False),
            ({"a": "1"}, {"a": ""}, True),
            ({"a": ""}, {"a": ""}, True),
            ({"a": "1"}, {"b": ""}, False),
            ({"a": "1", "b": "2"}, {"a": "1", "b": "2"}, True),
            ({"a": "1", "b": "2"}, {"a": "1", "b": "3"}, False),
        ],
    )
    def test_labels_match(self, labels, wanted, expected):
        assert _labels_match(labels, wanted) is expected

    @pytest.mark.parametrize("ref", ["", "   ", None])
    def test_split_ref_of_nothing_is_nothing(self, ref):
        """A blank reference must not become the repository ``""`` at ``latest``."""
        assert split_ref(ref) == ("", "")

    def test_memory_swap_is_none_when_memory_is_unbounded(self):
        assert DockerService._calc_memory_swap(None) is None

    def test_memory_swap_leaves_ten_gb_of_headroom(self):
        assert DockerService._calc_memory_swap(110) == 120 * GB

    def test_gb_to_bytes(self):
        assert DockerService._gb_to_bytes(1.5) == int(1.5 * GB)


# ── The module-level wrappers the simulation switch replaces ────────────────


class _DelegationSpy:
    """Stands in for the module singleton and records every delegated call."""

    def __init__(self, result: Any = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.result = result

    def __getattr__(self, name: str):
        def _call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            return self.result

        return _call


def _progress(_snapshot: dict) -> None:
    """A progress callback identity the delegation test can compare against."""


class TestModuleWrappers:
    """``routers`` and ``tools.images`` reach Docker through these, not the class.

    ``spark_pulse.tools.__init__`` swaps the whole *module* for the mock, so a
    wrapper that forgets an argument silently changes behaviour between
    simulation and production rather than failing anywhere.
    """

    @pytest.mark.parametrize(
        "call, expected",
        [
            (
                lambda m, sentinel: m.run_container(
                    image=IMAGE, name="c", env_vars={}, metadata=None
                ),
                (
                    "run_container",
                    (),
                    {"image": IMAGE, "name": "c", "env_vars": {}, "metadata": None},
                ),
            ),
            (lambda m, s: m.stop_container("c"), ("stop_container", ("c",), {})),
            (
                lambda m, s: m.get_container_status("c"),
                ("get_container_status", ("c",), {}),
            ),
            (
                lambda m, s: m.list_managed_containers(),
                ("list_managed_containers", (None,), {}),
            ),
            (
                lambda m, s: m.list_managed_containers({"spark-pulse.cluster": "c1"}),
                ("list_managed_containers", ({"spark-pulse.cluster": "c1"},), {}),
            ),
            (
                lambda m, s: m.get_container_by_deployment("dep"),
                ("get_container_by_deployment", ("dep",), {}),
            ),
            (lambda m, s: m.image_exists(IMAGE), ("image_exists", (IMAGE,), {})),
            (lambda m, s: m.pull_image(IMAGE), ("pull_image", (IMAGE, None), {})),
            (
                lambda m, s: m.pull_image(IMAGE, _progress),
                ("pull_image", (IMAGE, _progress), {}),
            ),
            (lambda m, s: m.image_info(IMAGE), ("image_info", (IMAGE,), {})),
            (lambda m, s: m.list_images(), ("list_images", (), {})),
            (
                lambda m, s: m.remove_image(IMAGE),
                ("remove_image", (IMAGE,), {"force": False}),
            ),
            (
                lambda m, s: m.remove_image(IMAGE, force=True),
                ("remove_image", (IMAGE,), {"force": True}),
            ),
        ],
        ids=[
            "run_container",
            "stop_container",
            "get_container_status",
            "list_managed_containers",
            "list_managed_containers-filtered",
            "get_container_by_deployment",
            "image_exists",
            "pull_image",
            "pull_image-with-progress",
            "image_info",
            "list_images",
            "remove_image",
            "remove_image-forced",
        ],
    )
    def test_each_wrapper_delegates_to_the_singleton(self, monkeypatch, call, expected):
        sentinel = object()
        spy = _DelegationSpy(result=sentinel)
        monkeypatch.setattr(real_docker, "_service", spy)

        assert call(real_docker, sentinel) is sentinel
        assert spy.calls == [expected]

    def test_the_service_is_built_once_and_reused(self, monkeypatch):
        """One connection pool for the process, created on first use."""
        built: list[Any] = []

        class _Counted(real_docker.DockerService):
            def __init__(self, client: Any | None = None):
                built.append(self)
                super().__init__(client=client)

        monkeypatch.setattr(real_docker, "_service", None)
        monkeypatch.setattr(real_docker, "DockerService", _Counted)

        first = real_docker._get_service()
        second = real_docker._get_service()

        assert first is second
        assert len(built) == 1
        # No injected client: each thread gets its own from the environment.
        assert first._injected_client is None

    def test_the_wrappers_share_that_one_service(self, monkeypatch):
        """Three different wrappers, one client, end to end."""
        built: list[Any] = []

        class _Counted(real_docker.DockerService):
            def __init__(self, client: Any | None = None):
                built.append(self)
                super().__init__(client=_FakeClient())

        monkeypatch.setattr(real_docker, "_service", None)
        monkeypatch.setattr(real_docker, "DockerService", _Counted)

        assert real_docker.image_exists(IMAGE) is True
        assert real_docker.list_images()[0]["id"] == f"sha256:{IMAGE}"
        assert real_docker.get_container_status("ghost")["status"] == "missing"

        assert len(built) == 1
