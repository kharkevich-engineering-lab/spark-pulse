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

**All fifteen interface methods, not six.** This file used to exercise six of
:data:`NODE_SERVICE_METHODS`; ``docs/transport-reexamined.md`` §5.1 audited the
seam method by method and found thirty semantic divergences between the two
real implementations, three of them live bugs, and **twenty-seven of the thirty
in the nine methods with no behavioural test at all**. The whole interface is
under contract now, and where two implementations do not agree the difference
is asserted from both sides in :class:`TestDeclaredDifferences` with the reason
it is kept, rather than skipped.
:meth:`TestImplementationsAreDistinct.test_every_interface_method_has_a_behavioural_contract`
is the ratchet: adding a method to the interface without exercising it here
fails, by name.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import threading
from types import SimpleNamespace
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
    CREATED_AT_LABEL,
    DEPLOYMENT_LABEL,
    GENERATION_LABEL,
    IMAGE_LABEL,
    MANAGED_LABEL,
    NAME_LABEL,
    RANK_LABEL,
    RECIPE_LABEL,
    ROLE_LABEL,
    WORLD_SIZE_LABEL,
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
from spark_pulse.tools.ssh import SSHError, SSHResult

# pytest-env forces SIMULATION_MODE=1, so the package attribute
# ``spark_pulse.tools.docker`` is the mock re-export. ``copy_to_container`` is
# the one method whose local implementation shells out instead of going through
# the SDK, so the contract test has to stub the process the real module starts.
real_docker = importlib.import_module("spark_pulse.tools.docker")

IMAGE = "ghcr.io/example/engine:latest"
IMAGE_SIZE = 26_843_545_600
MISSING_IMAGE = "ghcr.io/example/engine:not-pulled"
IDLE_COMMAND = ["sleep", "infinity"]

#: Markers the fake SDK client writes on each of the container's two streams.
LOG_STDOUT_MARK = "contract-stdout"
LOG_STDERR_MARK = "contract-stderr"

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

        def _stop(timeout=10):
            """Stop, and *stay*. Removal is ``remove``'s job, as in Docker."""
            container.status = "exited"
            container.attrs["State"] = {"Status": "exited", "Running": False}

        container.stop.side_effect = _stop
        container.remove.side_effect = lambda force=False: store.pop(name, None)
        # ``container.logs()`` is one stream carrying both of the container's:
        # the SDK asks for stdout and stderr together by default, and engines
        # write most of their output to stderr.
        container.logs.side_effect = lambda tail=None, **_kw: (
            f"{LOG_STDOUT_MARK} {name}\n{LOG_STDERR_MARK} {name}\n".encode()
        )
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

    def _image_remove(ref, force=False, **_kwargs):
        if ref not in images:
            raise _NotFound(f"No such image: {ref}")
        removed = images.pop(ref)
        for key in [k for k, v in images.items() if v is removed]:
            del images[key]

    client.containers.run.side_effect = _make_container
    client.containers.get.side_effect = _get
    client.containers.list.side_effect = _list
    client.images.get.side_effect = _image_get
    client.images.list.side_effect = lambda **_kw: list(images.values())
    client.images.remove.side_effect = _image_remove
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


class _MkdirRefused(SimulatedDockerSSHClient):
    """A node whose ``mkdir -p`` is refused, everything else intact."""

    def _exec_one(self, host: str, command: str):
        if command.strip().startswith("mkdir"):
            return SSHResult(returncode=1, stdout="", stderr="Permission denied")
        return super()._exec_one(host, command)


@pytest.fixture
def local_file(tmp_path) -> str:
    """A single file on this machine, to be copied into a container."""
    path = tmp_path / "launch.sh"
    path.write_text("#!/bin/bash\necho contract\n")
    return str(path)


@pytest.fixture
def local_tree(tmp_path) -> str:
    """A mod directory *with a subdirectory* — the shape that used to fail."""
    root = tmp_path / "mod"
    (root / "templates").mkdir(parents=True)
    (root / "run.sh").write_text("#!/bin/bash\necho mod\n")
    (root / "templates" / "chat.jinja").write_text("{{ messages }}\n")
    return str(root)


@pytest.fixture
def docker_cp(monkeypatch):
    """Answer the local path's ``docker cp`` without a Docker daemon.

    ``DockerService.copy_to_container`` shells out rather than going through
    the SDK, so this is the one method whose local implementation needs a
    process stubbed rather than a client injected.
    """
    calls: list[list[str]] = []

    def _run(args, **_kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        real_docker,
        "subprocess",
        SimpleNamespace(run=_run, TimeoutExpired=subprocess.TimeoutExpired),
    )
    return calls


# ── Reaching past the service, into whatever is standing in for a daemon ────
#
# Three of the fifteen methods can only be put under contract if the test can
# set the node up first: a container that is stopped but still there, a daemon
# that has stopped answering. Every one of these pokes the *backing store* —
# the fake SDK client, the in-memory client, the simulated node — and never the
# service, so what is under test stays the production code.


def _backing_service(service):
    """The :class:`DockerService` behind ``service``, or None for a peer."""
    if isinstance(service, RemoteNodeService):
        return None if not service.is_local else service._local
    return service


def _exit_container(service, name: str) -> None:
    """Leave ``name`` on the node stopped **and still present**.

    Which is the state a container is in between ``docker stop`` and
    ``docker rm`` — the state that used to be permanent on every peer.
    """
    backing = _backing_service(service)
    if backing is None:
        service._ssh.containers_on(service.node.address)[name]["State"] = "exited"
        return
    container = backing.client.containers.get(name)
    container.status = "exited"
    container.attrs["State"] = {"Status": "exited", "Running": False}


def _stop_answering(service) -> None:
    """Make the node's daemon stop answering, without unmaking the node."""
    backing = _backing_service(service)
    if backing is None:
        service._ssh.daemon_down_hosts.add(service.node.address)
        return

    def _refuse(*_args, **_kwargs):
        raise RuntimeError("Cannot connect to the Docker daemon at unix://...")

    backing.client.containers.list = _refuse


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


#: Defaults that are allowed to differ, and why. One entry, deliberately hard
#: to add to: ``MockDockerService.pull_image`` throttles at zero seconds so
#: simulation's handful of ticks all reach the UI instead of being folded into
#: one event. It changes how chatty a simulated pull is and nothing else, and
#: the mock's own docstring says so.
DELIBERATE_DEFAULTS = {("mock", "pull_image", "interval")}

#: Which suite below puts each interface method under contract.
#:
#: This is the map ``test_every_interface_method_has_a_behavioural_contract``
#: checks against :data:`NODE_SERVICE_METHODS`. It exists because the previous
#: version of this file covered six of the fifteen methods and nothing said so
#: — ``test_every_implementation_offers_the_whole_interface`` only checked
#: ``hasattr``, which every one of the nine untested methods passed while
#: twenty-seven divergences sat inside them.
COVERED_BY: dict[str, tuple[str, ...]] = {
    "run_container": (
        "TestContainerServiceContract",
        "TestGangIdentityContract",
        "TestRestartPolicy",
        "TestRunContainerLabels",
        "TestMemorySwapDerivation",
    ),
    "ensure_directories": ("TestEnsureDirectoriesContract",),
    "stop_container": ("TestContainerServiceContract", "TestStopRemoves"),
    "get_container_status": ("TestContainerStatusContract",),
    "exec_in_container": ("TestContainerServiceContract", "TestExecContract"),
    "copy_to_container": ("TestCopyIntoContainerContract",),
    "get_logs": ("TestLogsContract",),
    "list_managed_containers": (
        "TestContainerServiceContract",
        "TestListManagedContract",
    ),
    "get_container_by_deployment": ("TestFindContract",),
    "get_container_by_recipe": ("TestFindContract",),
    "image_exists": ("TestImageContract", "TestImageInfoContract"),
    "image_info": ("TestImageInfoContract",),
    "list_images": ("TestListImagesContract",),
    "pull_image": ("TestImageContract", "TestPullCancellationContract"),
    "remove_image": ("TestRemoveImageContract",),
}


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

    def test_every_signature_matches_the_one_docker_service_publishes(self):
        """Parameter names *and* defaults, not just arity.

        ``DockerService`` is the interface — the protocol's own docstring says
        so — and a caller holds one of these without knowing which. A default
        that differs between implementations is a divergence nothing else
        would catch: ``pull_image``'s ``interval`` differing by service would
        change how chatty a peer's pull is with no call site touched.
        """
        import inspect

        reference = DockerService(client=_fake_sdk_client())
        for name, factory in IMPLEMENTATIONS.items():
            service = factory(NODES["peer"])
            for method in NODE_SERVICE_METHODS:
                theirs = inspect.signature(getattr(service, method))
                ours = inspect.signature(getattr(reference, method))
                assert list(theirs.parameters) == list(
                    ours.parameters
                ), f"{name}.{method} takes different arguments"
                for parameter, expected in ours.parameters.items():
                    if (name, method, parameter) in DELIBERATE_DEFAULTS:
                        continue
                    assert (
                        theirs.parameters[parameter].default == expected.default
                    ), f"{name}.{method}({parameter}=) has a different default"

    def test_every_interface_method_has_a_behavioural_contract(self):
        """The guard that keeps this file honest as the interface grows.

        Six of the fifteen methods had a behavioural test and nine had none,
        and twenty-seven of the thirty divergences
        ``docs/transport-reexamined.md`` §5.1 found lived in the nine. Adding a
        method to :data:`NODE_SERVICE_METHODS` without exercising it here now
        fails, by name.
        """
        module = sys.modules[__name__]
        assert sorted(COVERED_BY) == sorted(
            NODE_SERVICE_METHODS
        ), "COVERED_BY and NODE_SERVICE_METHODS disagree about the interface"
        for method, class_names in COVERED_BY.items():
            assert class_names, f"{method} has no contract test"
            for class_name in class_names:
                suite = getattr(module, class_name, None)
                assert suite is not None, f"{method} names a missing suite {class_name}"
                assert [
                    attribute
                    for attribute in vars(suite)
                    if attribute.startswith("test_")
                ], f"{class_name} has no tests, so {method} is uncovered"


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


class TestGangIdentityContract:
    """Every implementation must agree about identity and about restarting."""

    def test_a_rank_container_carries_its_identity(self, service):
        info = service.run_container(
            image=IMAGE,
            name="contract-rank",
            env_vars={},
            metadata=_metadata("contract-gang", generation=2, rank=1, world_size=4),
        )

        assert info.labels[GENERATION_LABEL] == "2"
        assert info.labels[RANK_LABEL] == "1"
        assert info.labels[WORLD_SIZE_LABEL] == "4"

    def test_a_container_without_a_generation_carries_no_identity_labels(self, service):
        """The label set of everything that is not a rank is unchanged."""
        info = service.run_container(
            image=IMAGE,
            name="contract-plain",
            env_vars={},
            metadata=_metadata("contract-plain"),
        )

        assert GENERATION_LABEL not in info.labels
        assert RANK_LABEL not in info.labels
        assert WORLD_SIZE_LABEL not in info.labels


class TestRestartPolicy:
    """A rebooting node must not resurrect a rank into a torn-down gang."""

    def test_the_sdk_path_asks_for_no_restart(self):
        client = _fake_sdk_client()
        DockerService(client=client).run_container(
            image=IMAGE,
            name="contract-restart",
            env_vars={},
            metadata=_metadata("contract-restart"),
        )

        kwargs = client.containers.run.call_args.kwargs
        assert kwargs["restart_policy"] == {"Name": "no"}

    def test_the_ssh_path_asks_for_no_restart(self):
        ssh = _simulated_ssh()
        RemoteNodeService(NODES["peer"], ssh_client=ssh).run_container(
            image=IMAGE,
            name="contract-restart-remote",
            env_vars={},
            metadata=_metadata("contract-restart-remote"),
        )

        run = next(c["command"] for c in ssh.commands if " run " in c["command"])
        assert "--restart no" in run


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

    def test_reconcile_reads_a_ranks_identity_back(self):
        """Rank, generation and world size survive the round trip to Docker.

        Not a plan-level assertion: the identity has to reach the daemon
        through whichever service wrote the container, or reaping a leftover
        generation has nothing to go on.
        """
        docker = DockerService(client=_fake_sdk_client())
        docker.run_container(
            image=IMAGE,
            name="spark-pulse-contract-r1-g3",
            env_vars={},
            metadata=_metadata("contract", generation=3, rank=1, world_size=2),
        )

        deployments = _reconcile_deployments_real(docker)

        assert deployments[0]["generation"] == 3
        assert deployments[0]["rank"] == 1
        assert deployments[0]["world_size"] == 2

    def test_a_container_without_a_generation_reads_as_a_lone_rank_zero(self):
        """A container written before ranks existed is exactly what it was."""
        docker = DockerService(client=_fake_sdk_client())
        docker.run_container(
            image=IMAGE,
            name="spark-pulse-legacy",
            env_vars={},
            metadata=_metadata("legacy"),
        )

        deployment = _reconcile_deployments_real(docker)[0]

        assert (deployment["generation"], deployment["rank"]) == (0, 0)
        assert deployment["world_size"] == 1

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


# ── The nine methods that had no behavioural contract ───────────────────────
#
# `docs/transport-reexamined.md` §5.1 audited this seam method by method and
# found thirty semantic divergences across the fifteen interface methods, three
# of them live bugs — and twenty-seven of the thirty in the nine methods
# nothing here exercised. What follows closes that: every one of the fifteen is
# driven through every implementation, and where two implementations may
# legitimately differ the difference is asserted deliberately rather than
# skipped, so it cannot drift further without saying so out loud.


class TestRunContainerLabels:
    """Two services build the labels; one function has to make them."""

    def test_every_container_carries_its_own_name_as_a_label(self, service):
        """``reconciliation`` reads the name back out of this label.

        The SDK path stamped it and the CLI path did not, so a rank rebuilt
        from a peer's labels had no name of its own and fell through to the
        container's — which works until a container has more than one name.
        """
        info = _run(service, "contract-name", _metadata("contract-name"))

        assert info.labels[NAME_LABEL] == "contract-name"

    def test_every_container_carries_a_creation_time(self, service):
        """The label is the only place the creation time survives us."""
        info = _run(service, "contract-created", _metadata("contract-created"))

        assert info.labels[CREATED_AT_LABEL]
        assert info.metadata.created_at == info.labels[CREATED_AT_LABEL]

    def test_a_creation_time_the_caller_supplied_is_kept(self, service):
        """The deploy planner stamps one time for the whole gang."""
        stamped = "2026-01-01T00:00:00+00:00"
        metadata = _metadata("contract-stamped")
        metadata.created_at = stamped

        info = service.run_container(
            image=IMAGE, name="contract-stamped", env_vars={}, metadata=metadata
        )

        assert info.labels[CREATED_AT_LABEL] == stamped

    def test_the_image_is_recorded_on_metadata_that_did_not_carry_one(self, service):
        """``ContainerMetadata.image`` defaults from the image being run."""
        metadata = ContainerMetadata(deployment="contract-image", recipe="r", mode="s")

        info = service.run_container(
            image=IMAGE, name="contract-image", env_vars={}, metadata=metadata
        )

        assert info.metadata.image == IMAGE
        assert info.labels[IMAGE_LABEL] == IMAGE

    def test_the_id_handed_back_is_the_id_the_node_reports(self, service):
        """A truncated id compares equal to nothing.

        ``docker ps`` prints twelve hex characters unless asked for
        ``--no-trunc`` and the SDK hands back all sixty-four, so any cross-node
        comparison of container ids silently failed.
        """
        info = _run(service, "contract-id", _metadata("contract-id"))

        assert info.id
        assert service.get_container_status("contract-id")["id"] == info.id
        listed = next(
            c for c in service.list_managed_containers() if c.name == "contract-id"
        )
        assert listed.id == info.id


class TestMemorySwapDerivation:
    """``run_kwargs_from_docker_config`` claims both paths derive swap."""

    def test_the_sdk_path_derives_the_swap_limit(self):
        client = _fake_sdk_client()
        DockerService(client=client).run_container(
            image=IMAGE,
            name="contract-swap",
            env_vars={},
            metadata=_metadata("contract-swap"),
            memory_limit_gb=100,
        )

        kwargs = client.containers.run.call_args.kwargs
        assert kwargs["memswap_limit"] == int(110 * 1024 * 1024 * 1024)

    def test_the_ssh_path_derives_the_same_swap_limit(self):
        """It used to derive none, so a peer's rank ran with swap unlimited."""
        ssh = _simulated_ssh()
        RemoteNodeService(NODES["peer"], ssh_client=ssh).run_container(
            image=IMAGE,
            name="contract-swap-remote",
            env_vars={},
            metadata=_metadata("contract-swap-remote"),
            memory_limit_gb=100,
        )

        run = next(c["command"] for c in ssh.commands if " run " in c["command"])
        assert "--memory 100g" in run
        assert f"--memory-swap {int(110 * 1024 * 1024 * 1024)}" in run


class TestStopRemoves:
    """The bug that leaked an orphan on every peer, permanently."""

    def test_a_stopped_container_is_gone_not_merely_stopped(self, service):
        """``missing`` is the only thing that frees a rank's ports.

        ``DockerService.stop_container`` stops **and removes**; the SSH path
        issued ``docker stop`` and nothing else, and containers are created
        with ``auto_remove=False``. So the container stayed in ``exited``
        answering ``docker inspect`` forever, ``_is_confirmed_gone`` never saw
        ``missing``, ``_confirm_gone`` spun out its full 30 s, and the rank was
        recorded as an outstanding orphan holding its port range for good —
        per rank, per teardown, on every peer.
        """
        _run(service, "contract-gone", _metadata("contract-gone"))
        assert service.stop_container("contract-gone") is True

        assert service.get_container_status("contract-gone")["status"] == "missing"

    def test_stopping_a_container_that_is_not_there_is_false(self, service):
        assert service.stop_container("contract-never-existed") is False

    def test_stopping_an_already_stopped_container_still_removes_it(self, service):
        """Teardown gets retried, and a retry has to finish the job.

        This is also the migration path for the orphans the leak already
        wrote: ``sweep_orphans`` re-asks for the stop when the node answers
        and the container is still there.
        """
        _run(service, "contract-restop", _metadata("contract-restop"))
        _exit_container(service, "contract-restop")

        assert service.stop_container("contract-restop") is True
        assert service.get_container_status("contract-restop")["status"] == "missing"


class TestContainerStatusContract:
    """Four answers: Docker's own word, ``missing``, ``unknown``, or a raise."""

    def test_a_running_container_says_so(self, service):
        info = _run(service, "contract-status", _metadata("contract-status"))

        status = service.get_container_status("contract-status")

        assert status["status"] == "running"
        assert status["running"] is True
        assert status["id"] == info.id
        assert status["error"] is None
        assert status["state"]

    def test_a_container_that_is_not_there_is_missing(self, service):
        status = service.get_container_status("contract-absent")

        assert status["status"] == "missing"
        assert status["running"] is False
        assert status["id"] is None
        assert "contract-absent" in status["error"]

    def test_a_stopped_container_reports_dockers_own_word(self, service):
        """``exited``, not ``stopped``.

        The CLI path used to fold everything that was not running into
        ``stopped`` — a word Docker does not use and the SDK path never
        returns. ``reconciliation._clean_orphaned_containers`` sweeps on
        ``status == "exited"``, so on a peer it could never fire at all.
        """
        _run(service, "contract-exited", _metadata("contract-exited"))
        _exit_container(service, "contract-exited")

        status = service.get_container_status("contract-exited")

        assert status["status"] == "exited"
        assert status["running"] is False


class TestExecContract:
    """Exec, in every shape the deploy path uses."""

    def test_exec_takes_a_command_as_a_string(self, service):
        _run(service, "contract-exec-str", _metadata("contract-exec-str"))

        result = service.exec_in_container("contract-exec-str", "echo contract-ok")

        assert isinstance(result, ExecResult)
        assert result.ok

    def test_a_detached_exec_succeeds_and_says_nothing(self, service):
        """``_deploy_script`` launches the engine this way."""
        _run(service, "contract-exec-detach", _metadata("contract-exec-detach"))

        result = service.exec_in_container(
            "contract-exec-detach", IDLE_COMMAND, detach=True
        )

        assert result.ok
        assert result.stdout == ""

    def test_exec_accepts_the_container_object_the_sdk_hands_back(self, service):
        """The SDK path documents taking one; the SSH path used to ``repr`` it.

        Interpolating an object into a shell command produced ``docker exec
        <Container object at 0x…> …``, which fails in a way that reads like the
        container is missing.
        """
        _run(service, "contract-exec-obj", _metadata("contract-exec-obj"))

        class _Handle:
            name = "contract-exec-obj"

        backing = _backing_service(service)
        handle = (
            backing.client.containers.get("contract-exec-obj")
            if backing is not None
            else _Handle()
        )

        assert service.exec_in_container(handle, ["echo", "contract-ok"]).ok


class TestLogsContract:
    """A log pane that quietly drops half the output is worse than none."""

    def test_logs_come_back_for_a_running_container(self, service):
        _run(service, "contract-logs", _metadata("contract-logs"))

        assert service.get_logs("contract-logs").strip()

    def test_logs_for_a_container_that_is_not_there_say_so(self, service):
        """The SSH path used to return "", which reads as "it said nothing"."""
        logs = service.get_logs("contract-logs-absent")

        assert "not found" in logs
        assert "contract-logs-absent" in logs

    def test_a_tail_is_accepted_and_answered(self, service):
        _run(service, "contract-logs-tail", _metadata("contract-logs-tail"))

        assert service.get_logs("contract-logs-tail", tail=5).strip()


class TestListManagedContract:
    """An empty list is a claim about the world, not a shrug."""

    def test_a_node_that_did_not_answer_is_not_an_empty_node(self, service):
        """The daemon-versus-missing bug's unfixed twin, three methods away.

        ``list_managed_containers`` returned ``[]`` whenever the command
        failed, so a peer whose Docker had died erased that peer's deployments
        and clusters from reconciliation, gave ``_clean_orphaned_containers``
        nothing to clean, and told ``native_runtime._stale_names`` there was no
        earlier generation to reap — which starts a new rank on top of one that
        may still be holding the GPU. The local path lets the exception out;
        so does this one now.
        """
        _run(service, "contract-silent", _metadata("contract-silent"))
        _stop_answering(service)

        with pytest.raises(Exception):
            service.list_managed_containers()

    def test_the_listing_reports_dockers_own_status_word(self, service):
        _run(service, "contract-list-exited", _metadata("contract-list-exited"))
        _exit_container(service, "contract-list-exited")

        found = next(
            c
            for c in service.list_managed_containers()
            if c.name == "contract-list-exited"
        )

        assert found.status == "exited"

    def test_only_managed_containers_are_listed(self, service):
        """Only spark-pulse's own containers, whichever service asks."""
        _run(service, "contract-listed", _metadata("contract-listed"))

        listed = service.list_managed_containers()

        assert listed
        assert all(c.labels.get(MANAGED_LABEL) == "true" for c in listed)


class TestFindContract:
    """The two label lookups the routers and the runtime go through."""

    def test_find_by_deployment_finds_the_container(self, service):
        _run(service, "contract-find", _metadata("contract-find"))

        found = service.get_container_by_deployment("contract-find")

        assert found is not None
        assert found.name == "contract-find"
        assert found.metadata.deployment == "contract-find"

    def test_find_by_deployment_is_none_when_there_is_none(self, service):
        assert service.get_container_by_deployment("contract-no-such") is None

    def test_find_by_recipe_returns_every_container_of_that_recipe(self, service):
        _run(service, "contract-recipe-a", _metadata("contract-recipe-a"))
        _run(service, "contract-recipe-b", _metadata("contract-recipe-b"))

        found = service.get_container_by_recipe("qwen3-contract")

        assert sorted(c.name for c in found) == [
            "contract-recipe-a",
            "contract-recipe-b",
        ]

    def test_find_by_recipe_is_empty_when_there_is_none(self, service):
        assert service.get_container_by_recipe("no-such-recipe") == []


class TestImageInfoContract:
    """One image, described the same way by every service."""

    def test_image_info_describes_a_present_image(self, service):
        info = service.image_info(IMAGE)

        assert info is not None
        assert info["id"]
        assert info["size_bytes"] == IMAGE_SIZE
        assert IMAGE in info["repo_tags"]
        assert info["created"]
        assert isinstance(info["repo_digests"], list)

    def test_image_info_is_none_for_an_image_that_is_not_there(self, service):
        assert service.image_info(MISSING_IMAGE) is None

    def test_image_exists_is_false_for_an_empty_reference(self, service):
        assert service.image_exists("") is False


class TestListImagesContract:
    """Every image weighs something, on every node."""

    def test_the_listing_carries_real_sizes(self, service):
        """A peer's images used to weigh exactly nothing.

        ``docker images`` publishes ``Size`` only as prose (``26.8GB``), so
        the SSH path hardcoded ``"size_bytes": 0`` while the SDK path reported
        the real number — and the fleet's disk arithmetic was wrong by the
        whole of every peer.
        """
        listed = service.list_images()

        assert listed
        assert all(entry["size_bytes"] > 0 for entry in listed), listed

    def test_a_listed_image_is_shaped_exactly_like_image_info(self, service):
        row = next(
            entry for entry in service.list_images() if IMAGE in entry["repo_tags"]
        )

        assert row == service.image_info(IMAGE)

    def test_a_pulled_image_joins_the_listing(self, service):
        service.pull_image(MISSING_IMAGE)

        assert any(
            MISSING_IMAGE in entry["repo_tags"] for entry in service.list_images()
        )


class TestRemoveImageContract:
    """Removal, and the difference between "gone" and "was never here"."""

    def test_removing_an_image_takes_it_away(self, service):
        assert service.remove_image(IMAGE) is True
        assert service.image_exists(IMAGE) is False

    def test_removing_an_image_that_is_not_there_is_false(self, service):
        assert service.remove_image(MISSING_IMAGE) is False


class TestPullCancellationContract:
    """A teardown during a pull is a stop, not a failure."""

    def test_a_cancelled_pull_raises_on_every_implementation(self, service):
        """``PullCancelled`` could only ever fire for the control node.

        ``native_runtime.start`` catches it to record a deployment torn down
        during its own image pull as ``stopped``; the SSH path ignored
        ``cancel`` outright, so the same teardown on a peer was recorded as
        ``error`` — the exact miscategorisation the handler exists to prevent.

        What is *not* promised is that the image did not land. Neither
        implementation can promise that: the SDK path abandons a stream the
        daemon goes on serving, and the CLI path can only read ``cancel``
        before and after a blocking ``docker pull``. The contract is the
        exception, which is what the caller acts on.
        """
        with pytest.raises(PullCancelled):
            service.pull_image(MISSING_IMAGE, cancel=lambda: True)

    def test_a_pull_nobody_cancels_completes_on_every_implementation(self, service):
        result = service.pull_image(MISSING_IMAGE, cancel=lambda: False)

        assert result["percent"] == 100.0
        assert service.image_exists(MISSING_IMAGE) is True


class TestEnsureDirectoriesContract:
    """Bind sources have to exist, or Docker invents them owned by root."""

    def test_creating_directories_reports_nothing_left_undone(self, service, tmp_path):
        wanted = [str(tmp_path / "cache"), str(tmp_path / "models")]

        assert service.ensure_directories(wanted) == []

    def test_blank_and_empty_paths_are_ignored(self, service):
        assert service.ensure_directories([]) == []
        assert service.ensure_directories(["", "   "]) == []

    def test_whitespace_around_a_path_is_stripped(self, service, tmp_path):
        """A trailing newline out of a config file is not part of the name."""
        wanted = tmp_path / "padded"

        assert service.ensure_directories([f"  {wanted}\n"]) == []

        backing = _backing_service(service)
        if backing is None:
            assert str(wanted) in service._ssh.directories[service.node.address]
        elif isinstance(backing, MockDockerService):
            assert str(wanted) in backing.ensured
        else:
            assert wanted.is_dir()


class TestCopyIntoContainerContract:
    """``docker cp`` takes files and directories alike — on every node."""

    def test_copying_a_file_into_a_container_succeeds(
        self, service, local_file, docker_cp
    ):
        _run(service, "contract-cp", _metadata("contract-cp"))

        assert (
            service.copy_to_container("contract-cp", local_file, "/tmp/there") is True
        )

    def test_copying_a_directory_into_a_container_succeeds(
        self, service, local_tree, docker_cp
    ):
        """A mod with a subdirectory worked here and failed on every peer.

        ``_apply_mods`` copies every entry of a mod directory and says in a
        comment that ``docker cp`` takes directories — which it does. The peer
        path staged with :meth:`SSHClient.copy`, which is ``scp`` with no
        ``-r``, so the directory never arrived and the mod's ``run.sh`` failed
        on the far side of a fan-out.
        """
        _run(service, "contract-cp-dir", _metadata("contract-cp-dir"))

        assert (
            service.copy_to_container("contract-cp-dir", local_tree, "/tmp/mod") is True
        )

    def test_a_directory_travels_over_the_recursive_transport(self, local_tree):
        """The peer path, at the transport: ``copy_dir``, never ``copy``."""
        ssh = _simulated_ssh()
        service = RemoteNodeService(NODES["peer"], ssh_client=ssh)
        service.run_container(
            image=IMAGE,
            name="contract-cp-recursive",
            env_vars={},
            metadata=_metadata("contract-cp-recursive"),
        )

        assert service.copy_to_container(
            "contract-cp-recursive", local_tree, "/tmp/mod"
        )

        assert ssh.copies[-1]["recursive"] is True

    def test_a_file_travels_over_the_plain_transport(self, local_file):
        ssh = _simulated_ssh()
        service = RemoteNodeService(NODES["peer"], ssh_client=ssh)
        service.run_container(
            image=IMAGE,
            name="contract-cp-plain",
            env_vars={},
            metadata=_metadata("contract-cp-plain"),
        )

        assert service.copy_to_container("contract-cp-plain", local_file, "/tmp/f")

        assert ssh.copies[-1]["recursive"] is False


# ── Where the implementations differ, deliberately ──────────────────────────


class TestDeclaredDifferences:
    """Divergences that are kept, each with the reason it is kept.

    A contract test that skipped these would be a contract test that let them
    drift. Every one of them is asserted from both sides instead, so if either
    side changes, this file says so.
    """

    def test_exec_timeout_binds_a_peer_and_is_advisory_locally(self):
        """The SDK's exec has no per-call deadline; ``ssh`` does.

        Kept because the argument is real on one transport and unavailable on
        the other, and dropping it from the signature would mean a caller
        could not bound a remote exec at all. ``DockerService`` documents the
        asymmetry in its own docstring.
        """
        ssh = _simulated_ssh()
        remote = RemoteNodeService(NODES["peer"], ssh_client=ssh)
        remote.run_container(
            image=IMAGE,
            name="contract-timeout",
            env_vars={},
            metadata=_metadata("contract-timeout"),
        )
        remote.exec_in_container("contract-timeout", ["true"], timeout=7)
        assert ssh.commands[-1]["timeout"] == 7

        client = _fake_sdk_client()
        local = DockerService(client=client)
        local.run_container(
            image=IMAGE,
            name="contract-timeout",
            env_vars={},
            metadata=_metadata("contract-timeout"),
        )
        local.exec_in_container("contract-timeout", ["true"], timeout=7)
        assert (
            "timeout"
            not in client.containers.get("contract-timeout").exec_run.call_args.kwargs
        )

    def test_exec_on_a_container_that_is_not_there(self):
        """The SDK raises; the CLI returns a failed result.

        Kept because both callers — ``_apply_mods`` and ``_launch_rank`` —
        already fail the deploy on either, and turning the SDK's ``NotFound``
        into a synthetic exit code would invent a return code Docker never
        produced.
        """
        with pytest.raises(Exception):
            DockerService(client=_fake_sdk_client()).exec_in_container(
                "contract-nothing", ["true"]
            )

        remote = RemoteNodeService(NODES["peer"], ssh_client=_simulated_ssh())
        result = remote.exec_in_container("contract-nothing", ["true"])
        assert isinstance(result, ExecResult)
        assert result.ok is False

    def test_pull_progress_is_per_layer_locally_and_terminal_on_a_peer(self):
        """``docker pull`` over ssh is one blocking command with no stream.

        Kept, and documented on the method: fetch-once on the control node
        followed by a fan-out is the plan's answer, and it is later work. What
        is *not* kept is the cancel: see
        :class:`TestPullCancellationContract`.
        """
        seen_local: list[dict] = []
        DockerService(client=_fake_sdk_client()).pull_image(
            MISSING_IMAGE, seen_local.append, interval=0
        )
        assert len(seen_local) > 1
        assert seen_local[-1]["layers"] == len(FAKE_LAYERS)

        seen_remote: list[dict] = []
        RemoteNodeService(NODES["peer"], ssh_client=_simulated_ssh()).pull_image(
            MISSING_IMAGE, seen_remote.append, interval=0
        )
        assert len(seen_remote) == 1
        assert seen_remote[0]["layers"] == 0
        assert seen_remote[0]["percent"] == 100.0

    def test_ensure_directories_cannot_attribute_a_failure_on_a_peer(self, tmp_path):
        """One ``mkdir -p`` for the whole list, so one answer for all of them.

        Kept because the alternative is one round trip per cache directory on
        a path that runs before every deploy, and the caller treats the result
        as a warning either way: docker will still start, it will just have
        invented the bind source as root.
        """
        blocker = tmp_path / "a-file"
        blocker.write_text("not a directory")
        local = DockerService(client=_fake_sdk_client())

        failed = local.ensure_directories([str(tmp_path / "fine"), str(blocker / "no")])

        assert failed == [str(blocker / "no")]
        assert (tmp_path / "fine").is_dir()

        remote = RemoteNodeService(NODES["peer"], ssh_client=_MkdirRefused())
        wanted = ["/opt/one", "/opt/two"]
        assert remote.ensure_directories(wanted) == wanted

    def test_a_label_value_with_a_comma_does_not_survive_docker_ps(self):
        """Docker's own listing format, not ours.

        ``docker ps`` renders every label into one comma-separated string, so
        a value containing a comma cannot be parsed back out of it. Kept
        because the fix is not a parser — it is ``docker inspect`` per
        container, which is the Engine-API unification
        ``docs/transport-reexamined.md`` §5.1 recommends and this change
        deliberately does not attempt. Nothing spark-pulse writes contains a
        comma today; this is here so that when something does, a test says so.
        """
        metadata = _metadata("contract-comma", recipe="qwen3,awq")

        local = DockerService(client=_fake_sdk_client())
        local.run_container(
            image=IMAGE, name="contract-comma", env_vars={}, metadata=metadata
        )
        assert local.list_managed_containers()[0].metadata.recipe == "qwen3,awq"

        remote = RemoteNodeService(NODES["peer"], ssh_client=_simulated_ssh())
        remote.run_container(
            image=IMAGE,
            name="contract-comma",
            env_vars={},
            metadata=_metadata("contract-comma", recipe="qwen3,awq"),
        )
        assert remote.list_managed_containers()[0].metadata.recipe == "qwen3"

    def test_a_node_that_cannot_be_reached_raises_where_a_daemon_returns_false(self):
        """``image_exists`` is False for "not there" and raises for "cannot ask".

        There is no local analogue of an unreachable node, so this is not two
        answers to one question: a daemon that refuses is ``False`` on both
        paths, and only the peer has a third case at all. Kept because the
        alternative — swallowing the transport failure into ``False`` — is the
        inference ``list_managed_containers`` was just cured of.
        """
        ssh = _simulated_ssh()
        remote = RemoteNodeService(NODES["peer"], ssh_client=ssh)
        assert remote.image_exists(IMAGE) is True

        ssh.daemon_down_hosts.add(PEER_ADDRESS)
        assert remote.image_exists(IMAGE) is False

        ssh.daemon_down_hosts.clear()
        ssh.fail_hosts.add(PEER_ADDRESS)
        with pytest.raises(SSHError):
            remote.image_exists(IMAGE)

    def test_an_attached_run_is_not_a_shape_either_service_supports(self):
        """``detach=False`` is on the signature and is not honoured anywhere.

        Recorded rather than fixed. The SDK's ``containers.run(detach=False)``
        returns the container's *logs*, not a container, so the local path's
        ``container.id`` would fail; the CLI path reads the first line of a
        foreground ``docker run``'s output as an id, which it is not. Nothing
        in spark-pulse passes it — every caller starts an idle container and
        execs into it — so both paths assume detached. Making them agree means
        either implementing an attached run twice or dropping the argument, and
        the argument is part of ``DockerService``'s published signature.
        """
        import inspect

        for factory in IMPLEMENTATIONS.values():
            signature = inspect.signature(factory(NODES["peer"]).run_container)
            assert signature.parameters["detach"].default is True

    def test_the_simulated_transport_is_faithful_where_it_used_to_be_kind(self):
        """The four simulator properties that were hiding real bugs.

        A simulation that is kinder than the thing it stands in for cannot
        catch a bug in it, and this one was kind in exactly the places the seam
        was broken: ``docker stop`` deleted the container, so the missing
        ``docker rm`` was invisible; ``scp`` accepted a directory, so the
        missing ``-r`` was invisible; ``&&`` was truncated, so a two-command
        teardown could not be told from a one-command one.
        """
        ssh = _simulated_ssh()
        ssh.exec(PEER_ADDRESS, "docker run -d --name sim img")

        # 1. Stop keeps the container; only rm takes it away.
        ssh.exec(PEER_ADDRESS, "docker stop -t 1 sim")
        assert ssh.containers_on(PEER_ADDRESS)["sim"]["State"] == "exited"
        assert ssh.exec(PEER_ADDRESS, "docker inspect sim").ok

        # 2. Clauses run in order, and a failure short-circuits the rest.
        assert ssh.exec(PEER_ADDRESS, "docker rm -f sim && docker inspect sim").ok is (
            False
        )
        assert "sim" not in ssh.containers_on(PEER_ADDRESS)

        # 3. An ``&&`` inside a quoted ``bash -lc`` is one argument, not two
        #    commands — which is how ``_apply_mods`` runs every mod.
        ssh.exec(PEER_ADDRESS, "docker run -d --name sim2 img")
        inner = ssh.exec(
            PEER_ADDRESS, "docker exec sim2 bash -lc 'cd /mods/m && bash run.sh'"
        )
        assert inner.ok
        assert "bash run.sh" in inner.stdout

    def test_the_simulated_service_reports_every_copy_as_done(self, tmp_path):
        """``MockDockerService.copy_to_container`` is unconditionally True.

        Kept because simulation has no container to copy into and no
        filesystem inside one; the alternative is inventing a failure mode
        that nothing on the real path would produce. It is the one method of
        the fifteen whose simulated answer is not derived from state.
        """
        service = MockDockerService(MockDockerClient())

        assert service.copy_to_container("nothing", str(tmp_path), "/tmp/x") is True
