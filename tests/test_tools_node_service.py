"""Tests for the node-bound container service.

The service used to take a host as the first argument of every method, where
an empty string meant "this node". Thirteen call sites passed that empty
string, so operations aimed at a worker ran against the control plane's own
Docker daemon. The node is now fixed at construction, and these tests pin the
two properties that make the mistake unrepresentable:

* a service built for a peer never touches the local daemon, and
* a service built for the control node never shells out to ssh.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from spark_pulse.mock.node_service import SimulatedDockerSSHClient
from spark_pulse.tools.docker import ContainerInfo, ContainerMetadata
from spark_pulse.tools.node_service import (
    NODE_SERVICE_METHODS,
    Node,
    NodeServices,
    RemoteNodeService,
    control_node,
    is_local_address,
    node_for,
    peer_node,
    reset_local_addresses,
    run_kwargs_from_docker_config,
    service_for,
)

PEER = "10.0.0.2"
OTHER_PEER = "10.0.0.3"
IMAGE = "ghcr.io/example/engine:latest"


@pytest.fixture(autouse=True)
def _fresh_local_addresses():
    """Discovery is cached process-wide; do not leak it between tests."""
    reset_local_addresses()
    yield
    reset_local_addresses()


def _metadata(name: str) -> ContainerMetadata:
    return ContainerMetadata(deployment=name, image=IMAGE, mode="cluster")


def _forbidden_local() -> MagicMock:
    """A local Docker service that fails the test if anything reaches it."""
    local = MagicMock(name="local DockerService")
    for method in NODE_SERVICE_METHODS:
        getattr(local, method).side_effect = AssertionError(
            f"a peer-bound service called the local daemon's {method}"
        )
    return local


def _forbidden_ssh() -> MagicMock:
    """An SSH client that fails the test if anything shells out."""
    ssh = MagicMock(name="SSHClient")
    ssh.exec.side_effect = AssertionError("a self-bound service shelled out to ssh")
    ssh.copy.side_effect = AssertionError("a self-bound service scp'd")
    return ssh


def _peer_service(address: str = PEER, ssh=None) -> RemoteNodeService:
    """A service bound to a peer, with a local daemon that must go untouched."""
    return RemoteNodeService(
        peer_node(address),
        ssh_client=ssh or SimulatedDockerSSHClient(images={IMAGE: 10}),
        docker_service=_forbidden_local(),
    )


class TestNodeRecord:
    """The minimal node record, and who counts as "us"."""

    @pytest.mark.parametrize("address", ["", "localhost", "127.0.0.1", "::1"])
    def test_loopback_is_this_machine(self, address):
        assert is_local_address(address) is True

    def test_a_peer_address_is_not_this_machine(self):
        assert is_local_address(PEER) is False

    def test_node_for_resolves_loopback_to_the_control_node(self):
        node = node_for("127.0.0.1")
        assert node.is_self is True
        assert node.id == "control"

    def test_node_for_resolves_a_peer_to_a_peer(self):
        node = node_for(PEER, ssh_user="spark")
        assert node.is_self is False
        assert node.address == PEER
        assert node.ssh_user == "spark"

    def test_an_empty_address_can_never_be_a_peer(self):
        """The old local sentinel must not become an ssh attempt to ""."""
        with pytest.raises(ValueError):
            peer_node("")

    def test_a_node_carries_its_interfaces(self):
        node = Node(id="w1", address=PEER, interfaces=("eth0", "ib0"))
        assert node.interfaces == ("eth0", "ib0")
        assert node.label == PEER


class TestPeerNeverTouchesTheLocalDaemon:
    """Everything a peer-bound service does leaves this machine."""

    def test_run_container_goes_over_ssh(self):
        ssh = SimulatedDockerSSHClient(images={IMAGE: 10})
        service = _peer_service(ssh=ssh)

        info = service.run_container(
            image=IMAGE,
            name="cluster-worker-0",
            env_vars={"NCCL_SOCKET_IFNAME": "eth0"},
            metadata=_metadata("cluster"),
        )

        assert isinstance(info, ContainerInfo)
        assert [c["host"] for c in ssh.commands] == [PEER]
        assert "docker run" in ssh.commands[0]["command"]
        assert "--label" in ssh.commands[0]["command"]

    @pytest.mark.parametrize(
        "call",
        [
            lambda s: s.stop_container("cluster-worker-0"),
            lambda s: s.get_container_status("cluster-worker-0"),
            lambda s: s.exec_in_container("cluster-worker-0", ["ray", "status"]),
            lambda s: s.list_managed_containers(),
            lambda s: s.get_logs("cluster-worker-0"),
            lambda s: s.image_exists(IMAGE),
            lambda s: s.image_info(IMAGE),
            lambda s: s.list_images(),
        ],
        ids=[
            "stop",
            "status",
            "exec",
            "list",
            "logs",
            "image_exists",
            "image_info",
            "list_images",
        ],
    )
    def test_every_read_and_write_reaches_the_peer(self, call):
        """The whole surface, not just the one method somebody remembered."""
        ssh = SimulatedDockerSSHClient(images={IMAGE: 10})
        service = _peer_service(ssh=ssh)

        call(service)

        assert ssh.hosts_seen() == [PEER]

    def test_copy_to_container_stages_on_the_peer(self, tmp_path):
        payload = tmp_path / "run.sh"
        payload.write_text("echo hi\n")
        ssh = SimulatedDockerSSHClient(images={IMAGE: 10})
        service = _peer_service(ssh=ssh)
        service.run_container(
            image=IMAGE,
            name="worker",
            env_vars={},
            metadata=_metadata("cluster"),
        )

        assert service.copy_to_container("worker", str(payload), "/workspace/run.sh")

        assert [c["host"] for c in ssh.copies] == [PEER]
        assert any("docker cp" in c["command"] for c in ssh.commands)

    def test_reaching_for_the_local_daemon_is_an_error(self):
        """A peer has no local Docker daemon, and asking says so."""
        service = _peer_service()
        with pytest.raises(RuntimeError, match="peer"):
            _ = service._local


class TestExecOnAPeerLeavesTheMachine:
    """The regression test for the defect itself.

    On the old code every one of these ran against the control node, because
    the host argument defaulted to the empty string and the callers took the
    default. Here the service is bound to a worker, so an exec that does not
    reach that worker fails.
    """

    def test_ray_start_on_a_worker_is_seen_by_that_worker(self):
        ssh = SimulatedDockerSSHClient()
        service = _peer_service(ssh=ssh)
        service.run_container(
            image=IMAGE, name="c-worker-0", env_vars={}, metadata=_metadata("c")
        )

        service.exec_in_container(
            "c-worker-0", ["ray", "start", "--address", "10.0.0.1:29501"]
        )

        execs = [c for c in ssh.commands if c["command"].startswith("docker exec")]
        assert len(execs) == 1
        assert execs[0]["host"] == PEER
        assert "ray start" in execs[0]["command"]

    def test_two_peers_do_not_share_a_container_store(self):
        """The property the empty host destroyed: nodes are separate."""
        ssh = SimulatedDockerSSHClient()
        first = _peer_service(PEER, ssh=ssh)
        second = _peer_service(OTHER_PEER, ssh=ssh)

        first.run_container(
            image=IMAGE, name="only-on-first", env_vars={}, metadata=_metadata("c")
        )

        assert [c.name for c in first.list_managed_containers()] == ["only-on-first"]
        assert second.list_managed_containers() == []


class TestControlNodeNeverShellsOut:
    """A service bound to this machine uses the SDK and nothing else."""

    def _service(self, local) -> RemoteNodeService:
        return RemoteNodeService(
            control_node(address="127.0.0.1"),
            ssh_client=_forbidden_ssh(),
            docker_service=local,
        )

    def test_is_local_is_true(self):
        assert self._service(MagicMock()).is_local is True

    @pytest.mark.parametrize(
        "call,method",
        [
            (lambda s: s.stop_container("c"), "stop_container"),
            (lambda s: s.get_container_status("c"), "get_container_status"),
            (lambda s: s.exec_in_container("c", ["ray"]), "exec_in_container"),
            (lambda s: s.list_managed_containers(), "list_managed_containers"),
            (lambda s: s.get_logs("c"), "get_logs"),
            (lambda s: s.image_exists(IMAGE), "image_exists"),
            (lambda s: s.image_info(IMAGE), "image_info"),
            (lambda s: s.list_images(), "list_images"),
            (lambda s: s.remove_image(IMAGE), "remove_image"),
            (
                lambda s: s.get_container_by_deployment("d"),
                "get_container_by_deployment",
            ),
            (lambda s: s.get_container_by_recipe("r"), "get_container_by_recipe"),
        ],
        ids=lambda value: value if isinstance(value, str) else "",
    )
    def test_every_call_goes_to_the_sdk(self, call, method):
        local = MagicMock(name="DockerService")
        service = self._service(local)

        call(service)

        assert getattr(local, method).called

    def test_pull_keeps_the_sdk_progress_contract(self):
        """Cancel and the stall watchdog survive the local branch."""
        local = MagicMock(name="DockerService")
        service = self._service(local)
        cancel = object()

        service.pull_image("ref", None, interval=3, cancel=cancel, stall_timeout=7)

        _, kwargs = local.pull_image.call_args
        assert kwargs == {"interval": 3, "cancel": cancel, "stall_timeout": 7}

    def test_reaching_for_ssh_is_an_error(self):
        service = self._service(MagicMock())
        with pytest.raises(RuntimeError, match="not reached over SSH"):
            _ = service._ssh


class TestResolver:
    """``service_for`` binds a node to an implementation, once."""

    def test_a_peer_gets_the_remote_service(self):
        service = service_for(peer_node(PEER))
        assert isinstance(service, RemoteNodeService)
        assert service.node.address == PEER

    def test_the_control_node_gets_the_local_docker_service(self):
        from spark_pulse.tools.docker import DockerService

        service = service_for(control_node())
        assert isinstance(service, DockerService)

    def test_the_control_node_shares_one_service(self):
        """One DockerService process-wide, thread-local clients underneath."""
        assert service_for(control_node()) is service_for(control_node())

    def test_simulation_resolves_to_the_mock(self):
        from spark_pulse.mock.docker import MockDockerService
        from spark_pulse.mock.node_service import service_for as mock_service_for

        assert isinstance(mock_service_for(control_node()), MockDockerService)

    def test_the_cache_hands_back_one_service_per_node(self):
        services = NodeServices()

        first = services(peer_node(PEER))
        again = services(peer_node(PEER))
        other = services(peer_node(OTHER_PEER))

        assert first is again
        assert first is not other

    def test_for_address_routes_loopback_home_and_peers_out(self):
        from spark_pulse.tools.docker import DockerService

        services = NodeServices()

        assert isinstance(services.for_address("127.0.0.1"), DockerService)
        assert isinstance(services.for_address(PEER), RemoteNodeService)


class TestDockerConfigMapping:
    """The cluster API's untyped config blob still reaches run_container."""

    def test_known_keys_are_forwarded(self):
        kwargs = run_kwargs_from_docker_config(
            {
                "privileged": False,
                "memory_limit_gb": 96,
                "shm_size_gb": 32,
                "cache_dirs": ["/models"],
                "port_mappings": ["8000:8000"],
            }
        )

        assert kwargs["privileged"] is False
        assert kwargs["memory_limit_gb"] == 96
        assert kwargs["shm_size_gb"] == 32
        assert kwargs["cache_dirs"] == ["/models"]
        assert kwargs["port_mappings"] == ["8000:8000"]

    def test_unknown_keys_are_dropped_rather_than_exploding(self):
        """gpu_count and memory_swap_limit_gb are the service's own business."""
        kwargs = run_kwargs_from_docker_config(
            {"gpu_count": 1, "memory_swap_limit_gb": 200}
        )

        assert "gpu_count" not in kwargs
        assert "memory_swap_limit_gb" not in kwargs

    def test_an_empty_config_still_yields_the_defaults(self):
        assert run_kwargs_from_docker_config(None)["privileged"] is True


def _self_service() -> RemoteNodeService:
    """A service bound to this machine, which must never shell out."""
    return RemoteNodeService(
        control_node(address="127.0.0.1"),
        ssh_client=_forbidden_ssh(),
        docker_service=MagicMock(),
    )


class TestEnsureDirectories:
    """Bind sources are created before the container, on whichever node it is.

    Docker invents a missing bind source **owned by root**, and every path we
    mount is one of the login user's caches — so a directory that does not
    exist yet is how ``~/.cache/huggingface`` becomes unwritable and the model
    copy fails afterwards. ``launch-cluster.sh`` does the same ``mkdir -p``,
    locally at line 1094 and over SSH at line 1104.
    """

    def test_a_peer_is_asked_over_ssh(self):
        ssh = SimulatedDockerSSHClient(images={IMAGE: 10})
        service = _peer_service(ssh=ssh)

        assert service.ensure_directories(["/home/spark/.cache/vllm", "/x y"]) == []

        assert [c["host"] for c in ssh.commands] == [PEER]
        command = ssh.commands[0]["command"]
        assert command.startswith("mkdir -p ")
        # Quoted, because a path with a space is one path, not two.
        assert "'/x y'" in command

    def test_the_control_node_makes_them_here(self, tmp_path):
        target = tmp_path / "cache" / "vllm"

        assert _self_service().ensure_directories([str(target)]) == []

        assert target.is_dir()

    def test_an_empty_list_asks_nothing_of_anyone(self):
        ssh = SimulatedDockerSSHClient(images={IMAGE: 10})
        service = _peer_service(ssh=ssh)

        assert service.ensure_directories([]) == []
        assert service.ensure_directories(["", "  "]) == []

        assert ssh.commands == []

    def test_a_path_that_cannot_be_made_is_returned_not_raised(self, tmp_path):
        """A failed mkdir is a warning: docker will still start the container."""
        blocker = tmp_path / "file"
        blocker.write_text("not a directory")
        under = str(blocker / "under")

        assert _self_service().ensure_directories([under]) == [under]
