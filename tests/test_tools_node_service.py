"""Tests for the node-bound container service's *boundary*.

The behaviour of a service — all fifteen methods, on both a self node and a
peer, against every implementation — is under contract in
``tests/test_container_service_contract.py``. What is left here is the part
that file cannot exercise: how a :class:`Node` is decided, how one is turned
into a service, and what happens when it cannot be.

The property the module exists to keep is unchanged, and the shape that
guarantees it is stronger than it was. The service used to take a host as the
first argument of every method with an empty string meaning "this node";
thirteen call sites passed that empty string, so operations aimed at a worker
ran against the control plane's own Docker daemon. Fixing the node at
construction removed the argument. Removing the docker-over-SSH service —
which had a local branch inside it — removed the last place where "this node"
could be substituted for another one, because there is no longer any code that
chooses between them.

So the resolver now has exactly two outcomes: the service bound to a node's
agent, or a named refusal. Never a quietly wrong daemon.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from spark_pulse.agent.runtime import ControlPlaneRuntime, use
from spark_pulse.agent.sync_service import AgentNodeService
from spark_pulse.tools.node_service import (
    NoAgent,
    Node,
    NodeServices,
    control_node,
    is_local_address,
    node_for,
    peer_node,
    reset_local_addresses,
    run_kwargs_from_docker_config,
    service_for,
    transfer_address,
    transfer_route,
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


# ── The node record ─────────────────────────────────────────────────────────


class TestNodeRecord:
    """A node is decided once, here, so no caller has to ask "is that us?"."""

    @pytest.mark.parametrize("address", ["", "127.0.0.1", "localhost", "::1"])
    def test_loopback_is_this_machine(self, address):
        assert is_local_address(address) is True

    def test_a_peer_address_is_not_this_machine(self):
        assert is_local_address(PEER) is False

    def test_node_for_resolves_loopback_to_the_control_node(self):
        node = node_for("127.0.0.1")
        assert node.is_self is True
        assert node.address == "127.0.0.1"

    def test_node_for_resolves_a_peer_to_a_peer(self):
        node = node_for(PEER, ssh_user="spark")
        assert node.is_self is False
        assert node.id == PEER
        assert node.ssh_user == "spark"

    def test_an_empty_address_can_never_be_a_peer(self):
        """The old local sentinel. A stray "" must not become a machine."""
        with pytest.raises(ValueError):
            peer_node("")

    def test_a_node_carries_its_interfaces(self):
        node = peer_node(PEER, interfaces=("rocep1s0f1", "roceP2p1s0f1"))
        assert node.interfaces == ("rocep1s0f1", "roceP2p1s0f1")

    def test_the_label_prefers_an_address_over_an_opaque_id(self):
        """It goes in error messages, and a uuid tells an operator nothing."""
        assert peer_node(PEER, node_id="0f3c…").label == PEER
        assert Node(id="0f3c…").label == "0f3c…"


# ── The resolver ────────────────────────────────────────────────────────────


class TestResolver:
    """``service_for`` binds a node to its agent, once, or refuses by name."""

    def test_a_node_resolves_to_a_service_over_its_agent(
        self, agent_server, agent_node
    ):
        runtime = ControlPlaneRuntime(agent_server, asyncio.get_event_loop())
        with use(runtime):
            service = service_for(Node(id=agent_node.node_id, address=PEER))

        assert isinstance(service, AgentNodeService)
        assert service.node_id == agent_node.node_id

    def test_the_control_node_resolves_the_same_way_a_peer_does(
        self, agent_server, agent_node
    ):
        """Not a special case, and not a different class.

        This process runs an agent for itself and reaches it over loopback.
        There is no local branch here — which matters because the local branch
        of the service this replaced was never once executed by a test.
        """

        class Local:
            node_id = agent_node.node_id

        runtime = ControlPlaneRuntime(agent_server, asyncio.get_event_loop())
        runtime.local = Local()
        with use(runtime):
            mine = service_for(control_node())
            theirs = service_for(Node(id=agent_node.node_id, address=PEER))

        assert type(mine) is type(theirs) is AgentNodeService

    def test_a_node_with_no_agent_is_refused_by_name(self, agent_server):
        """Never a fallback. A fallback here reads the wrong machine."""
        runtime = ControlPlaneRuntime(agent_server, asyncio.get_event_loop())
        with use(runtime), pytest.raises(NoAgent) as caught:
            service_for(peer_node(PEER))

        assert PEER in str(caught.value)

    def test_resolving_before_the_transport_is_up_says_so(self):
        """A startup-ordering bug, named as one rather than as a missing node."""
        with use(None), pytest.raises(NoAgent) as caught:
            service_for(peer_node(PEER))

        assert "not running" in str(caught.value)

    def test_a_pinned_service_skips_resolution_entirely(self):
        """For a caller that already holds one. Never used as a fallback."""
        sentinel = object()
        with use(None):
            assert service_for(peer_node(PEER), docker_service=sentinel) is sentinel

    def test_simulation_resolves_to_the_mock(self):
        from spark_pulse.mock.docker import MockDockerService
        from spark_pulse.mock.node_service import service_for as mock_service_for

        assert isinstance(mock_service_for(control_node()), MockDockerService)

    def test_simulation_gives_every_peer_its_own_docker(self):
        """One daemon for all nodes is how a wrong answer looks plausible."""
        from spark_pulse.mock import node_service as mock_node_service

        mock_node_service.reset()
        try:
            first = mock_node_service.docker_for(peer_node(PEER))
            again = mock_node_service.docker_for(peer_node(PEER))
            other = mock_node_service.docker_for(peer_node(OTHER_PEER))

            assert first is again
            assert first is not other
            assert mock_node_service.docker_for(control_node()) is not first
        finally:
            mock_node_service.reset()


class TestResolverCache:
    """Callers act on several nodes, so they hold a resolver, not a service."""

    def test_the_cache_hands_back_one_service_per_node(self):
        built: dict[str, object] = {}

        def resolver(node, **_kwargs):
            built.setdefault(node.id, object())
            return built[node.id]

        services = NodeServices(resolver=resolver)

        first = services(peer_node(PEER))
        again = services(peer_node(PEER))
        other = services(peer_node(OTHER_PEER))

        assert first is again
        assert first is not other

    def test_for_address_decides_self_versus_peer_once(self):
        seen: list[Node] = []

        def resolver(node, **_kwargs):
            seen.append(node)
            return object()

        services = NodeServices(resolver=resolver)
        services.for_address("127.0.0.1")
        services.for_address(PEER)

        assert [node.is_self for node in seen] == [True, False]

    def test_control_asks_for_the_machine_this_process_runs_on(self):
        seen: list[Node] = []

        def resolver(node, **_kwargs):
            seen.append(node)
            return object()

        NodeServices(resolver=resolver).control()

        assert seen[0].is_self is True


# ── The untyped config blob ─────────────────────────────────────────────────


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


# ── Where the bulk bytes go ─────────────────────────────────────────────────


class TestTransferRoute:
    """A node is reached at its registered address; its bytes are not.

    Measured on a real pair: the registered address sat on Wi-Fi at 20 MB/s
    while the ConnectX fabric this control plane had itself configured did
    428-660 MB/s. A 22 GB model was fifteen minutes over the first and under a
    minute over the second.
    """

    @staticmethod
    def _registry(monkeypatch, **by_address):
        """A registry answering with these ``address -> fabric_addresses``."""
        from spark_pulse import tools

        nodes = [
            type("N", (), {"address": address, "fabric_addresses": tuple(fabric)})()
            for address, fabric in by_address.items()
        ]
        monkeypatch.setattr(tools.node_registry, "list_nodes", lambda: nodes)

    def test_the_fabric_address_wins_when_it_is_applied_and_answers(self, monkeypatch):
        self._registry(monkeypatch, **{"192.168.29.152": ["192.168.177.12"]})
        route = transfer_route("192.168.29.152", reachable=lambda _a: True)
        assert route.address == "192.168.177.12"
        assert route.via_fabric is True
        assert "192.168.177.12" in route.reason

    def test_without_a_verified_apply_there_is_no_fabric_address_to_prefer(
        self, monkeypatch
    ):
        self._registry(monkeypatch, **{"192.168.29.152": []})
        route = transfer_route(
            "192.168.29.152",
            reachable=lambda _a: pytest.fail("probed an address nobody recorded"),
        )
        assert route.address == "192.168.29.152"
        assert route.via_fabric is False
        assert "no fabric address" in route.reason

    def test_a_node_the_registry_has_never_heard_of_keeps_its_address(
        self, monkeypatch
    ):
        self._registry(monkeypatch)
        assert transfer_address("10.9.9.9") == "10.9.9.9"

    def test_a_cable_unplugged_after_the_apply_falls_back_rather_than_fails(
        self, monkeypatch
    ):
        """Slow beats failed. The record says applied; the wire says otherwise."""
        self._registry(monkeypatch, **{"192.168.29.152": ["192.168.177.12"]})
        route = transfer_route("192.168.29.152", reachable=lambda _a: False)
        assert route.address == "192.168.29.152"
        assert route.via_fabric is False
        assert "did not answer" in route.reason

    def test_a_mesh_node_is_tried_address_by_address(self, monkeypatch):
        """Three nodes in a ring means a different /24 with each peer.

        Only one of a peer's addresses is on a subnet this machine shares, so
        the first that answers is the one, not the first that is recorded.
        """
        self._registry(
            monkeypatch,
            **{"10.0.0.2": ["192.168.187.13", "192.168.197.13"]},
        )
        asked: list[str] = []

        def probe(address: str) -> bool:
            asked.append(address)
            return address == "192.168.197.13"

        route = transfer_route("10.0.0.2", reachable=probe)
        assert route.address == "192.168.197.13"
        assert asked == ["192.168.187.13", "192.168.197.13"]

    def test_a_fabric_address_equal_to_the_registered_one_is_not_a_choice(
        self, monkeypatch
    ):
        """A node registered *at* its fabric address is already on the fabric."""
        self._registry(monkeypatch, **{"192.168.177.12": ["192.168.177.12"]})
        route = transfer_route(
            "192.168.177.12",
            reachable=lambda _a: pytest.fail("probed the address we arrived on"),
        )
        assert route.address == "192.168.177.12"
        assert route.via_fabric is False

    def test_a_registry_that_cannot_be_read_does_not_stop_a_transfer(self, monkeypatch):
        from spark_pulse import tools

        def boom():
            raise RuntimeError("the database is locked")

        monkeypatch.setattr(tools.node_registry, "list_nodes", boom)
        assert transfer_address("10.0.0.2") == "10.0.0.2"

    def test_the_choice_is_logged_either_way(self, monkeypatch, caplog):
        self._registry(monkeypatch, **{"192.168.29.152": ["192.168.177.12"]})
        with caplog.at_level(logging.INFO):
            transfer_route("192.168.29.152", reachable=lambda _a: True)
        assert "192.168.177.12" in caplog.text
        assert "fabric" in caplog.text

    def test_the_probe_is_a_tcp_connect_to_the_ssh_port(self, monkeypatch):
        """SSH, because SSH is what the transfer then uses."""
        import socket

        seen: list[tuple] = []

        def create_connection(target, timeout):
            seen.append((target, timeout))
            raise OSError("refused")

        monkeypatch.setattr(socket, "create_connection", create_connection)
        # The real submodule by full name: under SIMULATION_MODE the attribute
        # on ``spark_pulse.tools`` is the mock, which re-exports the resolver
        # rather than defining its private probe.
        import importlib

        real_node_service = importlib.import_module("spark_pulse.tools.node_service")

        assert real_node_service._answers("192.168.177.12") is False
        assert seen == [(("192.168.177.12", 22), 1.5)]
