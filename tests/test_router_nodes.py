"""The node registry REST API, against the simulated registry."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spark_pulse.app import create_app
from spark_pulse.mock import discovery as mock_discovery
from spark_pulse.mock import node_registry as mock_registry


@pytest.fixture(autouse=True)
def clean_registry():
    """The simulated registry is process-wide; keep it per-test."""
    mock_registry.reset()
    mock_discovery.reset_mock_discovery()
    yield
    mock_registry.reset()
    mock_discovery.reset_mock_discovery()


@pytest.fixture
def client():
    return TestClient(create_app())


class TestList:
    def test_the_control_plane_comes_first_and_is_marked(self, client):
        nodes = client.get("/api/nodes").json()
        assert len(nodes) >= 2
        assert nodes[0]["is_control_plane"] is True
        assert all(not n["is_control_plane"] for n in nodes[1:])

    def test_every_field_the_cluster_page_renders_is_present(self, client):
        node = client.get("/api/nodes").json()[1]
        for key in (
            "id",
            "name",
            "address",
            "is_control_plane",
            "ethernet_interface",
            "infiniband_interfaces",
            "state",
            "last_seen",
        ):
            assert key in node
        assert node["infiniband_interfaces"] == ["ib0", "ib1"]
        assert node["state"] in ("healthy", "unknown", "dead")

    def test_one_node_by_id(self, client):
        node = client.get("/api/nodes").json()[0]
        assert client.get(f"/api/nodes/{node['id']}").json() == node

    def test_an_unknown_id_is_a_404(self, client):
        assert client.get("/api/nodes/nope").status_code == 404


class TestAdd:
    def test_a_node_is_added_with_a_minted_id(self, client):
        response = client.post(
            "/api/nodes",
            json={"name": "spark-04", "address": "10.0.0.14", "ssh_user": "spark"},
        )
        assert response.status_code == 200
        node = response.json()
        assert len(node["id"]) == 32
        assert node["name"] == "spark-04"
        assert node["state"] == "unknown"
        assert node["is_control_plane"] is False
        assert any(n["id"] == node["id"] for n in client.get("/api/nodes").json())

    def test_a_client_supplied_id_is_refused(self, client):
        """Identity is minted here; a client cannot name a node into existence."""
        response = client.post(
            "/api/nodes", json={"id": "chosen", "address": "10.0.0.14"}
        )
        assert response.status_code == 400
        assert "minted by the server" in response.json()["detail"]

    def test_an_address_is_required(self, client):
        response = client.post("/api/nodes", json={"name": "nameless"})
        assert response.status_code == 400
        assert "needs an address" in response.json()["detail"]

    def test_a_duplicate_address_is_refused(self, client):
        response = client.post("/api/nodes", json={"address": "10.0.0.11"})
        assert response.status_code == 400
        assert "already registered" in response.json()["detail"]

    def test_the_name_defaults_to_the_address(self, client):
        node = client.post("/api/nodes", json={"address": "10.0.0.15"}).json()
        assert node["name"] == "10.0.0.15"


class TestUpdate:
    def test_editable_fields_change(self, client):
        node = client.get("/api/nodes").json()[1]
        updated = client.patch(
            f"/api/nodes/{node['id']}",
            json={"name": "fabric-head", "state": "healthy"},
        ).json()
        assert updated["id"] == node["id"]
        assert updated["name"] == "fabric-head"
        assert updated["state"] == "healthy"

    def test_identity_is_not_editable(self, client):
        node = client.get("/api/nodes").json()[1]
        response = client.patch(f"/api/nodes/{node['id']}", json={"id": "other"})
        assert response.status_code == 400
        assert "cannot change: id" in response.json()["detail"]

    def test_control_plane_membership_is_not_editable(self, client):
        node = client.get("/api/nodes").json()[1]
        response = client.patch(
            f"/api/nodes/{node['id']}", json={"is_control_plane": True}
        )
        assert response.status_code == 400

    def test_an_invalid_state_is_refused(self, client):
        node = client.get("/api/nodes").json()[1]
        response = client.patch(f"/api/nodes/{node['id']}", json={"state": "flapping"})
        assert response.status_code == 400
        assert "state must be one of" in response.json()["detail"]

    def test_an_unknown_id_is_a_404(self, client):
        assert client.patch("/api/nodes/nope", json={"name": "x"}).status_code == 404


class TestRemove:
    def test_a_peer_is_forgotten(self, client):
        node = client.get("/api/nodes").json()[1]
        response = client.delete(f"/api/nodes/{node['id']}")
        assert response.status_code == 200
        assert response.json()["removed"] is True
        assert all(n["id"] != node["id"] for n in client.get("/api/nodes").json())

    def test_the_control_plane_cannot_be_removed(self, client):
        control = client.get("/api/nodes").json()[0]
        response = client.delete(f"/api/nodes/{control['id']}")
        assert response.status_code == 400
        assert "control plane cannot be removed" in response.json()["detail"]

    def test_an_unknown_id_is_a_404(self, client):
        assert client.delete("/api/nodes/nope").status_code == 404


class TestDiscover:
    def test_peers_are_returned_and_marked_registered(self, client):
        body = client.get("/api/nodes/discover?timeout=0.1").json()
        assert body["mdns_available"] is True
        by_address = {peer["address"]: peer for peer in body["peers"]}

        # 10.0.0.11 is the seeded spark-02, so it is already registered.
        assert by_address["10.0.0.11"]["registered"] is True
        assert by_address["10.0.0.11"]["is_spark_pulse"] is True
        assert by_address["10.0.0.11"]["node_id"]

        # 10.0.0.12 answers `_ssh._tcp` only: a Spark that has never enrolled.
        assert by_address["10.0.0.12"]["registered"] is False
        assert by_address["10.0.0.12"]["is_spark_pulse"] is False
        assert by_address["10.0.0.12"]["node_id"] == ""

    def test_unavailable_mdns_is_an_empty_list_not_an_error(self, client):
        mock_discovery.set_mdns_available(False)
        response = client.get("/api/nodes/discover?timeout=0.1")
        assert response.status_code == 200
        assert response.json() == {"mdns_available": False, "peers": []}

    def test_discover_is_not_mistaken_for_a_node_id(self, client):
        """`/discover` is declared before `/{node_id}`, so it must not 404."""
        assert client.get("/api/nodes/discover?timeout=0.1").status_code == 200
        assert client.get("/api/nodes/diagnostics").status_code == 200


class TestDiagnostics:
    def test_duplicate_machine_ids_are_reported_with_a_remedy(self, client):
        findings = client.get("/api/nodes/diagnostics").json()["findings"]
        codes = {finding["code"] for finding in findings}
        assert "duplicate_machine_id" in codes

        finding = next(f for f in findings if f["code"] == "duplicate_machine_id")
        assert finding["severity"] == "warning"
        assert finding["remedy"]
        assert len(finding["node_ids"]) == 2

    def test_mdns_being_unavailable_is_information(self, client):
        mock_discovery.set_mdns_available(False)
        findings = client.get("/api/nodes/diagnostics").json()["findings"]
        finding = next(f for f in findings if f["code"] == "mdns_unavailable")
        assert finding["severity"] == "info"
        assert "always works" in finding["remedy"]

    def test_a_fabric_link_without_link_local_is_reported(self, client):
        """The `dgx` scenario has two fabric links and no link-local on them."""
        mock_discovery.set_mock_scenario("dgx")
        findings = client.get("/api/nodes/diagnostics").json()["findings"]
        finding = next(f for f in findings if f["code"] == "interface_no_link_local")
        assert "ib0" in finding["summary"]
        assert "netplan" in finding["remedy"]
