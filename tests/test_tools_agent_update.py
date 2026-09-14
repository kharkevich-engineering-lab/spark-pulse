"""Updating a node's agent over its stream, and the auto-update sweep."""

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


@pytest.fixture(autouse=True)
def clean_registry():
    mock_registry.reset()
    yield
    mock_registry.reset()


@pytest.fixture
async def running(agent_server):
    runtime = ControlPlaneRuntime(agent_server, asyncio.get_running_loop())
    with agent_runtime.use(runtime):
        yield runtime


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def real_agent_update():
    import importlib

    return importlib.import_module("spark_pulse.tools.agent_update")


class FakeService:
    def __init__(self, sink, version="9.9.9", restarting=True, raises=None):
        self.sink = sink
        self.version = version
        self.restarting = restarting
        self.raises = raises

    def install_bundle(self, tarball, dir_name, version):
        self.sink.append(
            {"dir_name": dir_name, "version": version, "size": len(tarball)}
        )
        if self.raises:
            raise self.raises
        return pb.BundleInstalled(
            version=self.version, path=f"/x/{dir_name}", restarting=self.restarting
        )


def patch(monkeypatch, **kw):
    # update_node resolves through the switched node_service, which is the mock
    # under simulation — so that is where the fake service goes.
    from spark_pulse.mock import node_service as ns

    au = real_agent_update()
    sink = []
    monkeypatch.setattr(ns, "service_for", lambda node, **_: FakeService(sink, **kw))
    # a bundle without needing a real binary
    monkeypatch.setattr(
        au,
        "_bundle_for",
        lambda target: type(
            "B", (), {"data": b"BUNDLE", "name": "9.9.9-deadbeef", "version": "9.9.9"}
        )(),
    )
    return sink


class TestUpdateRoute:
    async def test_the_control_node_is_refused(self, client, running):
        r = await client.post(f"/api/nodes/{CONTROL_ID}/update")
        assert r.status_code == 400
        assert "control node" in r.json()["detail"]

    async def test_an_unknown_node_is_404(self, client, running):
        assert (await client.post("/api/nodes/nope/update")).status_code == 404

    async def test_without_a_transport_is_503(self, client):
        assert (await client.post(f"/api/nodes/{PEER_ID}/update")).status_code == 503

    async def test_a_peer_is_updated_over_its_agent(self, client, running, monkeypatch):
        sink = patch(monkeypatch)
        r = await client.post(f"/api/nodes/{PEER_ID}/update")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["updated"] is True and body["restarting"] is True
        assert body["version"] == "9.9.9"
        assert sink == [{"dir_name": "9.9.9-deadbeef", "version": "9.9.9", "size": 6}]

    async def test_a_refusing_agent_is_reported_not_raised(
        self, client, running, monkeypatch
    ):
        patch(monkeypatch, raises=RuntimeError("not a unit-managed install"))
        body = (await client.post(f"/api/nodes/{PEER_ID}/update")).json()
        assert body["updated"] is False
        assert "unit-managed" in body["detail"]


class TestStalePeers:
    async def test_a_peer_whose_digest_we_do_not_ship_is_stale(
        self, running, monkeypatch
    ):
        from spark_pulse.agent import bundle

        monkeypatch.setattr(bundle, "packaged_digests", lambda: {"t": "shipped"})
        running.hub.attach(
            AgentConnection(PEER_ID, facts=pb.NodeFacts(binary_sha256="different"))
        )
        au = real_agent_update()
        assert [n.id for n in au.stale_peers()] == [PEER_ID]

    async def test_a_peer_on_the_shipped_digest_is_not_stale(
        self, running, monkeypatch
    ):
        from spark_pulse.agent import bundle

        monkeypatch.setattr(bundle, "packaged_digests", lambda: {"t": "shipped"})
        running.hub.attach(
            AgentConnection(PEER_ID, facts=pb.NodeFacts(binary_sha256="shipped"))
        )
        au = real_agent_update()
        assert au.stale_peers() == []

    async def test_the_control_node_is_never_stale(self, running, monkeypatch):
        from spark_pulse.agent import bundle

        monkeypatch.setattr(bundle, "packaged_digests", lambda: {"t": "shipped"})
        # even connected with a wrong digest, the control node is excluded
        running.hub.attach(
            AgentConnection(CONTROL_ID, facts=pb.NodeFacts(binary_sha256="different"))
        )
        mock_registry.update_node(CONTROL_ID, machine_id="x")  # ensure present
        au = real_agent_update()
        assert CONTROL_ID not in [n.id for n in au.stale_peers()]
