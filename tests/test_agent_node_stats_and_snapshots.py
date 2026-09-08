"""Two things an agent answers about its machine, beyond containers.

`GetFacts` says what a node *is* — GPU count, memory size, kernel — and is
asked once, at enrolment. `GetNodeStats` says what it is *doing*: utilisation,
temperature, free memory, the processes holding it. That is what the monitoring
page needs from every node rather than from whichever machine the control plane
happens to run on.

`ListSnapshot` and `RemoveSnapshot` are the model cache. The listing carries
names and sizes and no verdict, on purpose: `hub_cache` decides
verified-vs-partial-vs-absent from the manifest it already holds, and the
previous arrangement — shipping `hub_cache.py` to each node over SSH and
running it there — was a second copy of that verifier on a machine that may not
have the interpreter to run it.

The Rust half has its own tests. What these hold is the contract between the
two: field names, which measurements may be absent, and what an empty answer
means.
"""

from __future__ import annotations

import pytest

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.mock.docker import MockDockerClient, MockDockerService

REPO = "/root/.cache/huggingface/hub/models--org--model"


@pytest.fixture
def node():
    return MockDockerService(MockDockerClient())


# ── The protocol itself ──────────────────────────────────────────────────────


class TestTheCommandsExist:
    """A command the control plane can build and an agent can answer.

    `cargo build` fails when the Rust match is not exhaustive, so a command
    added here without a handler there cannot ship. These assertions cover the
    other direction: the Python side naming a field the proto does not have
    would otherwise only fail when somebody ran it.
    """

    def test_a_stats_command_carries_no_arguments(self):
        command = pb.Command(command_id="c1", get_node_stats=pb.GetNodeStats())

        assert command.WhichOneof("op") == "get_node_stats"

    def test_a_snapshot_listing_names_a_repository_and_a_revision(self):
        command = pb.Command(
            command_id="c2",
            list_snapshot=pb.ListSnapshot(repo_path=REPO, revision="abc", deep=True),
        )

        assert command.list_snapshot.repo_path == REPO
        assert command.list_snapshot.deep is True

    def test_a_removal_with_no_revision_means_the_whole_repository(self):
        command = pb.Command(
            command_id="c3", remove_snapshot=pb.RemoveSnapshot(repo_path=REPO)
        )

        assert command.remove_snapshot.revision == ""

    def test_each_result_has_a_place_to_land(self):
        for outcome in ("stats", "snapshot", "removal"):
            result = pb.CommandResult(command_id="c")
            getattr(result, outcome).SetInParent()
            assert result.WhichOneof("outcome") == outcome


class TestAnAbsentMeasurementStaysAbsent:
    """The DGX Spark case, which the pre-flight already had to learn.

    `nvidia-smi` on a GB10 reports `[N/A]` for GPU memory because the pool is
    unified. A zero there would make the monitoring page report a full machine
    as empty, so every measurement is `optional` and absence is representable.
    """

    def test_gpu_memory_can_be_unset(self):
        gpu = pb.GpuStat(index=0, name="NVIDIA GB10")

        assert gpu.HasField("memory_total_bytes") is False
        assert gpu.HasField("utilization_percent") is False

    def test_a_zero_is_a_measurement_and_says_so(self):
        gpu = pb.GpuStat(index=0, name="NVIDIA GB10", utilization_percent=0.0)

        assert gpu.HasField("utilization_percent") is True
        assert gpu.utilization_percent == 0.0

    def test_stats_say_what_they_could_not_read(self):
        """Empty means "nothing to say", never "we did not look"."""
        stats = pb.NodeStats(unavailable=["GPUs: nvidia-smi could not be run"])

        assert list(stats.gpus) == []
        assert "nvidia-smi" in stats.unavailable[0]


# ── What a node answers ──────────────────────────────────────────────────────


class TestNodeStats:
    def test_a_node_reports_its_gpus_memory_and_disks(self, node):
        stats = node.get_node_stats()

        assert [g.name for g in stats.gpus] == ["NVIDIA GB10"]
        assert stats.memory.total_bytes > 0
        assert [d.mount for d in stats.disks] == ["/"]
        assert stats.cpu_count == 20

    def test_the_simulated_spark_reports_no_gpu_memory(self, node):
        """Because the real one does not, and simulation that answers a
        question the hardware refuses hides the branch that handles it."""
        gpu = node.get_node_stats().gpus[0]

        assert gpu.HasField("memory_total_bytes") is False
        assert gpu.utilization_percent == 12.0


class TestSnapshots:
    def test_a_listing_carries_files_and_sizes_but_no_verdict(self, node):
        node.snapshots[REPO] = {"abc": [("config.json", 12), ("model.bin", 1000)]}

        listing = node.list_snapshot(REPO, "abc")

        assert listing.present is True
        assert [f.path for f in listing.files] == ["config.json", "model.bin"]
        assert listing.bytes_present == 1012
        # No `state`, no `verified`: what the files mean is the control
        # plane's question, answered from the manifest it holds.
        assert not hasattr(listing, "state")

    def test_a_snapshot_that_is_not_there_is_absent_rather_than_an_error(self, node):
        listing = node.list_snapshot(REPO, "abc")

        assert listing.present is False
        assert list(listing.files) == []

    def test_hashes_are_asked_for_rather_than_always_computed(self, node):
        """Reading every byte of a 26 GB model is not something a presence
        check should do by default."""
        node.snapshots[REPO] = {"abc": [("model.bin", 1000)]}

        shallow = node.list_snapshot(REPO, "abc")
        deep = node.list_snapshot(REPO, "abc", deep=True)

        assert shallow.files[0].sha256 == ""
        assert deep.files[0].sha256 != ""

    def test_removing_a_revision_reports_what_it_freed(self, node):
        node.snapshots[REPO] = {"abc": [("model.bin", 4096)], "def": [("m", 8)]}

        removal = node.remove_snapshot(REPO, "abc")

        assert removal.removed is True
        assert removal.freed_bytes == 4096
        # The other revision is untouched: blobs are shared, and a delete that
        # took them would break the snapshot nobody asked to remove.
        assert node.list_snapshot(REPO, "def").present is True

    def test_removing_without_a_revision_takes_the_repository(self, node):
        node.snapshots[REPO] = {"abc": [("m", 8)], "def": [("m", 8)]}

        removal = node.remove_snapshot(REPO)

        assert removal.removed is True
        assert removal.freed_bytes == 16
        assert node.list_snapshot(REPO, "abc").present is False

    def test_removing_what_is_not_there_is_not_a_failure(self, node):
        removal = node.remove_snapshot(REPO, "abc")

        assert removal.removed is False
        assert removal.freed_bytes == 0


class TestEveryNodeAnswersTheSameWay:
    """Including the machine the control plane runs on.

    The whole point of routing through the agent: `NodeServices.control()`
    resolves to a service with these methods, exactly as a peer's does, so
    nothing has to ask "is this us?" before deciding how to read a GPU.
    """

    def test_the_control_node_service_answers_stats(self):
        from spark_pulse.tools import node_service

        stats = node_service.NodeServices().control().get_node_stats()

        assert stats.cpu_count > 0

    def test_a_peer_service_answers_the_same_methods(self):
        from spark_pulse.mock import node_service as mock_node_service

        peer = mock_node_service.service_for(mock_node_service.peer_node("10.0.0.2"))

        for method in ("get_node_stats", "list_snapshot", "remove_snapshot"):
            assert callable(getattr(peer, method))
