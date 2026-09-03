"""Tests for the /api/models router, driven by the simulation-mode mock tools."""

import time

import pytest
from fastapi.testclient import TestClient

from spark_pulse import tools
from spark_pulse.app import create_app
from spark_pulse.mock import models as mock_models


@pytest.fixture
def client(monkeypatch):
    """Serve the router from the mock model tools regardless of SIMULATION_MODE."""
    monkeypatch.setattr(tools, "models", mock_models)
    mock_models._deleted.clear()
    mock_models._jobs.clear()
    mock_models._cancelled.clear()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def _wait(client, job_id, states, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/models/downloads/{job_id}").json()
        if job["status"] in states:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {states}")


class TestCatalogue:
    def test_list_models(self, client):
        response = client.get("/api/models")
        assert response.status_code == 200
        models = response.json()["models"]
        assert len(models) >= 4
        assert {"id", "size_bytes", "revision", "config", "referenced_by"} <= set(
            models[0]
        )

    def test_catalogue_has_a_quantized_model(self, client):
        models = client.get("/api/models").json()["models"]
        quantized = [m for m in models if (m["config"] or {}).get("quantization")]
        assert quantized

    def test_get_model_by_nested_id(self, client):
        response = client.get("/api/models/openai/gpt-oss-120b")
        assert response.status_code == 200
        assert response.json()["id"] == "openai/gpt-oss-120b"

    def test_get_unknown_model_404(self, client):
        response = client.get("/api/models/nope/nope")
        assert response.status_code == 404


class TestSources:
    def test_get_sources(self, client):
        sources = client.get("/api/models/sources").json()["sources"]
        assert [s["name"] for s in sources] == ["hf", "mirror", "local"]

    def test_put_sources(self, client):
        body = {
            "sources": [
                {
                    "name": "hf",
                    "type": "hf_hub",
                    "endpoint": "https://huggingface.co",
                    "token_secret": "hf_token",
                },
                {"name": "vault", "type": "local_path", "path": "/srv/models"},
            ]
        }
        response = client.put("/api/models/sources", json=body)
        assert response.status_code == 200
        assert [s["name"] for s in response.json()["sources"]] == ["hf", "vault"]

    def test_put_sources_rejects_non_list(self, client):
        assert (
            client.put("/api/models/sources", json={"sources": {}}).status_code == 400
        )

    def test_put_sources_rejects_invalid_entry(self, client):
        response = client.put(
            "/api/models/sources", json={"sources": [{"type": "hf_hub"}]}
        )
        assert response.status_code == 400
        assert "name is required" in response.json()["detail"]


class TestDownloads:
    def test_download_lifecycle(self, client):
        response = client.post(
            "/api/models/download", json={"model": "acme/tiny", "source": "hf"}
        )
        assert response.status_code == 200
        job = response.json()
        assert job["status"] in ("queued", "running")
        assert job["bytes_total"] > 0

        listed = client.get("/api/models/downloads").json()["jobs"]
        assert [j["id"] for j in listed] == [job["id"]]

        done = _wait(client, job["id"], ("completed", "failed"))
        assert done["status"] == "completed"
        assert done["bytes_done"] == done["bytes_total"]
        assert done["path"]

    def test_download_requires_model(self, client):
        assert client.post("/api/models/download", json={}).status_code == 400

    def test_download_rejects_bad_allow_patterns(self, client):
        response = client.post(
            "/api/models/download", json={"model": "acme/tiny", "allow_patterns": "x"}
        )
        assert response.status_code == 400

    def test_download_unknown_source(self, client):
        response = client.post(
            "/api/models/download", json={"model": "acme/tiny", "source": "nope"}
        )
        assert response.status_code == 400
        assert "Unknown model source" in response.json()["detail"]

    def test_download_status_404(self, client):
        assert client.get("/api/models/downloads/nope").status_code == 404

    def test_cancel_download(self, client):
        job = client.post("/api/models/download", json={"model": "acme/tiny"}).json()
        response = client.post(f"/api/models/downloads/{job['id']}/cancel")
        assert response.status_code == 200
        final = _wait(client, job["id"], ("cancelled", "completed"))
        assert final["status"] == "cancelled"

    def test_cancel_unknown_job_404(self, client):
        assert client.post("/api/models/downloads/nope/cancel").status_code == 404


class TestDistribution:
    def test_sync(self, client):
        response = client.post(
            "/api/models/openai/gpt-oss-120b/sync", json={"nodes": ["n1", "n2"]}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert [r["node"] for r in body["results"]] == ["n1", "n2"]

    def test_sync_requires_nodes(self, client):
        response = client.post("/api/models/openai/gpt-oss-120b/sync", json={})
        assert response.status_code == 400

    def test_sync_unknown_model(self, client):
        response = client.post("/api/models/nope/nope/sync", json={"nodes": ["n1"]})
        assert response.status_code == 400

    def test_presence(self, client):
        response = client.get("/api/models/openai/gpt-oss-120b/presence?nodes=n1,n2")
        assert response.status_code == 200
        body = response.json()
        assert body["local"] is True
        assert [n["node"] for n in body["nodes"]] == ["n1", "n2"]


class TestDelete:
    def test_delete(self, client):
        response = client.delete("/api/models/openai/gpt-oss-120b")
        assert response.status_code == 200
        assert response.json()["deleted"] == "openai/gpt-oss-120b"
        assert client.get("/api/models/openai/gpt-oss-120b").status_code == 404

    def test_delete_unknown_404(self, client):
        assert client.delete("/api/models/nope/nope").status_code == 404

    def test_delete_in_use_conflicts(self, client, monkeypatch):
        monkeypatch.setattr(
            mock_models,
            "models_in_use",
            lambda: {"openai/gpt-oss-120b": ["dep1"]},
        )
        response = client.delete("/api/models/openai/gpt-oss-120b")
        assert response.status_code == 409
        assert "in use" in response.json()["detail"]


class TestMcpTools:
    def test_model_tools_registered(self):
        from spark_pulse.mcp_http import HANDLERS, TOOLS

        names = {t["name"] for t in TOOLS}
        assert {"list_models", "download_model", "model_download_status"} <= names
        assert {"list_models", "download_model", "model_download_status"} <= set(
            HANDLERS
        )


class TestSseRoute:
    def test_models_stream_registered(self):
        from spark_pulse.sse import router

        assert "/sse/models" in {r.path for r in router.routes}
