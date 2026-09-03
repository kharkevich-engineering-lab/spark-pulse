"""Tests for the /api/engines router."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from spark_pulse.app import create_app
from spark_pulse.config import config
from spark_pulse.engines import EngineRegistry, reset_registry

V1_RECIPE = {
    "id": "qwen3-8b",
    "name": "Qwen3 8B",
    "model": "Qwen/Qwen3-8B",
    "command": "vllm serve Qwen/Qwen3-8B --port {port} "
    "--tensor-parallel-size {tensor_parallel} "
    "--distributed-executor-backend ray",
    "defaults": {"port": 8000, "tensor_parallel": 2},
    "env": {},
}

V2_RECIPE = {
    "id": "generic",
    "name": "Generic",
    "model": "Qwen/Qwen3-8B",
    "recipe_version": "2",
    "command": "",
    "params": {"port": 30000, "host": "0.0.0.0"},
    "env": {},
}

RECIPES = {r["id"]: r for r in (V1_RECIPE, V2_RECIPE)}


@pytest.fixture
def client(tmp_path):
    reset_registry()
    # No configured indexes: the tests stay offline and see bundled specs only.
    with patch.object(type(config), "engine_indexes", property(lambda self: [])):
        registry = EngineRegistry(cache_dir=tmp_path / "engine-cache")
        yield from _client(registry)
    reset_registry()


def _client(registry):
    with (
        patch.object(type(config), "engine_indexes", property(lambda self: [])),
        patch("spark_pulse.routers.engines.get_registry", return_value=registry),
        patch(
            "spark_pulse.tools.recipes.get_recipe",
            side_effect=lambda rid, *a, **kw: RECIPES.get(rid),
        ),
    ):
        app = create_app()
        with TestClient(app) as test_client:
            yield test_client


class TestListEngines:
    def test_list(self, client):
        response = client.get("/api/engines")
        assert response.status_code == 200
        data = response.json()
        assert data["default_engine"] == "vllm"
        keys = {e["key"] for e in data["engines"]}
        assert keys == {"vllm/default", "sglang/default"}

    def test_list_carries_capabilities_image_and_verification(self, client):
        engines = client.get("/api/engines").json()["engines"]
        vllm = next(e for e in engines if e["engine"] == "vllm")
        assert vllm["capabilities"]["mods"] is True
        assert vllm["image_ref"].endswith(":0.1.0")
        assert vllm["digest"] is None
        assert vllm["enabled"] is True
        assert vllm["legacy_tags"] == ["vllm-node"]
        assert vllm["verified"][0]["model"] == "Qwen/Qwen2.5-0.5B-Instruct"
        assert vllm["source"] == "bundled"


class TestGetEngine:
    def test_get_by_engine_and_variant(self, client):
        response = client.get("/api/engines/sglang/default")
        assert response.status_code == 200
        data = response.json()
        assert data["engine"] == "sglang"
        assert data["runtime"]["ports"] == {"api": 30000, "rendezvous": 50000}
        assert data["runtime"]["container"]["shm_size_gb"] == 32

    def test_get_defaults_the_variant(self, client):
        assert client.get("/api/engines/vllm").json()["variant"] == "default"

    def test_unknown_engine_is_404(self, client):
        response = client.get("/api/engines/tensorrt/default")
        assert response.status_code == 404
        assert "unknown engine" in response.json()["detail"]


class TestRefresh:
    def test_refresh_reports_per_index_status(self, client):
        response = client.post("/api/engines/refresh")
        assert response.status_code == 200
        data = response.json()
        assert data["refreshed"] is True
        assert data["indexes"] == []
        assert data["engines"] == 2


class TestRender:
    def test_render_solo(self, client):
        response = client.post(
            "/api/engines/render", json={"recipe_id": "qwen3-8b", "solo": True}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["engine"] == "vllm"
        assert data["variant"] == "default"
        assert data["readiness"] == "/v1/models"
        assert data["solo"] is True
        assert len(data["ranks"]) == 1
        assert data["ranks"][0]["command"] == (
            "vllm serve Qwen/Qwen3-8B --port 8000 --tensor-parallel-size 1"
        )
        assert data["ranks"][0]["env"]["NCCL_IB_DISABLE"] == "0"

    def test_render_two_nodes_returns_a_script_per_rank(self, client):
        response = client.post(
            "/api/engines/render",
            json={
                "recipe_id": "qwen3-8b",
                "nodes": [
                    {"host": "spark-a", "ip": "10.0.0.1", "eth_if": "eth0"},
                    {"host": "spark-b", "ip": "10.0.0.2", "eth_if": "eth0"},
                ],
            },
        )
        assert response.status_code == 200
        ranks = response.json()["ranks"]
        assert [r["node_rank"] for r in ranks] == [0, 1]
        assert [r["host"] for r in ranks] == ["spark-a", "spark-b"]
        assert "--master-addr 10.0.0.1 --master-port 29501" in ranks[0]["command"]
        assert "--headless" in ranks[1]["command"]
        assert "--headless" not in ranks[0]["command"]
        assert ranks[1]["env"]["VLLM_HOST_IP"] == "10.0.0.2"

    def test_render_accepts_plain_host_strings(self, client):
        response = client.post(
            "/api/engines/render",
            json={"recipe_id": "qwen3-8b", "nodes": ["spark-a", "spark-b"]},
        )
        assert response.status_code == 200
        assert "--master-addr spark-a" in response.json()["ranks"][0]["command"]

    def test_render_with_overrides_and_extra_args(self, client):
        response = client.post(
            "/api/engines/render",
            json={
                "recipe_id": "qwen3-8b",
                "model": "Qwen/Qwen3-32B",
                "params": {"port": 9001, "tensor_parallel": 4},
                "extra_args": ["--chat-template", "my template.jinja"],
                "solo": True,
            },
        )
        command = response.json()["ranks"][0]["command"]
        assert "--port 9001" in command
        assert "--tensor-parallel-size 4" in command
        assert command.endswith("--chat-template 'my template.jinja'")

    def test_render_on_sglang(self, client):
        response = client.post(
            "/api/engines/render",
            json={"recipe_id": "generic", "engine": "sglang", "solo": True},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["engine"] == "sglang"
        assert data["ports"] == {"api": 30000, "rendezvous": 50000}
        assert data["container"]["privileged"] is False
        assert "--dist-init-addr 127.0.0.1:50000" in data["ranks"][0]["command"]

    def test_render_rejects_a_v1_recipe_on_sglang(self, client):
        response = client.post(
            "/api/engines/render",
            json={"recipe_id": "qwen3-8b", "engine": "sglang", "solo": True},
        )
        assert response.status_code == 400
        assert "engine-specific command" in response.json()["detail"]

    def test_render_unknown_recipe_is_404(self, client):
        response = client.post("/api/engines/render", json={"recipe_id": "nope"})
        assert response.status_code == 404

    def test_render_unknown_engine_is_404(self, client):
        response = client.post(
            "/api/engines/render", json={"recipe_id": "qwen3-8b", "engine": "trtllm"}
        )
        assert response.status_code == 404

    def test_render_missing_placeholder_is_400(self, client):
        bad = {**V1_RECIPE, "id": "bad", "command": "vllm serve M --x {nope}"}
        RECIPES["bad"] = bad
        try:
            response = client.post(
                "/api/engines/render", json={"recipe_id": "bad", "solo": True}
            )
            assert response.status_code == 400
            assert "{nope}" in response.json()["detail"]
        finally:
            RECIPES.pop("bad")


class TestSettingsExposure:
    def test_settings_report_engine_config(self, client):
        data = client.get("/api/settings").json()
        assert data["default_engine"] == "vllm"
        assert isinstance(data["engine_indexes"], list)
        assert data["engines"]["sglang"]["enabled"] is True


class TestMcpTools:
    def test_engine_tools_are_registered(self):
        from spark_pulse.mcp_http import HANDLERS, TOOLS

        names = {t["name"] for t in TOOLS}
        assert {"list_engines", "render_launch"} <= names
        assert {"list_engines", "render_launch"} <= set(HANDLERS)
