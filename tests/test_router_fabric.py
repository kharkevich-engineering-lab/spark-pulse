"""``/api/fabric``: the cluster's ConnectX ports as the agents report them."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from spark_pulse.agent import agent_pb2 as pb
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


def fabric_result(interfaces, peers, *, address_ok=True, mtu_ok=True, reachable=True):
    """A proto FabricResult a fake agent service returns for configure_fabric."""
    return pb.FabricResult(
        ports=[
            pb.FabricPortState(
                netdev=netdev,
                cidr=cidr,
                address_ok=address_ok,
                mtu=int(mtu),
                mtu_ok=mtu_ok,
            )
            for netdev, _name, cidr, mtu in interfaces
        ],
        pings=[
            pb.FabricPing(netdev=netdev, address=addr, reachable=reachable)
            for netdev, addr in peers
        ],
        steps=["configured via nmcli (fake)"],
    )


class FakeService:
    """Records the fabric config it was handed and answers as told."""

    def __init__(self, sink, **result_kwargs):
        self.sink = sink
        self.result_kwargs = result_kwargs

    def configure_fabric(self, interfaces, peers):
        self.sink.append({"interfaces": interfaces, "peers": peers})
        return fabric_result(interfaces, peers, **self.result_kwargs)


def patch_service(monkeypatch, sink, **result_kwargs):
    import importlib

    node_service = importlib.import_module("spark_pulse.tools.node_service")

    monkeypatch.setattr(
        node_service,
        "service_for",
        lambda node, **_: FakeService(sink, **result_kwargs),
    )
    return sink


class TestApply:
    async def test_without_a_transport_it_is_a_503(self, client):
        assert (await client.post("/api/fabric/apply", json={})).status_code == 503

    async def test_nothing_proposed_is_a_400_that_says_why(self, client, running):
        connect(running, PEER_ID, facts(cabled=False))
        response = await client.post("/api/fabric/apply", json={})
        assert response.status_code == 400
        assert "nothing to apply" in response.json()["detail"]

    async def test_the_proposed_node_is_configured_through_its_agent(
        self, client, running, monkeypatch
    ):
        connect(running, PEER_ID, facts())
        sink = patch_service(monkeypatch, [])
        response = await client.post("/api/fabric/apply", json={})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mode"] == "direct"
        assert [r["node_id"] for r in body["reports"]] == [PEER_ID]
        report = body["reports"][0]
        assert report["applied"] and report["verified"]
        assert report["errors"] == []
        # The agent was handed the planned per-port config and the peers.
        assert len(sink) == 1
        netdevs = [i[0] for i in sink[0]["interfaces"]]
        assert netdevs == ["enp1s0f1np1", "enP2p1s0f1np1"]
        assert all(
            name.startswith("spark-pulse-")
            for _n, name, _c, _m in sink[0]["interfaces"]
        )
        assert report["readback"]["enp1s0f1np1"]["address_ok"] is True

    async def test_node_ids_narrow_the_apply(self, client, running, monkeypatch):
        connect(running, PEER_ID, facts())
        sink = patch_service(monkeypatch, [])
        assert (
            await client.post("/api/fabric/apply", json={"node_ids": ["not-a-node"]})
        ).status_code == 400
        assert sink == []
        response = await client.post("/api/fabric/apply", json={"node_ids": [PEER_ID]})
        assert response.status_code == 200 and len(sink) == 1

    async def test_a_peer_that_does_not_answer_is_not_verified(
        self, client, running, monkeypatch
    ):
        # A second registered, connected node puts a peer on the pair's subnets,
        # so there is something to ping — and a ping that fails is not verified.
        second = mock_registry.add_node(name="spark-03", address="10.0.0.12")
        connect(running, PEER_ID, facts())
        connect(running, second.id, facts())
        patch_service(monkeypatch, [], reachable=False)
        body = (await client.post("/api/fabric/apply", json={})).json()
        assert body["reports"], body
        report = next(r for r in body["reports"] if r["node_id"] == PEER_ID)
        assert report["pings"], "the pair gives each node a peer to ping"
        assert report["applied"] and not report["verified"]
        assert any("does not answer" in e for e in report["errors"])
        assert "pinned" not in report

    async def test_an_agent_that_cannot_be_reached_is_reported_not_raised(
        self, client, running, monkeypatch
    ):
        connect(running, PEER_ID, facts())
        import importlib

        node_service = importlib.import_module("spark_pulse.tools.node_service")

        def boom(node, **_):
            raise node_service.NoAgent("no agent for this node")

        monkeypatch.setattr(node_service, "service_for", boom)
        body = (await client.post("/api/fabric/apply", json={})).json()
        report = body["reports"][0]
        assert not report["applied"]
        assert any("no agent" in e for e in report["errors"])

    async def test_the_agent_failing_the_op_is_applied_but_not_verified(
        self, client, running, monkeypatch
    ):
        connect(running, PEER_ID, facts())
        import importlib

        node_service = importlib.import_module("spark_pulse.tools.node_service")

        class Failing:
            def configure_fabric(self, interfaces, peers):
                raise RuntimeError("nmcli: sudo: a password is required")

        monkeypatch.setattr(node_service, "service_for", lambda node, **_: Failing())
        body = (await client.post("/api/fabric/apply", json={})).json()
        report = body["reports"][0]
        assert report["applied"] and not report["verified"]
        assert any("password is required" in e for e in report["errors"])


class TestShapesAndPinning:
    async def test_both_cables_on_the_seeded_pair_is_the_dual_shape(
        self, client, running
    ):
        connect(running, PEER_ID, facts(both_cables=True))
        body = (await client.get("/api/fabric")).json()
        peer = next(n for n in body["nodes"] if n["node_id"] == PEER_ID)
        assert peer["mode"] == "dual"
        assert peer["ib_hca"] == "rocep1s0f0,rocep1s0f1,roceP2p1s0f0,roceP2p1s0f1"
        assert peer["pinned"] == {
            "ethernet_interface": "eth0",
            "infiniband_interfaces": ["ib0", "ib1"],
            "fabric_addresses": [],
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
        patch_service(monkeypatch, [])
        body = (await client.post("/api/fabric/apply", json={})).json()
        assert body["reports"][0]["pinned"] == {
            "ethernet_interface": "enp1s0f1np1",
            "infiniband_interfaces": ["rocep1s0f1", "roceP2p1s0f1"],
            "fabric_addresses": ["192.168.177.11", "192.168.178.11"],
            "fabric_mode": "direct",
        }
        node = mock_registry.get_node(PEER_ID)
        assert node.ethernet_interface == "enp1s0f1np1"
        assert list(node.infiniband_interfaces) == ["rocep1s0f1", "roceP2p1s0f1"]
        assert node.fabric_mode == "direct"
        # The addresses a bulk transfer will prefer over the management NIC.
        assert list(node.fabric_addresses) == ["192.168.177.11", "192.168.178.11"]

    async def test_an_unverified_apply_pins_nothing(self, client, running, monkeypatch):
        connect(running, PEER_ID, facts())
        patch_service(monkeypatch, [], address_ok=False)
        body = (await client.post("/api/fabric/apply", json={})).json()
        assert "pinned" not in body["reports"][0]
        assert mock_registry.get_node(PEER_ID).fabric_mode == ""

    async def test_a_configured_node_is_pinned_without_calling_the_agent(
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
        import importlib

        node_service = importlib.import_module("spark_pulse.tools.node_service")

        def never(node, **_):  # pragma: no cover
            raise AssertionError("a configured node needs no agent call")

        monkeypatch.setattr(node_service, "service_for", never)
        response = await client.post("/api/fabric/apply", json={})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reports"] == []
        assert body["pinned"][PEER_ID]["fabric_mode"] == "direct"
        assert mock_registry.get_node(PEER_ID).ethernet_interface == "enp1s0f1np1"


class TestTheFabricAddressIsTrustedToo:
    """One machine, two links, one host key.

    Bootstrap recorded the key an operator confirmed against the node's
    management address. A transfer that now dials the fabric address is a
    first sighting to OpenSSH and strict checking refuses it — so the entry
    that is already there is copied, and nothing new is trusted.
    """

    KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    async def test_the_confirmed_key_is_trusted_under_every_fabric_address(
        self, client, running, monkeypatch
    ):
        from spark_pulse.tools.ssh import trust_host_key, trusted_entries

        trust_host_key("10.0.0.11", self.KEY)
        connect(running, PEER_ID, facts())
        patch_service(monkeypatch, [])

        await client.post("/api/fabric/apply", json={})

        assert trusted_entries("192.168.177.11") == [self.KEY]
        assert trusted_entries("192.168.178.11") == [self.KEY]

    async def test_an_unverified_apply_trusts_nothing_new(
        self, client, running, monkeypatch
    ):
        from spark_pulse.tools.ssh import trust_host_key, trusted_entries

        trust_host_key("10.0.0.11", self.KEY)
        connect(running, PEER_ID, facts())
        patch_service(monkeypatch, [], address_ok=False)

        await client.post("/api/fabric/apply", json={})

        assert trusted_entries("192.168.177.11") == []

    async def test_a_node_with_no_recorded_key_gets_none_invented_for_it(
        self, client, running, monkeypatch
    ):
        """Never a blind scan: what is copied is what was already confirmed."""
        from spark_pulse.tools.ssh import known_hosts_path

        connect(running, PEER_ID, facts())
        patch_service(monkeypatch, [])

        body = (await client.post("/api/fabric/apply", json={})).json()

        assert body["reports"][0]["verified"] is True
        assert not known_hosts_path().exists()
