"""Contract tests: every container service must agree, on every node.

Spark Pulse has three implementations of one node-bound container service:

* ``DockerService`` — this machine, Docker SDK.
* ``RemoteNodeService`` — one node: the docker CLI over SSH for a peer, the
  local SDK when the node it was built for is this machine.
* ``MockDockerService`` — simulation mode, in memory.

They are used interchangeably (the orchestrator does not know which one it
holds), so the same scenario runs against all three **against two nodes**: a
self node and a peer. That second axis is the point. The service used to take
the node as the first argument of every method, with an empty string meaning
"this machine", and the contract test hardcoded a remote address — so the
local branch of the remote service was never once executed, while thirteen
call sites passed the empty string and silently drove the control plane's own
daemon. With the node fixed at construction there is nothing to pass, and both
branches are exercised here.

Each implementation is driven through a fake at its own boundary — a mocked
docker SDK client, and the simulated docker-over-SSH transport that simulation
mode itself uses — so the production code paths, not stand-ins for them, run.
"""

from __future__ import annotations

import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from spark_pulse.config import config
from spark_pulse.mock.docker import MockDockerService, MockDockerClient
from spark_pulse.mock.node_service import SimulatedDockerSSHClient
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
from spark_pulse.tools.node_service import (
    NODE_SERVICE_METHODS,
    Node,
    RemoteNodeService,
    control_node,
    peer_node,
)
from spark_pulse.tools.reconciliation import (
    _reconcile_clusters_real,
    _reconcile_deployments_real,
)

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


# ── The three implementations, each bound to a node ─────────────────────────

PEER_ADDRESS = "10.0.0.2"


def _simulated_ssh() -> SimulatedDockerSSHClient:
    """The docker-over-SSH transport simulation mode ships, seeded with IMAGE."""
    return SimulatedDockerSSHClient(images={IMAGE: IMAGE_SIZE})


def _mock_service(_node: Node):
    """Simulation mode's in-memory service."""
    client = MockDockerClient()
    client.images.add(IMAGE, size=IMAGE_SIZE)
    return MockDockerService(client)


def _sdk_service(_node: Node):
    """The real DockerService over a fake docker SDK client."""
    return DockerService(client=_fake_sdk_client())


def _remote_service(node: Node):
    """The real node-bound service: SSH for a peer, the SDK for this machine."""
    return RemoteNodeService(
        node,
        ssh_client=_simulated_ssh(),
        docker_service=DockerService(client=_fake_sdk_client()),
    )


IMPLEMENTATIONS = {
    "mock": _mock_service,
    "docker-sdk": _sdk_service,
    "remote-ssh": _remote_service,
}

NODES = {
    "self": control_node(address="127.0.0.1"),
    "peer": peer_node(PEER_ADDRESS),
}


@pytest.fixture(params=sorted(IMPLEMENTATIONS), ids=sorted(IMPLEMENTATIONS))
def implementation(request):
    """Each container service implementation, as a factory over a node."""
    return IMPLEMENTATIONS[request.param]


@pytest.fixture(params=sorted(NODES), ids=sorted(NODES))
def node(request) -> Node:
    """The node the service under test is bound to."""
    return NODES[request.param]


@pytest.fixture
def service(implementation, node):
    """One container service, bound to one node. No method takes a host."""
    return implementation(node)


def _run(service, name: str, metadata: ContainerMetadata) -> ContainerInfo:
    """Start a contract container. Fills in constants — it adapts nothing.

    The bespoke ``_LocalAdapter``/``_RemoteAdapter`` this replaces existed
    because the two services disagreed about argument order and about whether
    a host came first. They agree now, so there is nothing left to adapt.
    """
    return service.run_container(
        image=IMAGE,
        name=name,
        env_vars={"SPARK_PULSE_CONTRACT": "1"},
        metadata=metadata,
    )


class TestImplementationsAreDistinct:
    """A misconfigured fixture must not silently test one thing three times."""

    def test_every_implementation_is_a_different_class(self):
        """Three implementations means three classes, not one wearing hats."""
        classes = {
            name: type(factory(NODES["peer"]))
            for name, factory in IMPLEMENTATIONS.items()
        }
        assert len(set(classes.values())) == len(IMPLEMENTATIONS), classes

    def test_the_matrix_builds_six_distinct_services(self):
        """Three implementations across two nodes: six separate objects."""
        built = [
            factory(node)
            for factory in IMPLEMENTATIONS.values()
            for node in NODES.values()
        ]
        assert len({id(service) for service in built}) == 6

    def test_the_remote_service_takes_a_different_branch_per_node(self):
        """The self node and the peer are not the same code path.

        This is the assertion the old suite could not make: it hardcoded a
        remote address, so the local branch never ran and the empty-host
        default was never tested at all.
        """
        on_self = _remote_service(NODES["self"])
        on_peer = _remote_service(NODES["peer"])

        assert on_self.is_local is True
        assert on_peer.is_local is False
        assert on_self.node != on_peer.node

    def test_every_implementation_offers_the_whole_interface(self):
        """Signature-identical, which is what let the adapters be deleted."""
        import inspect

        for name, factory in IMPLEMENTATIONS.items():
            service = factory(NODES["peer"])
            for method in NODE_SERVICE_METHODS:
                assert hasattr(service, method), f"{name} is missing {method}"
                signature = inspect.signature(getattr(service, method))
                assert (
                    "host" not in signature.parameters
                ), f"{name}.{method} still takes a host"


# ── The contract ────────────────────────────────────────────────────────────


class TestContainerServiceContract:
    """The same scenario, run against every container service."""

    def test_run_returns_container_info_with_labels(self, service):
        """A started container is described by ContainerInfo and spark-pulse labels."""
        info = _run(service, "contract-run", _metadata("contract-run"))

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
        _run(service, "contract-list", _metadata("contract-list"))

        containers = service.list_managed_containers()
        names = [c.name for c in containers]
        assert "contract-list" in names

        found = next(c for c in containers if c.name == "contract-list")
        assert found.metadata.deployment == "contract-list"
        assert found.metadata.recipe == "qwen3-contract"
        assert found.labels[MANAGED_LABEL] == "true"

    def test_list_managed_filters_by_label(self, service):
        """Label filters select the right containers, and an empty value means any."""
        _run(
            service,
            "contract-head",
            _metadata("contract-cluster", cluster="contract-cluster", role="head"),
        )
        _run(service, "contract-solo", _metadata("contract-solo"))

        in_cluster = service.list_managed_containers(
            {CLUSTER_LABEL: "contract-cluster"}
        )
        assert [c.name for c in in_cluster] == ["contract-head"]
        assert in_cluster[0].metadata.role == "head"

        any_cluster = service.list_managed_containers({CLUSTER_LABEL: ""})
        assert [c.name for c in any_cluster] == ["contract-head"]

        heads = service.list_managed_containers({ROLE_LABEL: "head"})
        assert [c.name for c in heads] == ["contract-head"]

    def test_exec_returns_exec_result(self, service):
        """Exec returns an ok/stdout/stderr result, never a bare string."""
        _run(service, "contract-exec", _metadata("contract-exec"))

        result = service.exec_in_container("contract-exec", ["echo", "contract-ok"])

        assert isinstance(result, ExecResult)
        assert result.ok
        assert result.returncode == 0
        assert "contract-ok" in result.stdout

    def test_stop_removes_the_container(self, service):
        """Stopping a container removes it from the managed listing."""
        _run(service, "contract-stop", _metadata("contract-stop"))
        assert service.stop_container("contract-stop") is True

        names = [c.name for c in service.list_managed_containers()]
        assert "contract-stop" not in names

    def test_idle_container_lifecycle(self, service):
        """The full run -> list -> exec -> stop sequence used by the deploy path."""
        info = _run(service, "contract-idle", _metadata("contract-idle"))
        assert info.status == "running"

        assert "contract-idle" in [c.name for c in service.list_managed_containers()]
        assert service.exec_in_container("contract-idle", IDLE_COMMAND).ok
        assert service.stop_container("contract-idle") is True
        assert "contract-idle" not in [
            c.name for c in service.list_managed_containers()
        ]


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
        remote = _remote_service(NODES["peer"])
        remote.run_container(
            image=IMAGE,
            name="contract-cluster-head",
            env_vars={},
            metadata=_metadata(
                "contract-cluster",
                mode="cluster",
                cluster="contract-cluster",
                role="head",
                head_ip=PEER_ADDRESS,
                ray_enabled=True,
            ),
        )

        # No pinning shim: the service is already bound to that peer, so
        # reconciliation reads the peer's containers by construction.
        clusters = _reconcile_clusters_real(remote)

        assert len(clusters) == 1
        assert clusters[0]["name"] == "contract-cluster"
        assert clusters[0]["head_ip"] == PEER_ADDRESS
        assert clusters[0]["ray_enabled"] is True
        assert clusters[0]["image"] == IMAGE

    def test_reconcile_ignores_unlabelled_containers(self):
        """Containers without our labels are not adopted."""
        client = _fake_sdk_client()
        client.containers.run(IMAGE, name="someone-elses", labels={"other": "1"})

        assert _reconcile_deployments_real(DockerService(client=client)) == []


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
