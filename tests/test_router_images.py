"""Tests for the /api/images router, driven by the simulation-mode mock tools."""

import importlib
import time

import pytest
from fastapi.testclient import TestClient

from spark_pulse import tools
from spark_pulse.app import create_app
from spark_pulse.mock import docker as mock_docker
from spark_pulse.mock import images as mock_images

# Under SIMULATION_MODE ``from spark_pulse.tools import images`` hands back the
# mock; the job registry lives in the real module, which is what the mock's
# pull machinery re-exports.
real_images = importlib.import_module("spark_pulse.tools.images")

VLLM_REF = "ghcr.io/kharkevich-engineering-lab/spark-pulse-engine/vllm:0.1.0"


@pytest.fixture
def client(monkeypatch):
    """Serve the router from the mock image tools on a fresh simulated host."""
    monkeypatch.setattr(tools, "images", mock_images)
    mock_docker.reset_mock()
    real_images._jobs.clear()
    real_images._cancelled.clear()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    real_images._jobs.clear()
    real_images._cancelled.clear()


def _wait(client, job_id, states, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/images/pulls/{job_id}").json()
        if job["status"] in states:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {states}")


class TestCatalogue:
    def test_list_images(self, client):
        response = client.get("/api/images")

        assert response.status_code == 200
        entries = response.json()["images"]
        assert entries
        assert {
            "ref",
            "repository",
            "tag",
            "engine",
            "variant",
            "present",
            "size_bytes",
            "local_digest",
            "index_digest",
            "digest_drift",
        } <= set(entries[0])

    def test_catalogue_shows_an_image_that_is_not_pulled(self, client):
        entries = client.get("/api/images").json()["images"]
        assert any(not e["present"] for e in entries)

    def test_catalogue_shows_digest_drift(self, client):
        """The simulated index advertises a digest the host does not have."""
        entries = client.get("/api/images").json()["images"]
        drifted = [e for e in entries if e["digest_drift"]]

        assert drifted
        assert all(e["update_available"] for e in drifted)
        assert all(e["local_digest"] != e["index_digest"] for e in drifted)


class TestPulls:
    def test_pull_runs_and_can_be_polled(self, client):
        started = client.post("/api/images/pull", json={"ref": "ghcr.io/x/y:1"})
        assert started.status_code == 200
        job = started.json()
        assert job["status"] == "queued"

        done = _wait(client, job["id"], ("completed",))
        assert done["percent"] == 100.0

        listed = client.get("/api/images/pulls").json()["jobs"]
        assert job["id"] in [j["id"] for j in listed]

    def test_pull_requires_a_ref(self, client):
        assert client.post("/api/images/pull", json={}).status_code == 400

    def test_unknown_job_is_404(self, client):
        assert client.get("/api/images/pulls/nope").status_code == 404
        assert client.post("/api/images/pulls/nope/cancel").status_code == 404

    def test_cancel_a_pull(self, client):
        job = client.post("/api/images/pull", json={"ref": "ghcr.io/x/z:1"}).json()
        response = client.post(f"/api/images/pulls/{job['id']}/cancel")

        assert response.status_code == 200
        final = _wait(client, job["id"], ("cancelled", "completed"))
        assert final["status"] in ("cancelled", "completed")


class TestDelete:
    def test_delete_by_query_param(self, client):
        """A ref has slashes and colons, so it travels as a query parameter."""
        response = client.request("DELETE", "/api/images", params={"ref": VLLM_REF})

        assert response.status_code == 200
        assert response.json()["deleted"] == VLLM_REF

    def test_delete_by_body(self, client):
        response = client.request("DELETE", "/api/images", json={"ref": VLLM_REF})

        assert response.status_code == 200
        assert response.json()["deleted"] == VLLM_REF

    def test_delete_without_a_ref_is_400(self, client):
        assert client.request("DELETE", "/api/images").status_code == 400

    def test_delete_of_an_absent_image_is_404(self, client):
        response = client.request(
            "DELETE", "/api/images", params={"ref": "ghcr.io/x/never:1"}
        )
        assert response.status_code == 404

    def test_delete_of_an_image_in_use_is_409(self, client, monkeypatch):
        # The mock re-exports the real delete_image, so the guard it consults
        # lives in the real module.
        monkeypatch.setattr(real_images, "images_in_use", lambda: {VLLM_REF: ["dep-1"]})

        response = client.request("DELETE", "/api/images", params={"ref": VLLM_REF})

        assert response.status_code == 409
        assert "dep-1" in response.json()["detail"]


class TestDistribution:
    def test_sync_reports_per_node_results(self, client):
        response = client.post(
            "/api/images/sync", json={"ref": VLLM_REF, "nodes": ["n1", "n2"]}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert [r["node"] for r in body["results"]] == ["n1", "n2"]
        assert any(r["skipped"] for r in body["results"])

    def test_sync_requires_nodes(self, client):
        response = client.post("/api/images/sync", json={"ref": VLLM_REF, "nodes": []})
        assert response.status_code == 400

    def test_sync_requires_a_ref(self, client):
        response = client.post("/api/images/sync", json={"nodes": ["n1"]})
        assert response.status_code == 400

    def test_sync_of_an_absent_image_is_400(self, client):
        response = client.post(
            "/api/images/sync", json={"ref": "ghcr.io/x/never:1", "nodes": ["n1"]}
        )
        assert response.status_code == 400

    def test_presence(self, client):
        response = client.get(
            "/api/images/presence", params={"ref": VLLM_REF, "nodes": "n1,n2"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["ref"] == VLLM_REF
        assert [n["node"] for n in body["nodes"]] == ["n1", "n2"]
