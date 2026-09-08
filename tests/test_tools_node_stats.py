"""What every node is doing, asked of every node the same way.

The monitoring page ran `nvidia-smi`, `free` and `df` in the control plane's
own process and showed the answers. On one machine that reads as "the
cluster". On four it is one Spark out of four, and nothing on the page says
which — there was no node parameter anywhere in the chain to say it with.

What these hold is the part that makes the new answer trustworthy rather than
merely wider:

* a node that cannot be asked is a row that says so, not a row that is missing
  — a missing row and an idle machine look identical on a page;
* an absent measurement stays absent, because a GB10 reports `[N/A]` for GPU
  memory and a zero there draws an empty bar for a full machine;
* whether a GPU process is *ours* is decided from the container the node
  reports and the containers this control plane started — each half asked of
  the side that has it.
"""

from __future__ import annotations

import pytest

from spark_pulse import tools
from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.tools import node_stats
from spark_pulse.tools.labels import DEPLOYMENT_LABEL


class FakeService:
    """One node's answers, with the two failure modes a real one has."""

    def __init__(self, stats=None, containers=None, error=None):
        self._stats = stats if stats is not None else pb.NodeStats()
        self._containers = containers or []
        self._error = error
        self.stopped: list[str] = []
        self.terminated: list[tuple[int, bool]] = []

    def get_node_stats(self):
        if self._error:
            raise self._error
        return self._stats

    def list_managed_containers(self, labels=None):
        return self._containers

    def stop_container(self, name, timeout=30):
        self.stopped.append(name)
        return True

    def terminate_process(self, pid, force=False):
        self.terminated.append((int(pid), bool(force)))
        return pb.ProcessTermination(terminated=True)


class FakeContainer:
    def __init__(self, id_: str, name: str, deployment: str = ""):
        self.id = id_
        self.name = name
        self.labels = {DEPLOYMENT_LABEL: deployment} if deployment else {}


def spark_stats() -> pb.NodeStats:
    """A GB10's answer: no per-GPU memory, because the pool is unified."""
    stats = pb.NodeStats(
        cpu_count=20,
        memory=pb.MemoryStat(
            total_bytes=130 * node_stats.MIB * 1024,
            used_bytes=26 * node_stats.MIB * 1024,
            available_bytes=104 * node_stats.MIB * 1024,
        ),
    )
    gpu = stats.gpus.add()
    gpu.index = 0
    gpu.name = "NVIDIA GB10"
    gpu.uuid = "GPU-abc"
    gpu.utilization_percent = 12.0
    disk = stats.disks.add()
    disk.mount = "/"
    disk.total_bytes = 1000
    disk.used_bytes = 250
    disk.free_bytes = 750
    return stats


def services_for(service):
    """A resolver that answers with ``service`` for any node."""
    return lambda _node: service


# ── One node ─────────────────────────────────────────────────────────────────


class TestOneNode:
    def test_an_absent_gpu_measurement_stays_absent(self):
        block = node_stats.for_node(
            tools.node_service.control_node(), services_for(FakeService(spark_stats()))
        )

        gpu = block["gpu"][0]
        assert gpu["memory_supported"] is False
        assert gpu["memory_total"] == 0
        assert gpu["temperature"] is None, "no reading is not a zero reading"
        assert gpu["utilization"] == 12.0

    def test_a_gpu_that_does_report_memory_reports_it_in_megabytes(self):
        stats = spark_stats()
        stats.gpus[0].memory_total_bytes = 81920 * node_stats.MIB
        stats.gpus[0].memory_used_bytes = 1024 * node_stats.MIB

        block = node_stats.for_node(
            tools.node_service.control_node(), services_for(FakeService(stats))
        )

        assert block["gpu"][0]["memory_supported"] is True
        assert block["gpu"][0]["memory_total"] == 81920
        assert block["gpu"][0]["memory_used"] == 1024

    def test_host_memory_is_megabytes_and_disks_stay_bytes(self):
        """`free -m` and `df -B1`, which is what the page already reads."""
        block = node_stats.for_node(
            tools.node_service.control_node(), services_for(FakeService(spark_stats()))
        )

        assert block["cpu"]["total"] == 130 * 1024
        assert block["disk"][0]["total"] == 1000
        assert block["disk"][0]["usage_percent"] == 25.0

    def test_a_node_that_cannot_be_asked_is_a_row_that_says_so(self):
        def _refuse(_node):
            raise RuntimeError("10.0.0.2 has no enrolled agent")

        block = node_stats.for_node(tools.node_service.peer_node("10.0.0.2"), _refuse)

        assert block["reachable"] is False
        assert "no enrolled agent" in block["error"]
        assert block["gpu"] == [] and block["processes"] == []

    def test_what_the_node_could_not_read_is_carried_through(self):
        """A node with no driver still answers, and says why it has no GPUs."""
        stats = pb.NodeStats(unavailable=["GPUs: nvidia-smi could not be run"])

        block = node_stats.for_node(
            tools.node_service.control_node(), services_for(FakeService(stats))
        )

        assert block["reachable"] is True
        assert "nvidia-smi" in block["unavailable"][0]


class TestWhoHoldsTheGpu:
    def _with_process(self, container_id: str = "abc123abc123"):
        stats = spark_stats()
        process = stats.processes.add()
        process.pid = 4242
        process.name = "VLLM::EngineCore"
        process.used_memory_bytes = 900 * node_stats.MIB
        process.container_id = container_id
        return stats

    def test_a_process_in_a_container_we_started_names_its_deployment(self):
        service = FakeService(
            self._with_process(),
            [FakeContainer("abc123abc123def", "spark-pulse-d1-r0-g1", "d1")],
        )

        block = node_stats.for_node(
            tools.node_service.control_node(), services_for(service)
        )

        process = block["processes"][0]
        assert process["is_tracked"] is True
        assert process["deployment"] == "d1"
        assert process["container_name"] == "spark-pulse-d1-r0-g1"
        assert process["used_memory"] == 900

    def test_a_process_in_a_container_we_did_not_start_is_untracked(self):
        """Not ours is a real answer. The old check walked /proc from a pid
        the records no longer carry, so *everything* came back untracked."""
        service = FakeService(self._with_process("999999999999"), [])

        block = node_stats.for_node(
            tools.node_service.control_node(), services_for(service)
        )

        assert block["processes"][0]["is_tracked"] is False
        assert block["processes"][0]["deployment"] == ""

    def test_a_node_that_will_not_list_containers_still_reports_its_stats(self):
        """Losing attribution is not a reason to lose the GPU panel."""

        class NoListing(FakeService):
            def list_managed_containers(self, labels=None):
                raise RuntimeError("daemon is down")

        block = node_stats.for_node(
            tools.node_service.control_node(),
            services_for(NoListing(self._with_process())),
        )

        assert block["gpu"][0]["name"] == "NVIDIA GB10"
        assert block["processes"][0]["is_tracked"] is False


# ── Every node ───────────────────────────────────────────────────────────────


class TestTheCluster:
    def test_every_registered_node_is_asked(self):
        asked = []

        def _resolve(node):
            asked.append(node.label)
            return FakeService(spark_stats())

        answer = node_stats.collect(_resolve)

        assert [n["name"] for n in answer["nodes"]] == ["spark-01", "spark-02"]
        assert len(asked) == 2

    def test_the_control_node_is_reached_as_itself_not_as_a_peer(self):
        """Its registry address is a LAN address like any other, and asking
        it as a peer is how a control plane ends up querying somebody else."""
        seen = []

        def _resolve(node):
            seen.append(node.is_self)
            return FakeService(spark_stats())

        node_stats.collect(_resolve)

        assert seen.count(True) == 1

    def test_the_control_nodes_block_is_repeated_at_the_top_level(self):
        """Every reader written before this existed reads those keys."""
        answer = node_stats.collect(services_for(FakeService(spark_stats())))
        control = next(n for n in answer["nodes"] if n["is_control_plane"])

        assert answer["gpu"] == control["gpu"]
        assert answer["cpu"] == control["cpu"]

    def test_one_unreachable_node_does_not_take_the_others_with_it(self):
        def _resolve(node):
            if node.label == "10.0.0.11":
                raise RuntimeError("unreachable")
            return FakeService(spark_stats())

        answer = node_stats.collect(_resolve)

        assert [n["reachable"] for n in answer["nodes"]] == [True, False]
        assert answer["gpu"], "the control node's panel went with the peer's"

    def test_an_unreadable_registry_still_reports_this_machine(self, monkeypatch):
        """A registry we cannot read is not a machine that does not exist."""
        monkeypatch.setattr(
            tools.node_registry,
            "list_nodes",
            lambda: (_ for _ in ()).throw(RuntimeError("no registry")),
        )

        answer = node_stats.collect(services_for(FakeService(spark_stats())))

        assert len(answer["nodes"]) == 1
        assert answer["nodes"][0]["is_control_plane"] is True


# ── Ending a process ─────────────────────────────────────────────────────────


class TestTerminate:
    def _service_with(self, container_id="abc123abc123", containers=None):
        stats = spark_stats()
        process = stats.processes.add()
        process.pid = 4242
        process.name = "python3"
        process.container_id = container_id
        return FakeService(stats, containers or [])

    def test_a_process_in_our_own_container_is_ended_by_stopping_it(self):
        """Killing the process inside leaves the container holding its ports,
        and whatever supervises it starts the process again."""
        service = self._service_with(
            containers=[FakeContainer("abc123abc123def", "spark-pulse-d1-r0-g1", "d1")]
        )

        result = node_stats.terminate(4242, "10.0.0.11", services=services_for(service))

        assert result["killed"] is True
        assert service.stopped == ["spark-pulse-d1-r0-g1"]
        assert service.terminated == []

    def test_a_process_nothing_claims_is_signalled_directly(self):
        service = self._service_with(container_id="")

        result = node_stats.terminate(4242, "10.0.0.11", services=services_for(service))

        assert result["killed"] is True
        assert service.terminated == [(4242, False)]
        assert service.stopped == []

    def test_force_is_carried_to_the_node(self):
        service = self._service_with(container_id="")

        node_stats.terminate(
            4242, "10.0.0.11", force=True, services=services_for(service)
        )

        assert service.terminated == [(4242, True)]

    def test_a_node_that_cannot_be_reached_is_reported_rather_than_raised(self):
        def _refuse(_node):
            raise RuntimeError("10.0.0.11 has no enrolled agent")

        result = node_stats.terminate(4242, "10.0.0.11", services=_refuse)

        assert result["killed"] is False
        assert "no enrolled agent" in result["error"]

    def test_a_node_that_refuses_the_signal_says_why(self):
        class Refusing(FakeService):
            def terminate_process(self, pid, force=False):
                return pb.ProcessTermination(terminated=False, detail="no such process")

        service = Refusing(spark_stats())

        result = node_stats.terminate(9999, "", services=services_for(service))

        assert result["killed"] is False
        assert result["error"] == "no such process"


@pytest.mark.parametrize("address", ["", "localhost", "127.0.0.1"])
def test_the_control_node_is_the_default_target(address):
    """An empty node is this machine, as it is everywhere else in the system."""
    seen = []

    def _resolve(node):
        seen.append(node.is_self)
        return FakeService(spark_stats())

    node_stats.terminate(4242, address, services=_resolve)

    assert seen == [True]
