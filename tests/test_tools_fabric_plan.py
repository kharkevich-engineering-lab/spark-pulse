"""The fabric planner against NETWORKING.md's own examples.

Every address below is the one that page writes by hand: ``192.168.177.11``
and ``.12`` on the two nodes' lowercase twins, ``178`` on the capital-P twins,
and the three-cable mesh from lines 162-252. A planner that produced anything
else would be a second scheme, and the point of the planner is that there is
only the one.
"""

from __future__ import annotations

import pytest

from spark_pulse.tools.discovery import RoCEPort, build_fabric_config
from spark_pulse.tools.fabric_plan import (
    FABRIC_MTU,
    STATUS_CONFIGURED,
    STATUS_PROPOSED,
    STATUS_REFUSED,
    STATUS_UNKNOWN,
    NodeFabric,
    plan_fabric,
    render_netplan,
)

# One cable in the outer QSFP port, as both Sparks on the bench report it.
ONE_CABLE = (
    RoCEPort("rocep1s0f0", 1, "enp1s0f0np0", False),
    RoCEPort("rocep1s0f1", 1, "enp1s0f1np1", True),
    RoCEPort("roceP2p1s0f0", 1, "enP2p1s0f0np0", False),
    RoCEPort("roceP2p1s0f1", 1, "enP2p1s0f1np1", True),
)
# Both cables: the mesh.
TWO_CABLES = tuple(RoCEPort(p.hca, p.port, p.netdev, True) for p in ONE_CABLE)


def node(
    node_id: str,
    name: str,
    ports=ONE_CABLE,
    addresses: dict[str, str] | None = None,
    mtu: int = 1500,
    control: bool = False,
    facts: bool = True,
) -> NodeFabric:
    fabric = build_fabric_config(ports, addresses or {}) if facts else None
    return NodeFabric(
        node_id,
        name,
        is_control_plane=control,
        fabric=fabric,
        mtus={p.netdev: mtu for p in ports},
    )


PAIR_FILE_11 = """\
# Written by Spark Pulse. The ConnectX-7 fabric, as spark-vllm-docker's
# NETWORKING.md lays it out: a static /24 per cable, jumbo frames, and
# no IPv6 link-local so nothing but these addresses lives on the fabric.
network:
  version: 2
  ethernets:
    enp1s0f1np1:
      dhcp4: false
      dhcp6: false
      link-local: []
      mtu: 9000
      addresses: [192.168.177.11/24]
    enP2p1s0f1np1:
      dhcp4: false
      dhcp6: false
      link-local: []
      mtu: 9000
      addresses: [192.168.178.11/24]
"""


class TestThePair:
    def test_two_unaddressed_nodes_get_networking_mds_pair(self):
        plan = plan_fabric([node("a", "spark", control=True), node("b", "spark2")])
        assert plan.mode == "direct"
        assert plan.problems == ()
        first, second = plan.nodes
        assert (first.status, second.status) == (STATUS_PROPOSED, STATUS_PROPOSED)
        assert [a.cidr for a in first.assignments] == [
            "192.168.177.11/24",
            "192.168.178.11/24",
        ]
        assert [a.cidr for a in second.assignments] == [
            "192.168.177.12/24",
            "192.168.178.12/24",
        ]
        assert all(a.mtu == FABRIC_MTU for a in first.assignments)
        # The lowercase twin is addressed first, NETWORKING.md line 37.
        assert [a.netdev for a in first.assignments] == ["enp1s0f1np1", "enP2p1s0f1np1"]

    def test_the_file_is_networking_mds_file(self):
        plan = plan_fabric([node("a", "spark"), node("b", "spark2")])
        assert plan.nodes[0].netplan == PAIR_FILE_11
        assert plan.nodes[0].to_dict()["netplan_path"] == "/etc/netplan/40-cx7.yaml"

    def test_each_assignment_names_the_peer_on_its_subnet(self):
        plan = plan_fabric([node("a", "spark"), node("b", "spark2")])
        first = plan.nodes[0].assignments
        assert first[0].peers == (("spark2", "192.168.177.12"),)
        assert first[1].peers == (("spark2", "192.168.178.12"),)

    def test_a_switch_is_a_pair_with_more_hosts(self):
        plan = plan_fabric([node(str(i), f"s{i}") for i in range(4)])
        assert plan.mode == "direct"
        assert [n.assignments[0].cidr for n in plan.nodes] == [
            f"192.168.177.{11 + i}/24" for i in range(4)
        ]
        assert len(plan.nodes[0].assignments[0].peers) == 3

    def test_a_node_addressed_but_without_jumbo_frames_is_proposed_the_mtu(self):
        addressed = {
            "enp1s0f1np1": "192.168.177.11/24",
            "enP2p1s0f1np1": "192.168.178.11/24",
        }
        plan = plan_fabric(
            [node("a", "spark", addresses=addressed), node("b", "spark2")]
        )
        first = plan.nodes[0]
        assert first.status == STATUS_PROPOSED
        assert "jumbo" in first.reasons[-1]
        assert first.assignments[0].current_cidr == "192.168.177.11/24"
        assert first.assignments[0].changes, "the MTU is the change"

    def test_reasons_carry_upstreams_own_words(self):
        plan = plan_fabric([node("a", "spark"), node("b", "spark2")])
        assert any("40-cx7.yaml" in r for r in plan.nodes[0].reasons)


class TestLeavingAValidFabricAlone:
    VALID_A = {"enp1s0f1np1": "10.10.1.1/24", "enP2p1s0f1np1": "10.10.2.1/24"}
    VALID_B = {"enp1s0f1np1": "10.10.1.2/24", "enP2p1s0f1np1": "10.10.2.2/24"}

    def test_somebody_elses_scheme_is_kept_when_every_node_is_on_it(self):
        plan = plan_fabric(
            [
                node("a", "spark", addresses=self.VALID_A, mtu=9000),
                node("b", "spark2", addresses=self.VALID_B, mtu=9000),
            ]
        )
        assert [n.status for n in plan.nodes] == [STATUS_CONFIGURED, STATUS_CONFIGURED]
        assert plan.nodes[0].assignments[0].cidr == "10.10.1.1/24"
        assert not plan.nodes[0].assignments[0].changes
        assert plan.nodes[0].assignments[0].peers == (("spark2", "10.10.1.2"),)
        assert plan.proposed == ()

    def test_a_lone_scheme_is_kept_but_said(self):
        plan = plan_fabric(
            [node("a", "spark", addresses=self.VALID_A, mtu=9000), node("b", "spark2")]
        )
        first, second = plan.nodes
        assert first.status == STATUS_CONFIGURED
        assert any("override" in r for r in first.reasons)
        assert second.status == STATUS_PROPOSED
        assert second.assignments[0].cidr == "192.168.177.12/24"
        assert second.assignments[0].peers == (), "nobody else is on 177 yet"

    def test_override_puts_every_node_on_one_scheme(self):
        plan = plan_fabric(
            [node("a", "spark", addresses=self.VALID_A, mtu=9000), node("b", "spark2")],
            override=True,
        )
        assert [n.status for n in plan.nodes] == [STATUS_PROPOSED, STATUS_PROPOSED]
        assert plan.nodes[0].assignments[0].cidr == "192.168.177.11/24"
        assert plan.nodes[0].assignments[0].current_cidr == "10.10.1.1/24"


class TestTheMesh:
    def test_three_nodes_get_networking_mds_drawing(self):
        plan = plan_fabric(
            [
                node("1", "spark1", TWO_CABLES),
                node("2", "spark2", TWO_CABLES),
                node("3", "spark3", TWO_CABLES),
            ]
        )
        assert plan.mode == "mesh"
        assert plan.problems == ()
        cidrs = {n.name: [a.cidr for a in n.assignments] for n in plan.nodes}
        # Lines 162-252, port 0 then port 1, lowercase twin then capital-P twin.
        assert cidrs["spark1"] == [
            "192.168.177.11/24",
            "192.168.178.11/24",
            "192.168.187.11/24",
            "192.168.188.11/24",
        ]
        assert cidrs["spark2"] == [
            "192.168.197.12/24",
            "192.168.198.12/24",
            "192.168.177.12/24",
            "192.168.178.12/24",
        ]
        assert cidrs["spark3"] == [
            "192.168.187.13/24",
            "192.168.188.13/24",
            "192.168.197.13/24",
            "192.168.198.13/24",
        ]

    def test_every_mesh_link_has_exactly_one_peer(self):
        plan = plan_fabric(
            [
                node("1", "spark1", TWO_CABLES),
                node("2", "spark2", TWO_CABLES),
                node("3", "spark3", TWO_CABLES),
            ]
        )
        for n in plan.nodes:
            for a in n.assignments:
                assert len(a.peers) == 1, (n.name, a.netdev)
        first = {a.netdev: a.peers for a in plan.nodes[0].assignments}
        assert first["enp1s0f0np0"] == (("spark2", "192.168.177.12"),)
        assert first["enp1s0f1np1"] == (("spark3", "192.168.187.13"),)

    def test_two_cables_on_two_nodes_is_the_dual_pair_not_the_mesh(self):
        """NVIDIA allows both cables between two Sparks; upstream would call
        four ports up a mesh and hand the pair the mesh's NCCL settings."""
        plan = plan_fabric(
            [node("1", "spark1", TWO_CABLES), node("2", "spark2", TWO_CABLES)]
        )
        assert plan.mode == "dual"
        assert plan.problems == ()
        cidrs = {n.name: [a.cidr for a in n.assignments] for n in plan.nodes}
        # One subnet pair per cable, the same on both nodes, hosts .11/.12.
        assert cidrs["spark1"] == [
            "192.168.177.11/24",
            "192.168.178.11/24",
            "192.168.187.11/24",
            "192.168.188.11/24",
        ]
        assert cidrs["spark2"] == [
            "192.168.177.12/24",
            "192.168.178.12/24",
            "192.168.187.12/24",
            "192.168.188.12/24",
        ]
        for a in plan.nodes[0].assignments:
            assert len(a.peers) == 1 and a.peers[0][0] == "spark2"
        assert any("second cable" in a.lower() for a in plan.advice)
        assert not any("port 0" in a for a in plan.advice), "no ring rule for a pair"

    def test_the_mesh_says_how_to_cable_it_and_what_it_coordinates_over(self):
        plan = plan_fabric([node(str(i), f"spark{i}", TWO_CABLES) for i in (1, 2, 3)])
        assert any("node 1 port 0 to node 2 port 1" in a for a in plan.advice)
        assert any("enP7s7" in a for a in plan.advice)
        assert plan.problems == (), "unknown link state is not a problem"

    def test_a_mesh_node_without_the_10g_link_is_named(self):
        nodes = [node(str(i), f"spark{i}", TWO_CABLES) for i in (1, 2, 3)]
        from dataclasses import replace

        nodes[1] = replace(nodes[1], wired_management_up=False)
        nodes[2] = replace(nodes[2], wired_management_up=True)
        plan = plan_fabric(nodes)
        assert any("spark2: the 10G RJ-45 port" in p for p in plan.problems)
        assert not any("spark3" in p for p in plan.problems)

    def test_a_pair_gets_the_second_cable_advice_and_no_ring_rule(self):
        plan = plan_fabric([node("a", "spark"), node("b", "spark2")])
        assert len(plan.advice) == 1 and "second cable" in plan.advice[0].lower()
        assert plan.to_dict()["advice"] == list(plan.advice)


class TestRefusals:
    def test_nodes_cabled_differently_are_refused_by_name(self):
        plan = plan_fabric([node("1", "spark1", TWO_CABLES), node("2", "spark2")])
        assert plan.mode == ""
        assert any(
            "not cabled alike" in p and "spark1: 4 up" in p for p in plan.problems
        )
        assert all(n.status == STATUS_REFUSED for n in plan.nodes)

    def test_an_odd_number_of_ports_is_refused_in_upstreams_words(self):
        three_up = tuple(
            RoCEPort(p.hca, p.port, p.netdev, p.netdev != "enP2p1s0f1np1")
            for p in ONE_CABLE
        )
        plan = plan_fabric(
            [node("1", "spark1", three_up), node("2", "spark2", three_up)]
        )
        assert plan.mode == ""
        assert any(
            "unexpected number of active CX7 interfaces (3)" in p for p in plan.problems
        )

    def test_a_node_without_agent_facts_is_unknown_and_planned_around(self):
        plan = plan_fabric([node("a", "spark"), node("b", "spark2", facts=False)])
        assert plan.nodes[1].status == STATUS_UNKNOWN
        assert "agent" in plan.nodes[1].reasons[0]
        assert plan.nodes[1].netplan == ""
        # The one node with facts is still planned as the first of a pair.
        assert plan.nodes[0].status == STATUS_PROPOSED
        assert plan.nodes[0].assignments[0].cidr == "192.168.177.11/24"

    def test_no_ports_at_all_is_refused_not_crashed(self):
        plan = plan_fabric([node("a", "spark", ()), node("b", "spark2", ())])
        assert plan.mode == ""
        assert all(n.status == STATUS_REFUSED for n in plan.nodes)


class TestSerialisation:
    def test_the_dict_the_page_reads(self):
        plan = plan_fabric([node("a", "spark"), node("b", "spark2")])
        data = plan.to_dict()
        assert data["mode"] == "direct"
        assert data["proposed"] == ["a", "b"]
        first = data["nodes"][0]
        assert set(first) == {
            "node_id",
            "name",
            "status",
            "is_control_plane",
            "assignments",
            "reasons",
            "netplan",
            "netplan_path",
        }
        assert first["assignments"][0]["peers"] == [
            {"name": "spark2", "address": "192.168.177.12"}
        ]
        assert first["assignments"][0]["changes"] is True

    def test_render_is_deterministic_and_ends_with_a_newline(self):
        plan = plan_fabric([node("a", "spark"), node("b", "spark2")])
        text = render_netplan(plan.nodes[0].assignments)
        assert text == render_netplan(list(plan.nodes[0].assignments))
        assert text.endswith("\n")


@pytest.mark.parametrize("override", [False, True])
def test_order_follows_the_registry(override):
    plan = plan_fabric([node("z", "last"), node("a", "first")], override=override)
    assert [n.name for n in plan.nodes] == ["last", "first"]
