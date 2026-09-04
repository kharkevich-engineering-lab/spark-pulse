"""Contract tests: the three container services must agree.

Spark Pulse has three implementations of the same container service:

* ``DockerService`` — local node, Docker SDK.
* ``RemoteDockerService`` — remote node, docker CLI over SSH.
* ``MockDockerService`` — simulation mode, in-memory.

They are used interchangeably (the cluster orchestrator does not know which
one it holds), so the same scenario is run against all three here: start a
container carrying ``spark-pulse.*`` labels, list it back, exec in it, stop
it, and reconcile it from its labels. Each is driven through a fake at its
own boundary — a mocked docker SDK client and a mocked SSH client — so the
production code paths, not stand-ins for them, are what run.
"""

from __future__ import annotations

import json
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from spark_pulse.config import config
from spark_pulse.mock.docker import MockDockerService, MockDockerClient
from spark_pulse.tools.docker import (
    ContainerInfo,
    ContainerMetadata,
    DockerService,
    ExecResult,
    PullCancelled,
    PullStalled,
    split_ref,
)
from spark_pulse.tools.labels import (
    CLUSTER_LABEL,
    DEPLOYMENT_LABEL,
    MANAGED_LABEL,
    RECIPE_LABEL,
    ROLE_LABEL,
)
from spark_pulse.tools.reconciliation import (
    _reconcile_clusters_real,
    _reconcile_deployments_real,
)
from spark_pulse.tools.remote_docker import RemoteDockerService
from spark_pulse.tools.ssh import SSHResult

IMAGE = "ghcr.io/example/engine:latest"
IMAGE_SIZE = 26_843_545_600
MISSING_IMAGE = "ghcr.io/example/engine:not-pulled"
IDLE_COMMAND = ["sleep", "infinity"]

# Layer plan the fake SDK client streams back for a pull.
FAKE_LAYERS = [("layer-a", 4_000), ("layer-b", 6_000)]


def _metadata(deployment: str, **overrides) -> ContainerMetadata:
    """Metadata for a solo deployment, overridable for cluster cases."""
    fields = {
        "deployment": deployment,
        "recipe": "qwen3-contract",
        "image": IMAGE,
        "mode": "solo",
    }
    fields.update(overrides)
    return ContainerMetadata(**fields)


# ── Fake docker SDK client (for the real DockerService) ─────────────────────


def _fake_sdk_client() -> MagicMock:
    """A MagicMock shaped like ``docker.DockerClient`` over a dict of containers."""
    client = MagicMock(name="DockerClient")
    store: dict[str, MagicMock] = {}

    class _NotFound(Exception):
        pass

    def _make_container(image, name, labels, **_kwargs):
        container = MagicMock(name=f"Container({name})")
        container.id = f"id-{name}"
        container.name = name
        container.status = "running"
        container.image = image
        container.labels = dict(labels or {})
        container.attrs = {"State": {"Status": "running", "Running": True}}
        exec_result = MagicMock()
        exec_result.exit_code = 0
        exec_result.output = (b"contract-ok\n", None)
        container.exec_run.return_value = exec_result
        container.remove.side_effect = lambda force=False: store.pop(name, None)
        store[name] = container
        return container

    def _get(name):
        if name not in store:
            raise _NotFound(f"Container {name} not found")
        return store[name]

    def _list(all=False, filters=None):  # noqa: A002 — mirrors the SDK kwarg
        containers = list(store.values())
        if not all:
            containers = [c for c in containers if c.status == "running"]
        # Docker takes the label filter as a list: "key" matches presence,
        # "key=value" matches a value.
        raw = (filters or {}).get("label")
        for term in [raw] if isinstance(raw, str) else list(raw or []):
            if "=" in term:
                key, _, value = term.partition("=")
                containers = [c for c in containers if c.labels.get(key) == value]
            else:
                containers = [c for c in containers if term in c.labels]
        return containers

    def _remove(name):
        store.pop(name, None)

    images: dict[str, MagicMock] = {}

    def _make_image(ref: str, size: int = 3) -> MagicMock:
        image = MagicMock(name=f"Image({ref})")
        image.id = f"sha256:{abs(hash(ref)):064x}"[:71]
        image.attrs = {
            "Id": image.id,
            "Size": size,
            "Created": "2026-01-01T00:00:00Z",
            "RepoTags": [ref],
            "RepoDigests": [f"{ref.split(':')[0]}@{image.id}"],
        }
        images[ref] = image
        return image

    _make_image(IMAGE, size=IMAGE_SIZE)

    def _image_get(ref):
        if ref not in images:
            raise _NotFound(f"No such image: {ref}")
        return images[ref]

    def _pull(repository, tag="latest", stream=True, decode=True, **_kwargs):
        ref = f"{repository}:{tag}"

        def _stream():
            for layer_id, size in FAKE_LAYERS:
                for step in (1, 2):
                    yield {
                        "status": "Downloading",
                        "id": layer_id,
                        "progressDetail": {
                            "current": size // 2 * step,
                            "total": size,
                        },
                    }
                yield {
                    "status": "Pull complete",
                    "id": layer_id,
                    "progressDetail": {"current": size, "total": size},
                }
            _make_image(ref, size=sum(s for _, s in FAKE_LAYERS))

        return _stream()

    client.containers.run.side_effect = _make_container
    client.containers.get.side_effect = _get
    client.containers.list.side_effect = _list
    client.images.get.side_effect = _image_get
    client.images.list.side_effect = lambda **_kw: list(images.values())
    client.api.pull.side_effect = _pull
    client._remove = _remove
    return client


# ── Fake SSH client (for RemoteDockerService) ───────────────────────────────


def _fake_ssh_client() -> MagicMock:
    """A MagicMock SSHClient that answers like the docker CLI on a remote node."""
    ssh = MagicMock(name="SSHClient")
    store: dict[str, dict] = {}
    remote_images: dict[str, dict] = {
        IMAGE: {"Id": "sha256:remote-engine", "Size": IMAGE_SIZE}
    }

    def _exec(host, command, timeout=30, **_kwargs):
        if command.startswith("docker run"):
            name = _flag_value(command, "--name")
            labels = {}
            parts = command.split()
            for i, part in enumerate(parts):
                if part == "--label":
                    key, _, value = parts[i + 1].strip("'").partition("=")
                    labels[key] = value
            store[name] = {
                "ID": f"id-{name}",
                "Names": name,
                "Image": parts[-1],
                "State": "running",
                "Labels": ",".join(f"{k}={v}" for k, v in labels.items()),
            }
            return SSHResult(returncode=0, stdout=f"id-{name}\n", stderr="")
        if command.startswith("docker ps"):
            lines = "\n".join(json.dumps(c) for c in store.values())
            return SSHResult(returncode=0, stdout=lines, stderr="")
        if command.startswith("docker image inspect"):
            ref = command.split()[3]
            if ref not in remote_images:
                return SSHResult(returncode=1, stdout="", stderr="No such image")
            field = "Id" if ".Id" in command else "Size"
            value = remote_images[ref][field]
            return SSHResult(returncode=0, stdout=f"{value}\n", stderr="")
        if command.startswith("docker pull"):
            ref = command.split()[2]
            remote_images[ref] = {"Id": f"sha256:{ref}", "Size": IMAGE_SIZE}
            return SSHResult(returncode=0, stdout=f"Downloaded {ref}\n", stderr="")
        if command.startswith("docker exec"):
            return SSHResult(returncode=0, stdout="contract-ok\n", stderr="")
        if command.startswith("docker stop"):
            name = command.split()[-1]
            if name not in store:
                return SSHResult(returncode=1, stdout="", stderr="No such container")
            del store[name]
            return SSHResult(returncode=0, stdout=name, stderr="")
        return SSHResult(returncode=1, stdout="", stderr=f"unexpected: {command}")

    ssh.exec.side_effect = _exec
    return ssh


def _flag_value(command: str, flag: str) -> str:
    parts = command.split()
    return parts[parts.index(flag) + 1]


# ── Uniform adapters over the three services ────────────────────────────────


class _LocalAdapter:
    """Drives a service with DockerService's local signatures."""

    def __init__(self, service):
        self.service = service

    def run(self, name: str, metadata: ContainerMetadata) -> ContainerInfo:
        return self.service.run_container(
            image=IMAGE,
            name=name,
            env_vars={"SPARK_PULSE_CONTRACT": "1"},
            metadata=metadata,
        )

    def list_managed(self, labels=None) -> list[ContainerInfo]:
        return self.service.list_managed_containers(labels)

    def exec(self, name: str, argv: list[str]) -> ExecResult:
        return self.service.exec_in_container(name, argv)

    def stop(self, name: str) -> bool:
        return self.service.stop_container(name)

    def image_exists(self, ref: str) -> bool:
        return self.service.image_exists(ref)

    def pull_image(self, ref: str, progress=None):
        return self.service.pull_image(ref, progress)

    def reconcile_deployments(self) -> list[dict]:
        return _reconcile_deployments_real(self.service)


class _RemoteAdapter:
    """Drives RemoteDockerService against a remote host."""

    HOST = "10.0.0.2"

    def __init__(self, service: RemoteDockerService):
        self.service = service

    def run(self, name: str, metadata: ContainerMetadata) -> ContainerInfo:
        return self.service.run_container(
            self.HOST,
            IMAGE,
            name,
            {"SPARK_PULSE_CONTRACT": "1"},
            {"privileged": True, "shm_size_gb": 64},
            metadata,
        )

    def list_managed(self, labels=None) -> list[ContainerInfo]:
        return self.service.list_managed_containers(self.HOST, labels)

    def exec(self, name: str, argv: list[str]) -> ExecResult:
        return self.service.exec_container(self.HOST, name, argv)

    def stop(self, name: str) -> bool:
        return self.service.stop_container(self.HOST, name)

    def image_exists(self, ref: str) -> bool:
        return self.service.image_exists(self.HOST, ref)

    def pull_image(self, ref: str, progress=None):
        return self.service.pull_image(self.HOST, ref, progress)

    def reconcile_deployments(self) -> list[dict]:
        raise NotImplementedError


@pytest.fixture(
    params=["mock", "docker-sdk", "remote-ssh"],
    ids=["mock", "docker-sdk", "remote-ssh"],
)
def service(request):
    """Yield each container service behind the same adapter interface."""
    if request.param == "mock":
        client = MockDockerClient()
        client.images.add(IMAGE, size=IMAGE_SIZE)
        return _LocalAdapter(MockDockerService(client))
    if request.param == "docker-sdk":
        return _LocalAdapter(DockerService(client=_fake_sdk_client()))
    return _RemoteAdapter(
        RemoteDockerService(
            ssh_client=_fake_ssh_client(),
            docker_service=DockerService(client=_fake_sdk_client()),
        )
    )


# ── The contract ────────────────────────────────────────────────────────────


class TestContainerServiceContract:
    """The same scenario, run against every container service."""

    def test_run_returns_container_info_with_labels(self, service):
        """A started container is described by ContainerInfo and spark-pulse labels."""
        info = service.run("contract-run", _metadata("contract-run"))

        assert isinstance(info, ContainerInfo)
        assert info.name == "contract-run"
        assert info.id
        assert info.status == "running"
        assert info.image == IMAGE
        assert info.labels[MANAGED_LABEL] == "true"
        assert info.labels[DEPLOYMENT_LABEL] == "contract-run"
        assert info.labels[RECIPE_LABEL] == "qwen3-contract"
        assert info.metadata.deployment == "contract-run"

    def test_list_managed_finds_the_container(self, service):
        """A started container comes back from list_managed_containers."""
        service.run("contract-list", _metadata("contract-list"))

        containers = service.list_managed()
        names = [c.name for c in containers]
        assert "contract-list" in names

        found = next(c for c in containers if c.name == "contract-list")
        assert found.metadata.deployment == "contract-list"
        assert found.metadata.recipe == "qwen3-contract"
        assert found.labels[MANAGED_LABEL] == "true"

    def test_list_managed_filters_by_label(self, service):
        """Label filters select the right containers, and an empty value means any."""
        service.run(
            "contract-head",
            _metadata("contract-cluster", cluster="contract-cluster", role="head"),
        )
        service.run("contract-solo", _metadata("contract-solo"))

        in_cluster = service.list_managed({CLUSTER_LABEL: "contract-cluster"})
        assert [c.name for c in in_cluster] == ["contract-head"]
        assert in_cluster[0].metadata.role == "head"

        any_cluster = service.list_managed({CLUSTER_LABEL: ""})
        assert [c.name for c in any_cluster] == ["contract-head"]

        heads = service.list_managed({ROLE_LABEL: "head"})
        assert [c.name for c in heads] == ["contract-head"]

    def test_exec_returns_exec_result(self, service):
        """Exec returns an ok/stdout/stderr result, never a bare string."""
        service.run("contract-exec", _metadata("contract-exec"))

        result = service.exec("contract-exec", ["echo", "contract-ok"])

        assert isinstance(result, ExecResult)
        assert result.ok
        assert result.returncode == 0
        assert "contract-ok" in result.stdout

    def test_stop_removes_the_container(self, service):
        """Stopping a container removes it from the managed listing."""
        service.run("contract-stop", _metadata("contract-stop"))
        assert service.stop("contract-stop") is True

        names = [c.name for c in service.list_managed()]
        assert "contract-stop" not in names

    def test_idle_container_lifecycle(self, service):
        """The full run -> list -> exec -> stop sequence used by the deploy path."""
        info = service.run("contract-idle", _metadata("contract-idle"))
        assert info.status == "running"

        assert "contract-idle" in [c.name for c in service.list_managed()]
        assert service.exec("contract-idle", IDLE_COMMAND).ok
        assert service.stop("contract-idle") is True
        assert "contract-idle" not in [c.name for c in service.list_managed()]


class TestImageContract:
    """Every container service answers the same questions about images."""

    def test_image_exists_is_true_for_a_present_image(self, service):
        assert service.image_exists(IMAGE) is True

    def test_image_exists_is_false_for_a_missing_image(self, service):
        assert service.image_exists(MISSING_IMAGE) is False

    def test_pull_makes_the_image_present(self, service):
        """A pull returns a completed summary and the image is then local."""
        result = service.pull_image(MISSING_IMAGE)

        assert result["ref"] == MISSING_IMAGE
        assert result["percent"] == 100.0
        assert result["bytes_total"] >= 0
        assert service.image_exists(MISSING_IMAGE) is True

    def test_pull_reports_progress(self, service):
        """Progress snapshots are aggregated over layers, never per-chunk."""
        seen: list[dict] = []
        service.pull_image(MISSING_IMAGE, seen.append)

        assert seen, "the pull reported no progress at all"
        for snapshot in seen:
            assert snapshot["ref"] == MISSING_IMAGE
            assert 0 <= snapshot["percent"] <= 100
            assert snapshot["bytes_done"] <= snapshot["bytes_total"]
        assert seen[-1]["percent"] == 100.0


class TestPullAggregation:
    """The layer folding and throttling that keeps pull events readable."""

    def test_progress_is_throttled_to_one_event_per_interval(self):
        """Many layer chunks collapse into one snapshot per interval."""
        docker = DockerService(client=_fake_sdk_client())
        seen: list[dict] = []

        # A one-hour interval means only the forced final snapshot survives.
        docker.pull_image(MISSING_IMAGE, seen.append, interval=3600)

        assert len(seen) == 1
        assert seen[0]["percent"] == 100.0

    def test_progress_aggregates_every_layer(self):
        """The reported total is the sum of the layers, not the last one."""
        docker = DockerService(client=_fake_sdk_client())
        seen: list[dict] = []

        docker.pull_image(MISSING_IMAGE, seen.append, interval=0)

        total = sum(size for _, size in FAKE_LAYERS)
        assert seen[-1]["bytes_total"] == total
        assert seen[-1]["layers"] == len(FAKE_LAYERS)
        assert seen[-1]["bytes_done"] == total
        assert seen[-1]["percent"] == 100.0

    @pytest.mark.parametrize(
        "ref,expected",
        [
            ("ghcr.io/org/img:1.2.3", ("ghcr.io/org/img", "1.2.3")),
            ("ghcr.io/org/img", ("ghcr.io/org/img", "latest")),
            ("registry:5000/org/img", ("registry:5000/org/img", "latest")),
            ("registry:5000/org/img:v2", ("registry:5000/org/img", "v2")),
            ("ghcr.io/org/img@sha256:abc", ("ghcr.io/org/img", "sha256:abc")),
        ],
    )
    def test_split_ref(self, ref, expected):
        """References split into (repository, tag-or-digest) for api.pull."""
        assert split_ref(ref) == expected


class TestReconciliationContract:
    """Reconciliation rebuilds state from the labels the services actually write."""

    # The public reconcile_* functions short-circuit to a no-op under
    # SIMULATION_MODE, which pytest forces on; the _real variants are what runs
    # in production, so those are what is exercised here.

    def test_reconcile_finds_a_solo_deployment(self):
        """A container started by DockerService is reconciled from its labels."""
        docker = DockerService(client=_fake_sdk_client())
        docker.run_container(
            image=IMAGE,
            name="contract-reconcile",
            env_vars={},
            metadata=_metadata("contract-reconcile"),
        )

        deployments = _reconcile_deployments_real(docker)

        assert len(deployments) == 1
        assert deployments[0]["id"] == "contract-reconcile"
        assert deployments[0]["container_name"] == "contract-reconcile"
        assert deployments[0]["image"] == IMAGE
        assert deployments[0]["status"] == "running"

    def test_reconcile_finds_a_mock_deployment(self):
        """The same holds for the simulation-mode service."""
        docker = MockDockerService(MockDockerClient())
        docker.run_container(
            image=IMAGE,
            name="contract-reconcile-mock",
            env_vars={},
            metadata=_metadata("contract-reconcile-mock"),
        )

        deployments = _reconcile_deployments_real(docker)

        assert [d["id"] for d in deployments] == ["contract-reconcile-mock"]

    def test_reconcile_finds_a_cluster(self):
        """A cluster container started over SSH is reconciled from its labels."""
        remote = RemoteDockerService(
            ssh_client=_fake_ssh_client(),
            docker_service=DockerService(client=_fake_sdk_client()),
        )
        remote.run_container(
            _RemoteAdapter.HOST,
            IMAGE,
            "contract-cluster-head",
            {},
            {},
            _metadata(
                "contract-cluster",
                mode="cluster",
                cluster="contract-cluster",
                role="head",
                head_ip="10.0.0.2",
                ray_enabled=True,
            ),
        )

        # Reconciliation asks the local node; point it at the same fake host.
        remote_view = _PinnedHost(remote, _RemoteAdapter.HOST)
        clusters = _reconcile_clusters_real(remote_view)

        assert len(clusters) == 1
        assert clusters[0]["name"] == "contract-cluster"
        assert clusters[0]["head_ip"] == "10.0.0.2"
        assert clusters[0]["ray_enabled"] is True
        assert clusters[0]["image"] == IMAGE

    def test_reconcile_ignores_unlabelled_containers(self):
        """Containers without our labels are not adopted."""
        client = _fake_sdk_client()
        client.containers.run(IMAGE, name="someone-elses", labels={"other": "1"})

        assert _reconcile_deployments_real(DockerService(client=client)) == []


class _PinnedHost:
    """Adapts a RemoteDockerService so ``host=""`` reaches a fixed remote node."""

    def __init__(self, service: RemoteDockerService, host: str):
        self._service = service
        self._host = host

    def list_managed_containers(self, host, labels=None):
        return self._service.list_managed_containers(host or self._host, labels)


# ── Client hygiene ──────────────────────────────────────────────────────────


class TestClientPerThread:
    """docker-py's client is not thread-safe, so each thread needs its own.

    Upstream says so plainly, and the reason is that a ``DockerClient`` is a
    ``requests.Session`` with a connection pool behind it. One module-global
    service is fine; one *client* shared by the health monitor, the pull
    threads, the readiness watcher, the fan-outs and forty request threads is
    not.
    """

    @staticmethod
    def _client_module() -> MagicMock:
        """A stand-in for the ``docker`` package handing out fresh clients."""
        module = MagicMock(name="docker-module")
        module.from_env.side_effect = lambda: MagicMock(name="DockerClient")
        return module

    @staticmethod
    def _collect(service: DockerService, count: int) -> list:
        """Read ``service.client`` on ``count`` separate threads."""
        seen: list = [None] * count
        errors: list = []

        def _grab(index: int) -> None:
            try:
                seen[index] = service.client
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [
            threading.Thread(target=_grab, args=(i,), name=f"grab-{i}")
            for i in range(count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert not errors, errors
        return seen

    def test_two_threads_get_different_clients(self):
        """Nothing is shared between threads when the client is ours to make."""
        service = DockerService()

        with patch.dict(sys.modules, {"docker": self._client_module()}):
            first, second = self._collect(service, 2)

        assert first is not None and second is not None
        assert first is not second

    def test_one_thread_keeps_its_own_client(self):
        """Per-thread, not per-call: the connection pool must survive."""
        service = DockerService()

        with patch.dict(sys.modules, {"docker": self._client_module()}):
            assert service.client is service.client

    def test_an_injected_client_is_honoured_on_every_thread(self):
        """A fake handed in is the whole object under test — never bypassed."""
        injected = _fake_sdk_client()
        service = DockerService(client=injected)

        seen = self._collect(service, 4)

        assert seen == [injected] * 4
        assert service.client is injected

    def test_an_injected_client_never_reaches_from_env(self):
        """An injected client short-circuits creation entirely."""
        module = self._client_module()
        service = DockerService(client=_fake_sdk_client())

        with patch.dict(sys.modules, {"docker": module}):
            self._collect(service, 2)

        module.from_env.assert_not_called()


class TestPullWatchdog:
    """A pull with no timeout is a thread held until the process dies."""

    @staticmethod
    def _stalling_client(release: threading.Event) -> MagicMock:
        """A client whose pull emits one chunk and then goes quiet."""
        client = _fake_sdk_client()

        def _pull(repository, tag="latest", **_kwargs):
            def _stream():
                yield {
                    "status": "Downloading",
                    "id": "layer-a",
                    "progressDetail": {"current": 1, "total": 1_000_000},
                }
                # Nothing more arrives until the test lets go, which is well
                # past when the watchdog should have given up.
                release.wait(20)

            return _stream()

        client.api.pull.side_effect = _pull
        return client

    def test_a_stalled_pull_trips_the_watchdog(self):
        """Silence past the timeout fails the pull instead of hanging."""
        release = threading.Event()
        docker = DockerService(client=self._stalling_client(release))
        try:
            with pytest.raises(PullStalled) as caught:
                docker.pull_image(MISSING_IMAGE, stall_timeout=0.2)
        finally:
            release.set()

        message = str(caught.value)
        assert MISSING_IMAGE in message
        assert "stalled" in message
        assert "no pull progress for 0.2s" in message

    def test_the_watchdog_default_comes_from_config(self):
        """Callers that pass nothing get the configured stall timeout."""
        release = threading.Event()
        docker = DockerService(client=self._stalling_client(release))
        try:
            with patch.object(
                type(config),
                "docker_pull_stall_timeout_seconds",
                property(lambda self: 1),
            ):
                with pytest.raises(PullStalled) as caught:
                    docker.pull_image(MISSING_IMAGE)
        finally:
            release.set()

        assert "no pull progress for 1s" in str(caught.value)

    def test_a_healthy_pull_is_untouched_by_the_watchdog(self):
        """Chunks that keep arriving are never mistaken for a stall."""
        docker = DockerService(client=_fake_sdk_client())

        result = docker.pull_image(MISSING_IMAGE, stall_timeout=5)

        assert result["percent"] == 100.0
        assert docker.image_exists(MISSING_IMAGE)

    def test_a_disabled_watchdog_still_pulls(self):
        """Zero opts out, which is what the in-memory fakes want."""
        docker = DockerService(client=_fake_sdk_client())

        assert docker.pull_image(MISSING_IMAGE, stall_timeout=0)["percent"] == 100.0


class TestPullCancellation:
    """Cancel is a per-chunk question, not a per-snapshot one."""

    def test_cancel_lands_on_the_next_chunk_not_the_next_snapshot(self):
        """A throttle interval no snapshot ever reaches still cancels at once."""
        docker = DockerService(client=_fake_sdk_client())
        snapshots: list[dict] = []
        asked = 0

        def _cancel() -> bool:
            nonlocal asked
            asked += 1
            return asked > 2

        with pytest.raises(PullCancelled) as caught:
            docker.pull_image(
                MISSING_IMAGE,
                snapshots.append,
                # An hour: a flag read inside the progress callback would
                # never get the chance to see it.
                interval=3600,
                cancel=_cancel,
                stall_timeout=0,
            )

        assert MISSING_IMAGE in str(caught.value)
        assert snapshots == [], "no progress snapshot fired, yet the cancel landed"
        assert asked == 3, "the flag is read once per chunk"
        assert docker.image_exists(MISSING_IMAGE) is False

    def test_a_pull_nobody_cancels_completes(self):
        """The cancel hook is consulted, not assumed."""
        docker = DockerService(client=_fake_sdk_client())

        result = docker.pull_image(MISSING_IMAGE, cancel=lambda: False)

        assert result["percent"] == 100.0
