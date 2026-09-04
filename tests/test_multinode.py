"""Multi-node deployments, at every size this hardware has a topology for.

**Nothing here is evidence that multi-node works.** There is one DGX Spark, so
no test in this repository can show that a rendezvous forms across machines,
that NCCL picks the fabric, or that workers-first ordering avoids the
ten-minute collective timeout. What these tests assert is what simulation can
actually witness:

* what is *rendered* — the rendezvous flags and the interface pinning each
  rank is handed, and where they came from;
* what *order* things happen in — workers created first, rank zero last,
  teardown the other way round;
* what is *refused*, and whether the refusal says why;
* what is *booked* — which node each container landed on, and that a rank
  nobody could reach is recorded as an outstanding orphan rather than assumed
  gone.

The transport underneath is ``mock.node_service``: the *real*
:class:`RemoteNodeService` over a simulated SSH channel, so the docker command
building, the label filtering and the local-versus-peer branch under test are
the production ones. Only the bytes on the wire are invented — including the
unreachable case, where the simulated channel raises the same
:class:`~spark_pulse.tools.ssh.SSHError` ``OpenSSHClient`` raises on ssh's own
exit 255.

The size-one class at the end is the safety property the whole convergence
rests on: at one node nothing about this changed.
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import patch

import pytest

from spark_pulse import tools
from spark_pulse.config import config
from spark_pulse.engines import MAX_CLUSTER_NODES, EngineRegistry, reset_registry
from spark_pulse.mock import docker as mock_docker
from spark_pulse.mock import node_service as mock_node_service
from spark_pulse.tools.discovery import MESH_NCCL_ENV
from spark_pulse.tools.labels import RANK_LABEL, WORLD_SIZE_LABEL

# See test_tools_native_runtime.py: under SIMULATION_MODE the package attribute
# is the mock re-export, so hold the real module to patch its globals.
nr = importlib.import_module("spark_pulse.tools.native_runtime")

#: The control node, as the simulated registry seeds it. Simulation resolves
#: this address to this machine, so rank zero runs on the local container
#: service and every other rank goes over SSH — the real shape.
CONTROL = "192.168.1.100"

#: Peers, in the order ranks are assigned. The first is seeded; the rest this
#: module enrolls, because four nodes is the largest topology NVIDIA publishes
#: anything for and the registry has to hold them before a plan can name them.
PEERS = ["10.0.0.11", "10.0.0.12", "10.0.0.13"]

FLEET = [CONTROL, *PEERS]

#: A recipe whose parallelism can be dialled to the node count. One GPU per
#: node means the world size *is* the node count, so tp has to track it.
RECIPE = {
    "id": "multinode",
    "name": "Multi-node",
    "model": "Qwen/Qwen3-8B",
    "container": "vllm-node",
    "command": (
        "vllm serve Qwen/Qwen3-8B --port {port} "
        "--tensor-parallel-size {tensor_parallel}"
    ),
    "defaults": {"port": 8000, "tensor_parallel": 1},
    "mods": [],
    "env": {},
}

#: SGLang declares ``mesh: false``: it claims the two-node arrangement and
#: nothing above it.
SGLANG_RECIPE = {
    "id": "multinode-sglang",
    "name": "Multi-node SGLang",
    "model": "Qwen/Qwen3-8B",
    "recipe_version": "2",
    "engine": "sglang",
    "container": "",
    "command": "",
    "defaults": {"port": 30000},
    "mods": [],
    "env": {},
}

RECIPES = {r["id"]: r for r in (RECIPE, SGLANG_RECIPE)}

CATALOGUE = [{"id": "Qwen/Qwen3-8B", "source": "hf", "path": "/models/qwen3-8b"}]


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def registry(tmp_path):
    """Bundled engine specs only — no index fetching, no network."""
    reset_registry()
    with patch.object(type(config), "engine_indexes", property(lambda self: [])):
        instance = EngineRegistry(cache_dir=tmp_path / "engine-cache")
        with patch(
            "spark_pulse.tools.native_runtime.get_registry", return_value=instance
        ):
            yield instance
    reset_registry()


@pytest.fixture
def fleet(tmp_path, registry):
    """Four enrolled Sparks, a temp record file, and a clean transport.

    The peers are reached through the process-wide simulated SSH channel,
    which is what :func:`native_runtime.rank_services` resolves to on its
    own — so the tests below drive the production resolver rather than
    handing ``start`` a services callable of their own.
    """
    mock_node_service.reset()
    mock_docker.reset_mock()
    # Peers carry a different management link name from the control node, on
    # purpose: interface names are per machine, and a test where every node
    # happens to share one cannot tell a per-node lookup from a global.
    existing = {node.address: node for node in tools.node_registry.list_nodes()}
    for index, address in enumerate(PEERS, start=1):
        fields = {
            "ssh_user": "spark",
            "ethernet_interface": "enp1s0",
            "infiniband_interfaces": ("ib0", "ib1"),
        }
        node = existing.get(address)
        if node is not None:
            tools.node_registry.update_node(node.id, **fields)
            continue
        tools.node_registry.add_node(
            name=f"spark-{index + 1:02d}", address=address, **fields
        )
    with (
        patch.object(tools.deployment_records, "RECORDS_FILE", tmp_path / "deps.json"),
        patch.object(
            tools.recipes,
            "get_recipe",
            side_effect=lambda rid, *a, **kw: RECIPES.get(rid),
        ),
        patch.object(
            tools.models,
            "get_model",
            side_effect=lambda mid: next(
                (m for m in CATALOGUE if m["id"] == mid), None
            ),
        ),
    ):
        yield mock_node_service.default_ssh_client()
    mock_node_service.reset()
    mock_docker.reset_mock()


def plan_for(size: int, **overrides: Any):
    """A plan across the first ``size`` machines of the fleet."""
    return nr.plan(
        "multinode",
        nodes=FLEET[:size],
        solo=False,
        params={"tensor_parallel": size},
        deployment_id=overrides.pop("deployment_id", f"dep{size}"),
        **overrides,
    )


def containers_on(ssh, address: str) -> list[str]:
    """Container names live on one peer, per the simulated docker store."""
    return sorted(ssh.containers_on(address))


def local_containers() -> list[str]:
    """Container names live on this machine."""
    return sorted(
        c.name for c in mock_docker._get_service().client.containers.list(all=True)
    )


# ── Rendering, at every size ─────────────────────────────────────────────────


@pytest.mark.parametrize("size", [2, 3, 4])
class TestRendering:
    """What each rank is handed. Rendered, not run."""

    def test_every_rank_gets_the_same_rendezvous_and_its_own_rank_number(
        self, fleet, size
    ):
        plan = plan_for(size)

        assert plan.node_count == size
        assert [r["node_rank"] for r in plan.ranks] == list(range(size))
        for rank, entry in enumerate(plan.ranks):
            command = entry["command"]
            assert f"--nnodes {size}" in command
            assert f"--node-rank {rank}" in command
            # Every rank rendezvouses through rank zero's address, which is
            # the head's — never its own.
            assert f"--master-addr {CONTROL}" in command
            assert ("--headless" in command) is (rank > 0)

    def test_pinning_comes_from_each_node_s_own_registry_record(self, fleet, size):
        """Find-or-fail names, so they are the node's own or nothing.

        This is the wiring the deleted ``tools/network.py`` never had: the
        names are per machine, and the registry is where an operator records
        which link on *that* Spark carries the fabric.
        """
        plan = plan_for(size)
        by_address = {n.address: n for n in tools.node_registry.list_nodes()}

        for rank, entry in enumerate(plan.ranks):
            record = by_address[FLEET[rank]]
            env = entry["env"]
            assert env["NCCL_SOCKET_IFNAME"] == record.ethernet_interface
            assert env["GLOO_SOCKET_IFNAME"] == record.ethernet_interface
            if record.infiniband_interfaces:
                assert env["NCCL_IB_HCA"] == ",".join(record.infiniband_interfaces)
            # The rank's own address, not the head's.
            assert env["VLLM_HOST_IP"] == entry["host"]

        # And they really do differ per machine, or the test would pass on a
        # single value copied to every rank.
        assert len({e["env"]["NCCL_SOCKET_IFNAME"] for e in plan.ranks}) > 1

    def test_the_plan_says_multi_node_is_unproven(self, fleet, size):
        plan = plan_for(size)
        assert nr.MULTI_NODE_UNPROVEN in plan.warnings

    def test_one_container_per_rank_named_with_the_generation(self, fleet, size):
        plan = plan_for(size)

        assert [r.container.name for r in plan.rank_plans] == [
            f"spark-pulse-dep{size}-r{rank}-g1" for rank in range(size)
        ]
        for rank, rank_plan in enumerate(plan.rank_plans):
            assert rank_plan.container.labels[RANK_LABEL] == str(rank)
            assert rank_plan.container.labels[WORLD_SIZE_LABEL] == str(size)
        # Only rank zero serves the API; two ranks binding one port collide.
        assert all(not r.container.port_mappings for r in plan.rank_plans[1:])


# ── The lifecycle, at every size ─────────────────────────────────────────────


@pytest.mark.parametrize("size", [2, 3, 4])
class TestLifecycle:
    """Plan, start, list, stop, delete — the general path, N machines wide."""

    def test_each_rank_lands_on_its_own_machine(self, fleet, size):
        plan = plan_for(size)

        nr.start(plan, wait=True)

        assert local_containers() == [f"spark-pulse-dep{size}-r0-g1"]
        for rank, address in enumerate(FLEET[1:size], start=1):
            assert containers_on(fleet, address) == [
                f"spark-pulse-dep{size}-r{rank}-g1"
            ]

    def test_workers_are_launched_first_and_rank_zero_last(self, fleet, size):
        """Upstream's two phases, and its order within each.

        ``launch-cluster.sh`` creates the head container first (line 1097) and
        then each worker (1106), and only once every container exists does it
        run the serve command — workers in the background (1207-1222) and rank
        zero last (1234-1241). Rank zero is local here, so it never appears on
        the wire; what the peers show is the relative order among themselves
        and, more importantly, that no rank was launched while another was
        still being created.
        """
        plan = plan_for(size)

        nr.start(plan, wait=True)

        runs = [
            entry["host"]
            for entry in fleet.commands
            if entry["command"].startswith("docker run")
        ]
        launches = [
            entry["host"]
            for entry in fleet.commands
            if entry["command"].startswith("docker exec")
            and "/proc/1/fd/1" in entry["command"]
        ]
        assert runs == FLEET[1:size]
        assert launches == list(reversed(FLEET[1:size]))

        kinds = [
            "run" if entry["command"].startswith("docker run") else "launch"
            for entry in fleet.commands
            if entry["command"].startswith("docker run")
            or (
                entry["command"].startswith("docker exec")
                and "/proc/1/fd/1" in entry["command"]
            )
        ]
        assert kinds == ["run"] * (size - 1) + ["launch"] * (size - 1)

    def test_the_record_lists_every_rank_and_its_node(self, fleet, size):
        plan = plan_for(size)
        record = nr.start(plan, wait=True)

        assert record["status"] == "running"
        assert record["node_count"] == size
        assert [r["node"] for r in record["ranks"]] == FLEET[:size]
        assert [r["is_head"] for r in record["ranks"]] == [True] + [False] * (size - 1)

    def test_listing_reports_it_once_at_its_real_size(self, fleet, size):
        nr.start(plan_for(size), wait=True)

        listed = [d for d in nr.list_deployments() if d["id"] == f"dep{size}"]
        assert len(listed) == 1
        assert listed[0]["node_count"] == size
        assert listed[0]["status"] == "running"

    def test_stop_tears_every_rank_down_head_first(self, fleet, size):
        nr.start(plan_for(size), wait=True)
        before = len(fleet.commands)

        record = nr.stop_deployment(f"dep{size}")

        assert record["status"] == "stopped"
        assert record["orphans"] == []
        assert local_containers() == []
        for address in FLEET[1:size]:
            assert containers_on(fleet, address) == []
        stops = [
            entry["host"]
            for entry in fleet.commands[before:]
            if entry["command"].startswith("docker stop")
        ]
        # Rank zero is local and stops first, off the wire; the peers follow
        # in rank order, so the rendezvous collapses before the workers wait.
        assert stops == FLEET[1:size]

    def test_delete_drops_the_record_once_every_rank_is_confirmed_gone(
        self, fleet, size
    ):
        nr.start(plan_for(size), wait=True)

        assert nr.delete_deployment(f"dep{size}") is True
        assert nr.get_deployment(f"dep{size}") is None
        for address in FLEET[1:size]:
            assert containers_on(fleet, address) == []


# ── Refusals ─────────────────────────────────────────────────────────────────


class TestRefusals:
    """Every size that cannot work is refused at plan time, by name."""

    def test_a_size_above_the_published_topology_is_refused(self, fleet):
        tools.node_registry.add_node(name="spark-05", address="10.0.0.14")
        with pytest.raises(nr.NativeRuntimeError) as exc:
            nr.plan(
                "multinode",
                nodes=[*FLEET, "10.0.0.14"],
                solo=False,
                params={"tensor_parallel": 5},
            )
        reason = str(exc.value)
        assert str(MAX_CLUSTER_NODES) in reason
        assert "nothing above four" in reason

    def test_an_engine_that_claims_no_mesh_is_refused_above_two_nodes(self, fleet):
        """SGLang declares ``mesh: false``, and three nodes is not a pair.

        Above two the reference wants a QSFP switch or, at three, the
        switchless ring — and either way a daisy chain sustains 100G between
        each pair rather than 200G (``docs/NETWORKING.md`` line 43). The
        refusal has to say that, not just "no".
        """
        assert (
            nr.plan(
                "multinode-sglang",
                nodes=FLEET[:2],
                solo=False,
                params={"tensor_parallel": 2},
            ).node_count
            == 2
        )

        with pytest.raises(nr.NativeRuntimeError) as exc:
            nr.plan(
                "multinode-sglang",
                nodes=FLEET[:3],
                solo=False,
                params={"tensor_parallel": 3},
            )
        reason = str(exc.value)
        assert "mesh: false" in reason
        assert "100G between each pair" in reason

    def test_a_node_the_registry_has_never_seen_is_refused(self, fleet):
        with pytest.raises(nr.NativeRuntimeError) as exc:
            nr.plan(
                "multinode",
                nodes=[CONTROL, "10.9.9.9"],
                solo=False,
                params={"tensor_parallel": 2},
            )
        reason = str(exc.value)
        assert "10.9.9.9" in reason
        assert "find-or-fail" in reason

    def test_more_nodes_than_the_registry_holds_is_refused_by_count(self, fleet):
        for node in tools.node_registry.list_nodes():
            if not node.is_control_plane:
                tools.node_registry.remove_node(node.id)

        with pytest.raises(nr.NativeRuntimeError) as exc:
            nr.plan(
                "multinode",
                nodes=[CONTROL, "10.0.0.11"],
                solo=False,
                params={"tensor_parallel": 2},
            )
        assert "2 nodes were requested but the registry holds 1" in str(exc.value)

    def test_the_same_machine_twice_is_refused(self, fleet):
        with pytest.raises(nr.NativeRuntimeError) as exc:
            nr.plan(
                "multinode",
                nodes=[CONTROL, CONTROL],
                solo=False,
                params={"tensor_parallel": 2},
            )
        assert "listed twice" in str(exc.value)

    def test_parallelism_larger_than_the_topology_is_refused(self, fleet):
        with pytest.raises(nr.NativeRuntimeError) as exc:
            nr.plan(
                "multinode",
                nodes=FLEET[:2],
                solo=False,
                params={"tensor_parallel": 4},
            )
        reason = str(exc.value)
        assert "one GPU per node" in reason
        assert "need 4, have 2" in reason

    def test_parallelism_smaller_than_the_topology_is_refused(self, fleet):
        """Upstream trimmed the spare peers. Refusing says what would happen."""
        with pytest.raises(nr.NativeRuntimeError) as exc:
            nr.plan(
                "multinode",
                nodes=FLEET[:3],
                solo=False,
                params={"tensor_parallel": 2},
            )
        reason = str(exc.value)
        assert "only occupies 2" in reason
        # Upstream trimmed the spare peer silently. vLLM would not have let it:
        # above one node it requires --nnodes to divide the world size exactly.
        assert "divide the world size exactly" in reason

    def test_pipeline_parallelism_that_needs_more_machines_is_refused(self, fleet):
        with pytest.raises(nr.NativeRuntimeError) as exc:
            nr.plan(
                "multinode",
                nodes=FLEET[:2],
                solo=False,
                params={"tensor_parallel": 2},
                extra_args=["--pipeline-parallel-size", "2"],
            )
        assert "need 4, have 2" in str(exc.value)


# ── Failure semantics ────────────────────────────────────────────────────────


class TestUnreachablePeer:
    """All-or-nothing, and released on evidence rather than on inference."""

    def test_a_peer_that_is_already_gone_starts_nothing_anywhere(self, fleet):
        fleet.fail_hosts.add(PEERS[0])
        plan = plan_for(3)

        record = nr.start(plan, wait=True)

        assert record["status"] == "error"
        assert PEERS[0] in record["error_message"]
        assert local_containers() == []
        assert containers_on(fleet, PEERS[1]) == []

    def test_a_peer_lost_mid_start_leaves_an_orphan_and_no_survivors(self, fleet):
        """The rank nobody could reach is outstanding, not assumed gone.

        Ranks start highest first, so rank 2 comes up on the last peer and
        then that peer's neighbour goes away before rank 1 is created. The
        gang fails; rank 2, which is reachable, is confirmed gone; rank 1,
        which is not, is recorded as an orphan so its GPU and ports stay held.
        """
        plan = plan_for(3)

        original_exec = fleet.exec

        def _lose_the_middle_peer(host, command, *args, **kwargs):
            result = original_exec(host, command, *args, **kwargs)
            if host == PEERS[1] and command.startswith("docker run"):
                fleet.fail_hosts.add(PEERS[0])
            return result

        with patch.object(fleet, "exec", _lose_the_middle_peer):
            record = nr.start(plan, wait=True)

        assert record["status"] == "error"
        assert "rank 1 of 3" in record["error_message"]

        orphans = record["orphans"]
        assert [o["rank"] for o in orphans] == [1]
        assert orphans[0]["node"] == PEERS[0]
        assert orphans[0]["container_name"] == "spark-pulse-dep3-r1-g1"
        assert "could not be reached" in orphans[0]["reason"]

        # Rank 2 was reachable, so it was torn down and confirmed; rank 0 was
        # never created, because rank 1 failed before its turn.
        assert containers_on(fleet, PEERS[1]) == []
        assert local_containers() == []

    def test_a_deployment_with_an_orphan_is_not_deleted(self, fleet):
        plan = plan_for(3)
        nr.start(plan, wait=True)
        fleet.fail_hosts.add(PEERS[0])

        assert nr.delete_deployment("dep3") is False
        record = nr.get_deployment("dep3")
        assert record is not None
        assert [o["node"] for o in record["orphans"]] == [PEERS[0]]

    def test_an_unreachable_peer_never_reads_as_an_empty_daemon(self, fleet):
        """The property the whole orphan story rests on.

        A node that will not answer must raise, never return "no such
        container". Returning a definite negative is how a rank that is still
        holding a GPU gets confirmed gone on inference.
        """
        from spark_pulse.tools.ssh import SSHError

        service = tools.node_service.NodeServices().for_address(PEERS[0])
        fleet.fail_hosts.add(PEERS[0])

        with pytest.raises(SSHError):
            service.get_container_status("anything")
        with pytest.raises(SSHError):
            service.list_managed_containers()


# ── The fabric, per node ─────────────────────────────────────────────────────
#
# Upstream reads the *head's* interfaces and hands the same ``ETH_IF`` and
# ``IB_IF`` to every node (``launch-cluster.sh`` lines 957-974 build one
# ``get_env_flags`` from globals). That is right only while every Spark is
# cabled identically. Ours come from each node's own registry record, so these
# tests check the per-node path and the two things upstream's ``.env`` carries
# that a bare interface name does not: both RoCE twins, and the mesh settings.


def enroll_fabric(address: str, **fields) -> None:
    """Record a fabric shape on one already-enrolled node."""
    node = next(n for n in tools.node_registry.list_nodes() if n.address == address)
    tools.node_registry.update_node(node.id, **fields)


#: One cable in the outermost QSFP port, both RoCE twins named — the value
#: ``docs/NETWORKING.md`` line 38 prints verbatim.
DIRECT_FIELDS = {
    "ethernet_interface": "enp1s0f1np1",
    "infiniband_interfaces": ("rocep1s0f1", "roceP2p1s0f1"),
    "fabric_mode": "direct",
}

#: The switchless ring: all four ports up, coordination on the 10G link.
MESH_FIELDS = {
    "ethernet_interface": "enP7s7",
    "infiniband_interfaces": (
        "rocep1s0f0",
        "rocep1s0f1",
        "roceP2p1s0f0",
        "roceP2p1s0f1",
    ),
    "fabric_mode": "mesh",
}


class TestFabricEnv:
    def test_both_roce_twins_reach_nccl_ib_hca(self, fleet):
        """The twin rule: one QSFP port, two RoCE devices, name both.

        Each port gets a PCIe 5.0 x4 link, so one device carries about half
        the port's bandwidth (``docs/NETWORKING.md`` lines 15-40). Upstream
        discovers both and joins them; the registry holds both and the plan
        joins them the same way.
        """
        for address in FLEET[:2]:
            enroll_fabric(address, **DIRECT_FIELDS)

        plan = plan_for(2)

        for entry in plan.ranks:
            assert entry["env"]["NCCL_IB_HCA"] == "rocep1s0f1,roceP2p1s0f1"

    def test_a_single_cable_carries_none_of_the_mesh_settings(self, fleet):
        for address in FLEET[:2]:
            enroll_fabric(address, **DIRECT_FIELDS)

        plan = plan_for(2)

        for entry in plan.ranks:
            for key in MESH_NCCL_ENV:
                assert key not in entry["env"]

    def test_a_mesh_carries_all_three_mesh_settings(self, fleet):
        """``autodiscover.sh`` lines 186-190, and NETWORKING.md line 444.

        Two of the three are NVIDIA's own: its ring playbook publishes
        ``NCCL_IB_SUBNET_AWARE_ROUTING=1`` and ``NCCL_NET_PLUGIN=none``
        (https://build.nvidia.com/spark/nccl/three-sparks).
        ``NCCL_IB_MERGE_NICS=0`` is upstream's addition and NVIDIA sets it
        nowhere. They configure NCCL rather than the engine, so every rank
        gets them, and they only make sense where each link pair sits on its
        own subnet.
        """
        size = 3
        for address in FLEET[:size]:
            enroll_fabric(address, **MESH_FIELDS)

        plan = plan_for(size)

        for entry in plan.ranks:
            assert entry["env"]["NCCL_NET_PLUGIN"] == "none"
            assert entry["env"]["NCCL_IB_SUBNET_AWARE_ROUTING"] == "1"
            assert entry["env"]["NCCL_IB_MERGE_NICS"] == "0"
            # Coordination moves off the fabric onto the 10G link, because
            # all four CX7 ports are carrying the ring.
            assert entry["env"]["NCCL_SOCKET_IFNAME"] == "enP7s7"
            assert entry["env"]["NCCL_IB_HCA"] == ",".join(
                MESH_FIELDS["infiniband_interfaces"]
            )

    def test_a_ring_of_four_is_refused_because_nobody_documents_one(self, fleet):
        """NVIDIA's ring is exactly three, and its NCCL launcher says so.

        ``dgx-spark-playbooks/nvidia/nccl/assets/launch.sh`` refuses any other
        count outright, and the Sync cluster assistant routes four nodes to a
        switch. A four-node ring exists only as one community repository that
        needs a patched NCCL, so we do not pretend to configure one.
        """
        for address in FLEET[:4]:
            enroll_fabric(address, **MESH_FIELDS)

        with pytest.raises(nr.NativeRuntimeError) as exc:
            plan_for(4)

        reason = str(exc.value)
        assert "exactly 3 nodes" in reason
        assert "switch" in reason

    def test_four_nodes_behind_a_switch_are_a_single_cable_each(self, fleet):
        """Four nodes are NVIDIA-supported — through a switch, not a ring.

        Each node then has one cable and two ports up, so it reports
        ``direct`` and gets none of the ring's NCCL settings.
        """
        for address in FLEET[:4]:
            enroll_fabric(address, **DIRECT_FIELDS)

        plan = plan_for(4)

        assert plan.node_count == 4
        for entry in plan.ranks:
            assert entry["env"]["NCCL_IB_HCA"] == "rocep1s0f1,roceP2p1s0f1"
            for key in MESH_NCCL_ENV:
                assert key not in entry["env"]

    def test_nodes_that_disagree_about_the_cabling_are_refused(self, fleet):
        """A ring is symmetric: one node in it and one not is not a fabric."""
        enroll_fabric(FLEET[0], **MESH_FIELDS)
        enroll_fabric(FLEET[1], **DIRECT_FIELDS)
        enroll_fabric(FLEET[2], **MESH_FIELDS)

        with pytest.raises(nr.NativeRuntimeError) as exc:
            plan_for(3)

        reason = str(exc.value)
        assert "disagree about how the fabric is cabled" in reason
        assert "direct" in reason and "mesh" in reason

    def test_a_registry_that_records_no_cabling_still_plans(self, fleet):
        """``fabric_mode`` is an observation, and an absent one is not a fault.

        The fixture's peers carry interface names but no mode, which is what a
        hand-entered node looks like. It gets the pinning it asked for and no
        mesh settings, because nothing said it was a mesh.
        """
        plan = plan_for(2)

        for entry in plan.ranks:
            assert "NCCL_NET_PLUGIN" not in entry["env"]


class TestCacheDirectories:
    """Bind sources are created before the container, as upstream does.

    ``launch-cluster.sh`` runs ``mkdir -p`` for every cache directory on the
    head (line 1094) and over SSH on every worker (line 1104). Without it
    docker invents the missing source **owned by root**, which is what the
    runbook's "Troubleshoot model-copy permissions" section is about.
    """

    def test_every_peer_is_asked_to_create_its_mounts_first(self, fleet):
        plan = plan_for(2)

        nr.start(plan, wait=True)

        made = fleet.directories.get(PEERS[0]) or []
        assert made, "the peer was never asked to create its bind sources"
        assert set(made) == set(plan.rank_plans[1].container.mounts)

        indices = [
            index
            for index, entry in enumerate(fleet.commands)
            if entry["host"] == PEERS[0]
        ]
        first_mkdir = next(
            index
            for index in indices
            if fleet.commands[index]["command"].startswith("mkdir -p")
        )
        first_run = next(
            index
            for index in indices
            if fleet.commands[index]["command"].startswith("docker run")
        )
        assert first_mkdir < first_run

    def test_the_control_node_creates_its_own_too(self, fleet):
        plan = plan_for(2)

        nr.start(plan, wait=True)

        ensured = mock_docker._get_service().ensured
        assert set(plan.rank_plans[0].container.mounts) <= set(ensured)


# ── The safety property ──────────────────────────────────────────────────────


class TestSizeOneIsUntouched:
    """None of the above may reach a single-node deployment.

    A registry full of peers, an engine with mesh support and the whole
    multi-node apparatus present, and a solo plan still renders exactly what
    it rendered before any of it existed.
    """

    def test_a_solo_plan_reads_no_node_registry_at_all(self, fleet):
        with patch.object(
            tools.node_registry, "list_nodes", side_effect=AssertionError("read")
        ):
            plan = nr.plan("multinode", deployment_id="solo")

        assert plan.node_count == 1
        assert plan.solo is True
        assert plan.nodes == []
        assert plan.rank_plans[0].node == ""

    def test_a_solo_plan_pins_no_fabric_interface(self, fleet):
        plan = nr.plan("multinode", deployment_id="solo")

        env = plan.ranks[0]["env"]
        # Loopback for Gloo, which otherwise resolves the hostname; nothing
        # else, because a single node never touches the fabric.
        assert env["GLOO_SOCKET_IFNAME"] == "lo"
        assert "NCCL_SOCKET_IFNAME" not in env
        assert "NCCL_IB_HCA" not in env
        assert "MN_IF_NAME" not in env

    def test_a_solo_plan_carries_no_unproven_warning(self, fleet):
        plan = nr.plan("multinode", deployment_id="solo")
        assert plan.warnings == []

    def test_a_solo_plan_renders_the_rendezvous_flags_all_the_same(self, fleet):
        plan = nr.plan("multinode", deployment_id="solo")

        assert plan.launch_command.endswith(
            "--nnodes 1 --node-rank 0 --master-addr 127.0.0.1 --master-port 29501"
        )
        assert "--headless" not in plan.launch_command

    def test_a_solo_deploy_touches_no_peer(self, fleet):
        plan = nr.plan("multinode", deployment_id="solo")

        record = nr.start(plan, wait=True)

        assert record["status"] == "running"
        assert local_containers() == ["spark-pulse-solo-r0-g1"]
        assert fleet.hosts_seen() == []
