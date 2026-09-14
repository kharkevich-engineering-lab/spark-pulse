"""``/api/fabric``: the cluster's ConnectX ports as the agents report them."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.agent import fabric_apply
from spark_pulse.agent import runtime as agent_runtime
from spark_pulse.agent.hub import AgentConnection
from spark_pulse.agent.runtime import ControlPlaneRuntime
from spark_pulse.app import create_app
from spark_pulse.mock import node_registry as mock_registry

pytestmark = pytest.mark.asyncio

PEER_ID = "9f3c1a2b4d5e6f708192a3b4c5d6e7f8"
CONTROL_ID = "c0ntr01plane00000000000000000001"


def facts(
    addresses: dict[str, str] | None = None,
    mtu: int = 1500,
    cabled: bool = True,
    both_cables: bool = False,
    wired_up: bool | None = None,
) -> pb.NodeFacts:
    addresses = addresses or {}

    def interface(name: str):
        ip, _, prefix = addresses.get(name, "").partition("/")
        return pb.NetworkInterface(
            name=name,
            ip=ip,
            prefix_length=int(prefix or 0),
            mtu=mtu,
            is_up=True,
            type="ethernet",
        )

    interfaces = [
        interface("enp1s0f0np0"),
        interface("enp1s0f1np1"),
        interface("enP2p1s0f0np0"),
        interface("enP2p1s0f1np1"),
        pb.NetworkInterface(
            name="wlP9s9",
            ip="10.0.0.11",
            prefix_length=22,
            mtu=1500,
            is_up=True,
            type="other",
        ),
    ]
    if wired_up is not None:
        interfaces.append(
            pb.NetworkInterface(
                name="enP7s7", mtu=1500, is_up=wired_up, type="ethernet"
            )
        )
    return pb.NodeFacts(
        hostname="spark",
        interfaces=interfaces,
        roce_links=[
            pb.RoceLink(hca="rocep1s0f0", netdev="enp1s0f0np0", is_up=both_cables),
            pb.RoceLink(hca="rocep1s0f1", netdev="enp1s0f1np1", is_up=cabled),
            pb.RoceLink(hca="roceP2p1s0f0", netdev="enP2p1s0f0np0", is_up=both_cables),
            pb.RoceLink(hca="roceP2p1s0f1", netdev="enP2p1s0f1np1", is_up=cabled),
        ],
    )


@pytest.fixture(autouse=True)
def clean_registry():
    mock_registry.reset()
    yield
    mock_registry.reset()


@pytest.fixture
async def running(agent_server):
    """A runtime whose hub holds both seeded nodes' agents, with facts."""
    runtime = ControlPlaneRuntime(agent_server, asyncio.get_running_loop())
    with agent_runtime.use(runtime):
        yield runtime


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def connect(runtime, node_id: str, node_facts: pb.NodeFacts) -> None:
    runtime.hub.attach(AgentConnection(node_id, facts=node_facts))


class TestRead:
    async def test_without_a_transport_every_node_is_unknown(self, client):
        body = (await client.get("/api/fabric")).json()
        assert body["transport"] is False
        assert all(not n["reported"] for n in body["nodes"])
        assert all(n["status"] == "unknown" for n in body["plan"]["nodes"])
        assert body["plan"]["proposed"] == []

    async def test_two_cabled_unaddressed_nodes_are_proposed_the_pair(
        self, client, running
    ):
        connect(running, PEER_ID, facts())
        # The control node's agent is keyed by the runtime's own id, which is
        # empty without a local agent; the peer alone is what is reported.
        body = (await client.get("/api/fabric")).json()
        assert body["transport"] is True
        peer = next(n for n in body["nodes"] if n["node_id"] == PEER_ID)
        assert peer["reported"] is True
        assert peer["mode"] == "direct"
        assert peer["ib_hca"] == "rocep1s0f1,roceP2p1s0f1"
        assert [p["netdev"] for p in peer["ports"] if p["is_up"]] == [
            "enp1s0f1np1",
            "enP2p1s0f1np1",
        ]
        assert peer[
            "errors"
        ], "no address on the cabled twin is an error in upstream's words"
        planned = next(n for n in body["plan"]["nodes"] if n["node_id"] == PEER_ID)
        assert planned["status"] == "proposed"
        assert planned["assignments"][0]["cidr"].startswith("192.168.177.")
        assert "40-cx7.yaml" in planned["netplan_path"]

    async def test_a_configured_node_is_reported_as_such(self, client, running):
        connect(
            running,
            PEER_ID,
            facts(
                {
                    "enp1s0f1np1": "192.168.177.12/24",
                    "enP2p1s0f1np1": "192.168.178.12/24",
                },
                mtu=9000,
            ),
        )
        body = (await client.get("/api/fabric")).json()
        planned = next(n for n in body["plan"]["nodes"] if n["node_id"] == PEER_ID)
        assert planned["status"] == "configured"
        assert (await client.get("/api/fabric?override=true")).json()["plan"][
            "proposed"
        ] == [PEER_ID]


class TestApply:
    async def test_without_a_transport_it_is_a_503(self, client):
        assert (await client.post("/api/fabric/apply", json={})).status_code == 503

    async def test_nothing_proposed_is_a_400_that_says_why(self, client, running):
        connect(running, PEER_ID, facts(cabled=False))
        response = await client.post("/api/fabric/apply", json={})
        assert response.status_code == 400
        assert "nothing to apply" in response.json()["detail"]

    async def test_the_proposed_nodes_are_applied_with_the_registrys_ssh_user(
        self, client, running, monkeypatch
    ):
        connect(running, PEER_ID, facts())
        seen = {}

        async def fake_apply(
            server,
            access,
            plan,
            *,
            connector=None,
            sudo_password_prompt=None,
            override_netplan=None,
        ):
            seen["access"] = access
            seen["plan"] = plan
            seen["sudo"] = (
                await sudo_password_prompt("?") if sudo_password_prompt else None
            )
            report = fabric_apply.FabricApplyReport(
                node_id=plan.node_id, name=plan.name, applied=True, verified=True
            )
            report.note("done")
            return report

        monkeypatch.setattr(fabric_apply, "apply_node_plan", fake_apply)
        response = await client.post(
            "/api/fabric/apply", json={"sudo_password": "s3cret"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mode"] == "direct"
        assert [r["node_id"] for r in body["reports"]] == [PEER_ID]
        assert body["reports"][0]["verified"] is True
        assert seen["access"].host == "10.0.0.11"
        assert seen["access"].username == "spark", "the user the install recorded"
        assert seen["plan"].node_id == PEER_ID
        assert seen["sudo"] == "s3cret"
        assert "s3cret" not in response.text

    async def test_node_ids_narrow_the_apply(self, client, running, monkeypatch):
        connect(running, PEER_ID, facts())
        calls = []

        async def fake_apply(server, access, plan, **_):
            calls.append(plan.node_id)
            return fabric_apply.FabricApplyReport(node_id=plan.node_id, name=plan.name)

        monkeypatch.setattr(fabric_apply, "apply_node_plan", fake_apply)
        response = await client.post(
            "/api/fabric/apply", json={"node_ids": ["not-a-node"]}
        )
        assert response.status_code == 400
        response = await client.post("/api/fabric/apply", json={"node_ids": [PEER_ID]})
        assert response.status_code == 200 and calls == [PEER_ID]

    async def test_a_node_without_an_ssh_user_is_reported_not_attempted(
        self, client, running, monkeypatch
    ):
        connect(running, PEER_ID, facts())
        mock_registry.update_node(PEER_ID, ssh_user="")

        async def never(*_, **__):  # pragma: no cover
            raise AssertionError("no login should be attempted")

        monkeypatch.setattr(fabric_apply, "apply_node_plan", never)
        body = (await client.post("/api/fabric/apply", json={})).json()
        assert body["reports"][0]["applied"] is False
        assert "no SSH user" in body["reports"][0]["errors"][0]


class TestShapesAndPinning:
    async def test_both_cables_on_the_seeded_pair_is_the_dual_shape(
        self, client, running
    ):
        """The registry holds two nodes, so four ports up is both cables."""
        connect(running, PEER_ID, facts(both_cables=True))
        body = (await client.get("/api/fabric")).json()
        peer = next(n for n in body["nodes"] if n["node_id"] == PEER_ID)
        assert peer["mode"] == "dual"
        assert peer["ib_hca"] == "rocep1s0f0,rocep1s0f1,roceP2p1s0f0,roceP2p1s0f1"
        assert peer["wired_management_up"] is None
        assert peer["pinned"] == {
            "ethernet_interface": "eth0",
            "infiniband_interfaces": ["ib0", "ib1"],
            "fabric_mode": "",
        }

    async def test_the_wired_management_link_is_reported(self, client, running):
        connect(running, PEER_ID, facts(wired_up=False))
        body = (await client.get("/api/fabric")).json()
        peer = next(n for n in body["nodes"] if n["node_id"] == PEER_ID)
        assert peer["wired_management_up"] is False

    async def test_a_verified_apply_pins_the_registry_record(
        self, client, running, monkeypatch
    ):
        connect(running, PEER_ID, facts())

        async def fake_apply(server, access, plan, **_):
            return fabric_apply.FabricApplyReport(
                node_id=plan.node_id, name=plan.name, applied=True, verified=True
            )

        monkeypatch.setattr(fabric_apply, "apply_node_plan", fake_apply)
        body = (await client.post("/api/fabric/apply", json={})).json()
        assert body["reports"][0]["pinned"] == {
            "ethernet_interface": "enp1s0f1np1",
            "infiniband_interfaces": ["rocep1s0f1", "roceP2p1s0f1"],
            "fabric_mode": "direct",
        }
        node = mock_registry.get_node(PEER_ID)
        assert node.ethernet_interface == "enp1s0f1np1"
        assert list(node.infiniband_interfaces) == ["rocep1s0f1", "roceP2p1s0f1"]
        assert node.fabric_mode == "direct"

    async def test_an_unverified_apply_pins_nothing(self, client, running, monkeypatch):
        connect(running, PEER_ID, facts())

        async def fake_apply(server, access, plan, **_):
            return fabric_apply.FabricApplyReport(
                node_id=plan.node_id, name=plan.name, applied=True, verified=False
            )

        monkeypatch.setattr(fabric_apply, "apply_node_plan", fake_apply)
        body = (await client.post("/api/fabric/apply", json={})).json()
        assert "pinned" not in body["reports"][0]
        assert mock_registry.get_node(PEER_ID).fabric_mode == ""

    async def test_a_configured_node_is_pinned_without_logging_in(
        self, client, running, monkeypatch
    ):
        connect(
            running,
            PEER_ID,
            facts(
                {
                    "enp1s0f1np1": "192.168.177.12/24",
                    "enP2p1s0f1np1": "192.168.178.12/24",
                },
                mtu=9000,
            ),
        )

        async def never(*_, **__):  # pragma: no cover
            raise AssertionError("a configured node needs no login")

        monkeypatch.setattr(fabric_apply, "apply_node_plan", never)
        response = await client.post("/api/fabric/apply", json={})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reports"] == []
        assert body["pinned"][PEER_ID]["fabric_mode"] == "direct"
        assert mock_registry.get_node(PEER_ID).ethernet_interface == "enp1s0f1np1"


class TestExpertConfig:
    async def test_a_supplied_file_is_passed_through_to_the_apply(
        self, client, running, monkeypatch
    ):
        connect(running, PEER_ID, facts())
        seen = {}

        async def fake_apply(
            server,
            access,
            plan,
            *,
            connector=None,
            sudo_password_prompt=None,
            override_netplan=None,
        ):
            seen["override"] = override_netplan
            return fabric_apply.FabricApplyReport(
                node_id=plan.node_id, name=plan.name, applied=True, verified=True
            )

        monkeypatch.setattr(fabric_apply, "apply_node_plan", fake_apply)
        custom = "network:\n  version: 2\n  ethernets:\n    enp1s0f1np1:\n      addresses: [10.9.0.1/24]\n"
        response = await client.post(
            "/api/fabric/apply", json={"files": {PEER_ID: custom}}
        )
        assert response.status_code == 200, response.text
        assert seen["override"] == custom

    async def test_a_configured_node_with_a_supplied_file_becomes_a_target(
        self, client, running, monkeypatch
    ):
        # Addressed and jumbo already, so the plan leaves it configured; a file
        # makes it a target without turning on override for everyone.
        connect(
            running,
            PEER_ID,
            facts(
                {
                    "enp1s0f1np1": "192.168.177.12/24",
                    "enP2p1s0f1np1": "192.168.178.12/24",
                },
                mtu=9000,
            ),
        )
        applied = []

        async def fake_apply(
            server,
            access,
            plan,
            *,
            connector=None,
            sudo_password_prompt=None,
            override_netplan=None,
        ):
            applied.append((plan.node_id, override_netplan))
            return fabric_apply.FabricApplyReport(
                node_id=plan.node_id, name=plan.name, applied=True, verified=True
            )

        monkeypatch.setattr(fabric_apply, "apply_node_plan", fake_apply)
        custom = "network:\n  version: 2\n"
        response = await client.post(
            "/api/fabric/apply", json={"files": {PEER_ID: custom}}
        )
        assert response.status_code == 200, response.text
        assert applied == [(PEER_ID, custom)]
