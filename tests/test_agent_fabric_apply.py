"""Writing the fabric onto a node, through the simulated node's netplan and nmcli."""

from __future__ import annotations

import pytest

from spark_pulse.agent.bootstrap import NodeAccess, control_plane_keypair
from spark_pulse.agent.fabric_apply import apply_node_plan
from spark_pulse.tools.discovery import RoCEPort, build_fabric_config
from spark_pulse.tools.fabric_plan import NETPLAN_PATH, NodeFabric, plan_fabric
from tests.agent_bootstrap_fixtures import (
    SUDO_PASSWORD,
    USER,
    make_node,
    password_prompt,
)

pytestmark = pytest.mark.asyncio

ONE_CABLE = (
    RoCEPort("rocep1s0f0", 1, "enp1s0f0np0", False),
    RoCEPort("rocep1s0f1", 1, "enp1s0f1np1", True),
    RoCEPort("roceP2p1s0f0", 1, "enP2p1s0f0np0", False),
    RoCEPort("roceP2p1s0f1", 1, "enP2p1s0f1np1", True),
)


def the_plan():
    """Two Sparks with one cable and nothing on it; the first node's plan."""
    plan = plan_fabric(
        [
            NodeFabric(
                "a",
                "spark",
                fabric=build_fabric_config(ONE_CABLE, {}),
                mtus={"enp1s0f1np1": 1500, "enP2p1s0f1np1": 1500},
            ),
            NodeFabric(
                "b",
                "spark2",
                fabric=build_fabric_config(ONE_CABLE, {}),
                mtus={"enp1s0f1np1": 1500, "enP2p1s0f1np1": 1500},
            ),
        ]
    )
    return plan.nodes[0]


@pytest.fixture
def machine(agent_server, agent_fleet, tmp_path):
    """A node the control plane's key already opens, with NM's DHCP profiles."""
    node = make_node(tmp_path, sudo_password=SUDO_PASSWORD)
    node.users[USER].authorized_keys.add(
        control_plane_keypair(agent_server).public_openssh
    )
    node.nm_profiles = {
        "Wired connection 1": {
            "device": "enp1s0f1np1",
            "autoconnect": True,
            "active": True,
        },
        "Wired connection 2": {
            "device": "enP2p1s0f1np1",
            "autoconnect": True,
            "active": True,
        },
        "Unsecured network": {"device": "wlP9s9", "autoconnect": True, "active": True},
    }
    node.fabric_peers = {"192.168.177.12", "192.168.178.12"}
    agent_fleet.add(node)
    return node


def access(node) -> NodeAccess:
    return NodeAccess(host=node.host, username=USER)


async def test_the_file_is_written_applied_and_read_back(
    agent_server, agent_fleet, machine
):
    report = await apply_node_plan(
        agent_server,
        access(machine),
        the_plan(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(SUDO_PASSWORD),
    )
    assert report.errors == []
    assert report.applied and report.verified

    # The file, root-owned at 0600, byte for byte what the plan rendered.
    written = machine.read(NETPLAN_PATH)
    assert written.decode() == the_plan().netplan
    assert machine.path(NETPLAN_PATH, "root").stat().st_mode & 0o777 == 0o600

    # The ports carry the addresses and jumbo frames.
    assert machine.addresses == {
        "enp1s0f1np1": "192.168.177.11/24",
        "enP2p1s0f1np1": "192.168.178.11/24",
    }
    assert machine.mtus == {"enp1s0f1np1": 9000, "enP2p1s0f1np1": 9000}
    assert report.readback["enp1s0f1np1"] == {
        "cidr": "192.168.177.11/24",
        "address_ok": True,
        "mtu": "9000",
        "mtu_ok": True,
    }
    # Both peers answered over the right port.
    assert [(p["netdev"], p["address"], p["reachable"]) for p in report.pings] == [
        ("enp1s0f1np1", "192.168.177.12", True),
        ("enP2p1s0f1np1", "192.168.178.12", True),
    ]

    # NetworkManager's DHCP profiles on those two ports are quiet; the Wi-Fi
    # one — the management link — was not touched.
    assert machine.nm_profiles["Wired connection 1"] == {
        "device": "enp1s0f1np1",
        "autoconnect": False,
        "active": False,
    }
    assert machine.nm_profiles["Wired connection 2"]["active"] is False
    assert machine.nm_profiles["Unsecured network"]["active"] is True
    assert any("quieted" in s and "Wired connection 1" in s for s in report.steps)

    # Every privileged call carries its reason.
    whys = [c["why"] for c in report.privileged_calls]
    assert f"write {NETPLAN_PATH}" in whys
    assert "bring the fabric addresses up" in whys
    # And the sudo password is nowhere in what came back.
    assert SUDO_PASSWORD not in str(report.to_dict())


async def test_a_machine_without_network_manager_has_nothing_to_quiet(
    agent_server, agent_fleet, machine
):
    machine.nm_profiles = None
    report = await apply_node_plan(
        agent_server,
        access(machine),
        the_plan(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(SUDO_PASSWORD),
    )
    assert report.verified
    assert any("not managing" in s for s in report.steps)


async def test_a_file_netplan_rejects_is_never_applied(
    agent_server, agent_fleet, machine
):
    machine.netplan_error = "Error in network definition: unknown key 'addreses'"
    report = await apply_node_plan(
        agent_server,
        access(machine),
        the_plan(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(SUDO_PASSWORD),
    )
    assert not report.applied
    assert any(
        "netplan rejected the file" in e and "addreses" in e for e in report.errors
    )
    assert machine.addresses == {}, "nothing was brought up"
    assert (
        machine.nm_profiles["Wired connection 1"]["active"] is True
    ), "and nothing was taken down"


async def test_a_peer_that_does_not_answer_names_the_link(
    agent_server, agent_fleet, machine
):
    machine.fabric_peers = {"192.168.177.12"}  # the second cable goes elsewhere
    report = await apply_node_plan(
        agent_server,
        access(machine),
        the_plan(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(SUDO_PASSWORD),
    )
    assert report.applied
    assert not report.verified
    assert any(
        "192.168.178.12" in e and "enP2p1s0f1np1" in e and "cable" in e
        for e in report.errors
    )
    assert [p["reachable"] for p in report.pings] == [True, False]


async def test_without_sudo_nothing_is_written(agent_server, agent_fleet, machine):
    report = await apply_node_plan(
        agent_server,
        access(machine),
        the_plan(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(None),
    )
    assert not report.applied
    assert any("needs root" in e for e in report.errors)
    assert not machine.exists(NETPLAN_PATH)


async def test_a_node_the_key_does_not_open_is_reported_not_raised(
    agent_server, agent_fleet, tmp_path
):
    node = agent_fleet.add(make_node(tmp_path, password=None))  # no key authorised
    report = await apply_node_plan(
        agent_server, access(node), the_plan(), connector=agent_fleet
    )
    assert not report.applied
    assert any("could not log in" in e for e in report.errors)


async def test_a_plan_with_nothing_to_write_is_refused_before_logging_in(
    agent_server, agent_fleet, machine
):
    from spark_pulse.tools.fabric_plan import NodePlan

    report = await apply_node_plan(
        agent_server,
        access(machine),
        NodePlan("a", "spark", "unknown"),
        connector=agent_fleet,
    )
    assert report.errors == ["the plan has nothing to write for this node"]
    assert machine.commands == []


async def test_an_operator_supplied_file_is_written_verbatim(
    agent_server, agent_fleet, machine
):
    """The expert path: a hand-edited file is applied as-is, and verification
    confirms only that the plan's ports came up — not the plan's addresses."""
    custom = (
        "network:\n  version: 2\n  ethernets:\n"
        "    enp1s0f1np1:\n      addresses: [10.9.0.1/24]\n      mtu: 9000\n"
        "    enP2p1s0f1np1:\n      addresses: [10.9.1.1/24]\n      mtu: 9000\n"
    )
    # The plan's peers are 192.168.177/178.12; the sim can reach neither, so a
    # normal apply would fail verification. The override must not ping them.
    machine.fabric_peers = set()
    report = await apply_node_plan(
        agent_server,
        access(machine),
        the_plan(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(SUDO_PASSWORD),
        override_netplan=custom,
    )
    assert report.errors == []
    assert report.applied and report.verified
    assert report.pings == [], "the plan's peers are not pinged for a supplied file"
    written = machine.read(NETPLAN_PATH).decode()
    assert written == custom
    assert machine.addresses == {
        "enp1s0f1np1": "10.9.0.1/24",
        "enP2p1s0f1np1": "10.9.1.1/24",
    }
    assert report.readback["enp1s0f1np1"]["cidr"] == "10.9.0.1/24"
    assert any("operator-supplied" in step for step in report.steps)


async def test_a_supplied_file_that_leaves_a_port_down_is_not_verified(
    agent_server, agent_fleet, machine
):
    custom = (
        "network:\n  version: 2\n  ethernets:\n"
        "    enp1s0f1np1:\n      addresses: [10.9.0.1/24]\n      mtu: 9000\n"
    )  # enP2p1s0f1np1 left out — it stays down
    report = await apply_node_plan(
        agent_server,
        access(machine),
        the_plan(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(SUDO_PASSWORD),
        override_netplan=custom,
    )
    assert report.applied
    assert not report.verified
    assert any("enP2p1s0f1np1 came up with no address" in e for e in report.errors)
