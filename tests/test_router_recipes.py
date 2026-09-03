"""Tests for the /api/recipes router.

Focused on what the deploy form reads: which engines a recipe declares, whether
each one can actually run it, and which source it came from.
"""

import pytest
from fastapi.testclient import TestClient

from spark_pulse.app import create_app
from spark_pulse.config import config

BUNDLED_ID = "bundled/qwen2.5-0.5b-instruct"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """No upstream checkout: the bundled source is all there is."""
    monkeypatch.setattr(
        type(config),
        "spark_vllm_path",
        property(lambda self: str(tmp_path / "no-checkout")),
    )
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


class TestListRecipes:
    def test_bundled_recipes_are_listed_with_their_source(self, client):
        entries = client.get("/api/recipes").json()
        bundled = {e["id"]: e for e in entries if e["source"] == "bundled"}
        assert BUNDLED_ID in bundled
        assert bundled[BUNDLED_ID]["engines"] == ["vllm", "sglang"]

    def test_every_entry_carries_a_source_and_support_table(self, client):
        for entry in client.get("/api/recipes").json():
            assert entry["source"]
            assert {e["engine"] for e in entry["engine_support"]} == {
                "sglang",
                "vllm",
            }


class TestGetRecipe:
    def test_detail_reports_engines_and_per_engine_support(self, client):
        detail = client.get(f"/api/recipes/{BUNDLED_ID}").json()

        assert detail["id"] == BUNDLED_ID
        assert detail["recipe_version"] == "2"
        assert detail["engines"] == ["vllm", "sglang"]
        support = {e["engine"]: e for e in detail["engine_support"]}
        assert support["vllm"]["supported"] is True
        assert support["sglang"]["supported"] is True
        assert support["sglang"]["reason"] == ""
        assert support["sglang"]["enabled"] is True

    def test_detail_keeps_each_engine_args(self, client):
        specs = client.get(f"/api/recipes/{BUNDLED_ID}").json()["engine_specs"]
        assert specs["vllm"]["args"] == "--enable-prefix-caching"
        assert specs["sglang"]["args"] == "--chunked-prefill-size 2048"

    def test_a_v1_recipe_explains_why_sglang_is_unavailable(self, client, tmp_path):
        recipe_dir = tmp_path / "no-checkout" / "recipes"
        recipe_dir.mkdir(parents=True)
        (recipe_dir / "v1.yaml").write_text(
            "name: V1\nmodel: org/v1\ncontainer: vllm-node\n"
            "command: vllm serve org/v1 --port {port}\n",
            encoding="utf-8",
        )

        detail = client.get("/api/recipes/v1").json()

        assert detail["source"] == "upstream"
        assert detail["engines"] == ["vllm"]
        support = {e["engine"]: e for e in detail["engine_support"]}
        assert support["vllm"]["supported"] is True
        assert support["sglang"]["supported"] is False
        assert "engine-specific command" in support["sglang"]["reason"]

    def test_unknown_recipe_is_404(self, client):
        assert client.get("/api/recipes/nope").status_code == 404
