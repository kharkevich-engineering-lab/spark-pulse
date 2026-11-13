"""A simulated workstation: discrete GPU memory and rootless Podman (#174).

CI has no workstation, so the paths written for one — discrete memory (#171),
recipe fit by memory (#173), the runtime checks for rootless Podman (#168) —
ran only on the lab desktop. The simulated workstation answers what that
desktop's agent reported, and these tests hold each of those paths to it.
"""

from __future__ import annotations

import pytest

from spark_pulse import tools
from spark_pulse.mock import node_registry as mock_registry
from spark_pulse.mock import node_service as mock_node_service
from spark_pulse.mock import profiles
from spark_pulse.tools import node_stats, recipe_fit
from spark_pulse.tools.backends import runtime_checks
from spark_pulse.tools.node_service import Node

GiB = 1024**3
CONTROL = "192.168.1.100"
PEER = "10.0.0.11"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("SPARK_PULSE_SIM_PROFILE", raising=False)
    profiles.reset()
    mock_registry.reset()
    mock_node_service.reset()
    recipe_fit._cache.clear()
    yield
    profiles.reset()
    mock_registry.reset()
    mock_node_service.reset()
    recipe_fit._cache.clear()


def peer() -> Node:
    record = next(n for n in mock_registry.list_nodes() if n.address == PEER)
    return Node(id=record.id, address=PEER)


def peer_id() -> str:
    return next(n.id for n in mock_registry.list_nodes() if n.address == PEER)


class TestTheSparkIsUnchanged:
    """The default machine is the one simulation always had."""

    def test_its_gpu_memory_is_the_hosts(self):
        memory = node_stats.node_memory(tools.node_service.service_for(peer()))
        assert memory.model == node_stats.MEMORY_UNIFIED

    def test_it_reports_no_agent_facts(self):
        assert tools.node_facts.facts_for(peer_id()) is None


class TestTheWorkstation:
    @pytest.fixture(autouse=True)
    def workstation(self):
        profiles.use(PEER, profiles.WORKSTATION)

    def test_its_card_has_memory_of_its_own(self):
        memory = node_stats.node_memory(tools.node_service.service_for(peer()))
        assert memory.model == node_stats.MEMORY_DISCRETE
        assert memory.total_bytes == 12 * GiB
        assert memory.free_bytes == 12 * GiB - 960 * 1024**2

    def test_its_agent_reports_rootless_podman_on_amd64(self):
        facts = tools.node_facts.facts_for(peer_id())
        assert facts.platform == "linux/amd64"
        assert facts.container_runtime == "podman"
        assert facts.runtime_version == "4.9.3"
        assert facts.rootless is True
        assert "nvidia.com/gpu=all" in facts.cdi_devices
        # No Docker daemon: the doctor reads an empty version as "not running".
        assert facts.docker_version == ""

    def test_a_recipe_is_fitted_against_the_cards_twelve_gigabytes(self):
        capacity = recipe_fit.node_capacity(peer_id(), PEER, False)
        # The first ask starts the background gather; the second reads it.
        for _ in range(50):
            capacity = recipe_fit.node_capacity(peer_id(), PEER, False)
            if capacity is not None:
                break
            import time

            time.sleep(0.02)
        assert capacity == 12 * GiB

    def test_the_fleet_knows_its_platform(self):
        platforms = {n.name: n.platform for n in recipe_fit.fleet()}
        assert platforms["spark-02"] == "linux/amd64"
        # The Spark beside it still reports nothing, as before.
        assert platforms["spark-01"] == ""

    def test_a_model_bigger_than_the_card_will_not_run_there(self):
        node = recipe_fit.FleetNode(peer_id(), "spark-02", "linux/amd64", 12 * GiB)
        verdict = recipe_fit.memory_fit(27 * GiB, [node], 1)
        assert verdict is not None and verdict.verdict == recipe_fit.WONT_RUN
        assert "spark-02" in verdict.reason

    def test_its_runtime_is_checked_as_rootless_podman(self):
        from spark_pulse.tools.backends import DockerBackend

        probe = tools.preflight.probe_for(
            tools.node_service.node_for(PEER), services=None
        )
        report = DockerBackend().diagnose(peer_id(), "spark-02", probe.run)
        assert report.runtime == "podman"
        assert report.storage_root == profiles.WORKSTATION.store_root
        statuses = {f.check: f.status for f in report.findings}
        assert statuses[runtime_checks.GPU_ACCESS] == "ok", report.findings
        assert "broken" not in statuses.values(), report.findings

    def test_the_probe_reads_the_cards_memory_not_n_a(self):
        probe = tools.preflight.probe_for(
            tools.node_service.node_for(PEER), services=None
        )
        row = probe.run("nvidia-smi --query-gpu=index,name").stdout
        assert row == "0, NVIDIA GeForce RTX 3060, 12288, 960"


class TestChoosingTheMachine:
    def test_the_environment_makes_every_node_a_workstation(self, monkeypatch):
        monkeypatch.setenv("SPARK_PULSE_SIM_PROFILE", "workstation")
        assert profiles.profile_for(PEER) is profiles.WORKSTATION
        assert profiles.profile_for(CONTROL) is profiles.WORKSTATION

    def test_an_unknown_name_is_a_spark(self, monkeypatch):
        monkeypatch.setenv("SPARK_PULSE_SIM_PROFILE", "mainframe")
        assert profiles.profile_for(PEER) is profiles.SPARK

    def test_the_control_node_is_profiled_by_its_registered_address(self):
        profiles.use(CONTROL, profiles.WORKSTATION)
        control = Node(id="", address="", is_self=True)
        memory = node_stats.node_memory(tools.node_service.service_for(control))
        assert memory.model == node_stats.MEMORY_DISCRETE

    def test_a_connected_agent_wins_over_the_profile(self, agent_server):
        import asyncio

        from spark_pulse.agent import agent_pb2 as pb
        from spark_pulse.agent import runtime as agent_runtime
        from spark_pulse.agent.hub import AgentConnection
        from spark_pulse.agent.runtime import ControlPlaneRuntime

        profiles.use(PEER, profiles.WORKSTATION)
        loop = asyncio.new_event_loop()
        try:
            runtime = ControlPlaneRuntime(agent_server, loop)
            with agent_runtime.use(runtime):
                runtime.hub.attach(
                    AgentConnection(
                        peer_id(), facts=pb.NodeFacts(platform="linux/arm64")
                    )
                )
                assert tools.node_facts.facts_for(peer_id()).platform == "linux/arm64"
        finally:
            loop.close()
