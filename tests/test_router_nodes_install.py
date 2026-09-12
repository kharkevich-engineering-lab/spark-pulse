"""Installing an agent from the browser: ``/host-key`` then ``/install``.

Through the real installer against the simulated fleet — the same node the
installer's own suite uses — with the app served in-process on the test's
event loop, so the control plane, the simulated node and its in-process agent
all share one loop and one hub.

What is asserted is the contract the Cluster page relies on: the host key is
shown before any secret moves, a secret is used once and kept nowhere, the
registry ends up saying what the hub says, and every refusal names its cause
with a status a form can act on.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from spark_pulse.agent import onboarding
from spark_pulse.agent import runtime as agent_runtime
from spark_pulse.agent.bootstrap_transport import generate_keypair
from spark_pulse.agent.runtime import ControlPlaneRuntime
from spark_pulse.app import create_app
from spark_pulse.mock import node_registry as mock_registry
from tests.agent_bootstrap_fixtures import PASSWORD, SUDO_PASSWORD, USER, make_node

pytestmark = pytest.mark.asyncio

PEER_ID = "9f3c1a2b4d5e6f708192a3b4c5d6e7f8"  # the mock registry's seeded peer
CONTROL_ID = "c0ntr01plane00000000000000000001"


@pytest.fixture(autouse=True)
def clean_registry():
    mock_registry.reset()
    yield
    mock_registry.reset()


@pytest.fixture
async def fleet_hooks(agent_fleet, agent_bundle, monkeypatch):
    """Route the router's connector and bundle to the simulated fleet."""
    monkeypatch.setattr(onboarding, "connector_factory", lambda: agent_fleet)
    monkeypatch.setattr(onboarding, "bundle_factory", lambda: agent_bundle)
    return agent_fleet


@pytest.fixture
async def running(agent_server):
    """The app with a control-plane runtime installed, on this loop."""
    runtime = ControlPlaneRuntime(agent_server, asyncio.get_running_loop())
    with agent_runtime.use(runtime):
        yield runtime


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def peer_node(tmp_path, **kwargs):
    """A simulated node at the seeded peer's address."""
    peer = mock_registry.get_node(PEER_ID)
    return make_node(tmp_path, host=peer.address, **kwargs)


async def host_key(client) -> str:
    response = await client.get(f"/api/nodes/{PEER_ID}/host-key")
    assert response.status_code == 200, response.text
    return response.json()["fingerprint"]


# ── The host key ─────────────────────────────────────────────────────────────


async def test_the_host_key_is_shown_before_anything_is_sent(
    client, running, fleet_hooks, tmp_path
):
    node = fleet_hooks.add(peer_node(tmp_path))
    response = await client.get(f"/api/nodes/{PEER_ID}/host-key")
    assert response.status_code == 200
    body = response.json()
    assert body["fingerprint"] == node.host_key.fingerprint
    assert body["fingerprint"].startswith("SHA256:")
    assert body["host"] == node.host
    assert body["port"] == 22
    # Asking for the key is not a login: nothing ran on the node.
    assert node.commands == []


async def test_an_unreachable_node_is_a_502_from_the_host_key_on(
    client, running, fleet_hooks
):
    response = await client.get(f"/api/nodes/{PEER_ID}/host-key")
    assert response.status_code == 502
    assert "cannot reach" in response.json()["detail"]


async def test_the_control_node_has_nothing_to_install(client, running, fleet_hooks):
    response = await client.get(f"/api/nodes/{CONTROL_ID}/host-key")
    assert response.status_code == 400
    assert "its own agent" in response.json()["detail"]
    response = await client.post(f"/api/nodes/{CONTROL_ID}/install", json={})
    assert response.status_code == 400


async def test_an_unknown_node_is_a_404(client, running, fleet_hooks):
    assert (await client.get("/api/nodes/nope/host-key")).status_code == 404
    assert (await client.post("/api/nodes/nope/install", json={})).status_code == 404


# ── Installing ───────────────────────────────────────────────────────────────


async def test_a_password_install_leaves_a_connected_node_and_no_secret(
    client, running, fleet_hooks, tmp_path
):
    node = fleet_hooks.add(peer_node(tmp_path, sudo_password=SUDO_PASSWORD))
    fingerprint = await host_key(client)

    response = await client.post(
        f"/api/nodes/{PEER_ID}/install",
        json={
            "username": USER,
            "auth": "password",
            "password": PASSWORD,
            "sudo_password": SUDO_PASSWORD,
            "host_key_fingerprint": fingerprint,
            "control_host": "127.0.0.1",
        },
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["connected"] is True
    assert report["used_password"] is True
    assert report["node_id"] == PEER_ID, "the agent enrolled as the registry's node"
    assert report["steps"], "the report says what happened"

    # The secrets were used and are nowhere in what came back.
    text = json.dumps(report)
    assert PASSWORD not in text
    assert SUDO_PASSWORD not in text

    # The registry now says what the hub says: healthy, with the SSH user kept.
    assert report["node"]["state"] == "healthy"
    assert report["node"]["ssh_user"] == USER
    assert report["node"]["agent"] == {"enrolled": True, "connected": True}
    listed = (await client.get("/api/nodes")).json()
    peer = next(n for n in listed if n["id"] == PEER_ID)
    assert peer["state"] == "healthy"
    assert peer["agent"]["connected"] is True

    # The node holds the control plane's public key for every SSH after this.
    authorized = node.read(f"/home/{USER}/.ssh/authorized_keys", USER).decode()
    assert "spark-pulse-control-plane" in authorized


async def test_an_operator_key_with_a_passphrase_is_unlocked_here(
    client, running, fleet_hooks, tmp_path
):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    private = ed25519.Ed25519PrivateKey.generate()
    locked = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.BestAvailableEncryption(b"open sesame"),
    )
    public = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )

    node = peer_node(tmp_path, password=None)
    node.users[USER].authorized_keys.add(public)
    fleet_hooks.add(node)
    fingerprint = await host_key(client)

    response = await client.post(
        f"/api/nodes/{PEER_ID}/install",
        json={
            "username": USER,
            "auth": "key",
            "private_key": locked.decode(),
            "passphrase": "open sesame",
            "host_key_fingerprint": fingerprint,
            "control_host": "127.0.0.1",
        },
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["connected"] is True
    assert report["used_password"] is False
    assert "open sesame" not in json.dumps(report)
    # The operator's key opened the door; the control plane's key was left
    # behind so the next SSH does not need the operator's key again.
    authorized = node.read(f"/home/{USER}/.ssh/authorized_keys", USER).decode()
    assert "spark-pulse-control-plane" in authorized


async def test_the_control_planes_own_key_works_once_it_is_authorized(
    client, running, fleet_hooks, tmp_path
):
    from spark_pulse.agent.bootstrap import control_plane_keypair

    node = peer_node(tmp_path, password=None)
    node.users[USER].authorized_keys.add(
        control_plane_keypair(running.server).public_openssh
    )
    fleet_hooks.add(node)
    fingerprint = await host_key(client)

    response = await client.post(
        f"/api/nodes/{PEER_ID}/install",
        json={
            "username": USER,
            "auth": "control_plane_key",
            "host_key_fingerprint": fingerprint,
            "control_host": "127.0.0.1",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["connected"] is True


async def test_the_control_host_defaults_to_the_control_nodes_address(
    client, running, fleet_hooks, tmp_path
):
    """No ``control_host`` in the body: the control plane's own entry is it."""
    mock_registry.update_node(CONTROL_ID, address="127.0.0.1")
    fleet_hooks.add(peer_node(tmp_path))
    fingerprint = await host_key(client)
    response = await client.post(
        f"/api/nodes/{PEER_ID}/install",
        json={
            "username": USER,
            "auth": "password",
            "password": PASSWORD,
            "host_key_fingerprint": fingerprint,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["connected"] is True


async def test_a_control_node_without_an_address_cannot_be_dialled(
    client, running, fleet_hooks, tmp_path
):
    mock_registry.update_node(CONTROL_ID, address="")
    fleet_hooks.add(peer_node(tmp_path))
    fingerprint = await host_key(client)
    response = await client.post(
        f"/api/nodes/{PEER_ID}/install",
        json={
            "username": USER,
            "auth": "password",
            "password": PASSWORD,
            "host_key_fingerprint": fingerprint,
        },
    )
    assert response.status_code == 400
    assert "control_host" in response.json()["detail"]


# ── Refusals ─────────────────────────────────────────────────────────────────


async def test_a_host_key_that_changed_is_refused_before_any_secret_moves(
    client, running, fleet_hooks, tmp_path
):
    node = fleet_hooks.add(peer_node(tmp_path))
    response = await client.post(
        f"/api/nodes/{PEER_ID}/install",
        json={
            "username": USER,
            "auth": "password",
            "password": PASSWORD,
            "host_key_fingerprint": "SHA256:not-what-the-node-offers",
        },
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert node.host_key.fingerprint in detail
    assert "nothing was sent" in detail
    assert node.commands == [] and node.uploads == []


async def test_a_wrong_password_is_a_401_and_nothing_ran(
    client, running, fleet_hooks, tmp_path
):
    node = fleet_hooks.add(peer_node(tmp_path))
    fingerprint = await host_key(client)
    response = await client.post(
        f"/api/nodes/{PEER_ID}/install",
        json={
            "username": USER,
            "auth": "password",
            "password": "not-the-password",
            "host_key_fingerprint": fingerprint,
        },
    )
    assert response.status_code == 401
    assert "refused the credentials" in response.json()["detail"]
    assert node.commands == []


async def test_a_key_that_cannot_be_read_is_a_400_that_says_why(
    client, running, fleet_hooks, tmp_path
):
    fleet_hooks.add(peer_node(tmp_path))
    fingerprint = await host_key(client)
    response = await client.post(
        f"/api/nodes/{PEER_ID}/install",
        json={
            "username": USER,
            "auth": "key",
            "private_key": "ssh-ed25519 AAAA this is a public key, not a private one",
            "host_key_fingerprint": fingerprint,
        },
    )
    assert response.status_code == 400
    assert "not an OpenSSH or PEM private key" in response.json()["detail"]


async def test_a_key_the_node_does_not_know_is_a_401(
    client, running, fleet_hooks, tmp_path
):
    fleet_hooks.add(peer_node(tmp_path))
    fingerprint = await host_key(client)
    response = await client.post(
        f"/api/nodes/{PEER_ID}/install",
        json={
            "username": USER,
            "auth": "key",
            "private_key": generate_keypair("stranger").private_openssh.decode(),
            "host_key_fingerprint": fingerprint,
        },
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "body,missing",
    [
        (
            {"auth": "password", "password": "x", "host_key_fingerprint": "f"},
            "username",
        ),
        ({"username": "a", "password": "x", "host_key_fingerprint": "f"}, "auth"),
        (
            {"username": "a", "auth": "password", "password": "x"},
            "host_key_fingerprint",
        ),
        (
            {"username": "a", "auth": "password", "host_key_fingerprint": "f"},
            "password",
        ),
        ({"username": "a", "auth": "key", "host_key_fingerprint": "f"}, "private key"),
        (
            {"username": "a", "auth": "telepathy", "host_key_fingerprint": "f"},
            "auth must be one of",
        ),
        (
            {
                "username": "a",
                "auth": "control_plane_key",
                "host_key_fingerprint": "f",
                "port": 0,
            },
            "port",
        ),
        (
            {
                "username": "a",
                "auth": "control_plane_key",
                "host_key_fingerprint": "f",
                "scope": "root",
            },
            "scope",
        ),
    ],
)
async def test_an_incomplete_request_names_the_field(
    client, running, fleet_hooks, tmp_path, body, missing
):
    fleet_hooks.add(peer_node(tmp_path))
    response = await client.post(f"/api/nodes/{PEER_ID}/install", json=body)
    assert response.status_code == 400, response.text
    assert missing in response.json()["detail"]


async def test_without_the_transport_nothing_can_enrol(client, fleet_hooks, tmp_path):
    """No runtime installed: the answer is 503, not a traceback."""
    fleet_hooks.add(peer_node(tmp_path))
    response = await client.post(
        f"/api/nodes/{PEER_ID}/install",
        json={
            "username": USER,
            "auth": "password",
            "password": PASSWORD,
            "host_key_fingerprint": "SHA256:whatever",
        },
    )
    assert response.status_code == 503
    assert "transport is not running" in response.json()["detail"]


async def test_a_node_that_went_away_between_the_two_calls_is_a_502(
    client, running, fleet_hooks, tmp_path
):
    node = fleet_hooks.add(peer_node(tmp_path))
    fingerprint = await host_key(client)
    node.reachable = False
    response = await client.post(
        f"/api/nodes/{PEER_ID}/install",
        json={
            "username": USER,
            "auth": "password",
            "password": PASSWORD,
            "host_key_fingerprint": fingerprint,
        },
    )
    assert response.status_code == 502


async def test_the_listing_reads_agent_state_from_the_hub(client, running):
    """Without an agent the seeded peer is not enrolled, and says so."""
    listed = {n["id"]: n for n in (await client.get("/api/nodes")).json()}
    assert listed[PEER_ID]["agent"] == {"enrolled": False, "connected": False}
    assert listed[PEER_ID]["state"] == "unknown", "what the registry last wrote"


async def test_the_listing_without_a_transport_reports_no_agent(client):
    listed = (await client.get("/api/nodes")).json()
    assert all(n["agent"] == {"enrolled": False, "connected": False} for n in listed)
