"""Functional tests for the recipe_import router."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spark_pulse import tools
from spark_pulse.app import create_app
from spark_pulse.tools import recipe_import

VALID_V1 = """
name: TinyLlama
container: vllm-node
command: vllm serve TinyLlama/TinyLlama-1.1B --port {port}
""".strip()


@pytest.fixture
def upstream(tmp_path):
    recipes = tmp_path / "upstream" / "recipes"
    recipes.mkdir(parents=True)
    (recipes / "tiny.yaml").write_text(VALID_V1, encoding="utf-8")
    return tmp_path / "upstream"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """App wired so imports land in a temp dir instead of the real config dir."""
    dest = tmp_path / "imported"
    monkeypatch.setattr(recipe_import, "IMPORTED_DIR", dest)
    monkeypatch.setattr(tools, "recipe_import", recipe_import)
    return TestClient(create_app(), raise_server_exceptions=False)


class TestImportStatus:
    def test_status_before_any_import(self, client):
        res = client.get("/api/recipes/import/status")
        assert res.status_code == 200
        assert res.json() == {"imported": False}

    def test_status_is_not_shadowed_by_the_recipe_catch_all(self, client):
        # /api/recipes/{recipe_id:path} must not swallow the import routes.
        assert client.get("/api/recipes/import/status").json() == {"imported": False}


class TestImportEndpoint:
    def test_import_from_path(self, client, upstream):
        res = client.post("/api/recipes/import", json={"path": str(upstream)})
        assert res.status_code == 200

        body = res.json()
        assert body["source"] == str(upstream)
        assert body["counts"]["recipes"] == {"ok": 1, "skipped": 0, "error": 0}
        assert body["recipes"][0]["id"] == "imported/tiny"

        status = client.get("/api/recipes/import/status").json()
        assert status["imported"] is True
        assert status["source"] == str(upstream)

    def test_import_from_git_url(self, client, monkeypatch, upstream):
        captured = {}

        def fake_import_from_git(url, ref=None, dest=None):
            captured["url"] = url
            captured["ref"] = ref
            return {"source_url": url, "ref": ref, "recipes": [], "mods": []}

        monkeypatch.setattr(recipe_import, "import_from_git", fake_import_from_git)

        res = client.post(
            "/api/recipes/import",
            json={"url": "https://example.invalid/repo.git", "ref": "main"},
        )
        assert res.status_code == 200
        assert captured == {"url": "https://example.invalid/repo.git", "ref": "main"}

    def test_requires_a_path_or_url(self, client):
        res = client.post("/api/recipes/import", json={})
        assert res.status_code == 400
        assert "path" in res.json()["detail"]

    def test_rejects_both_path_and_url(self, client, upstream):
        res = client.post(
            "/api/recipes/import",
            json={"path": str(upstream), "url": "https://example.invalid/x.git"},
        )
        assert res.status_code == 400

    def test_missing_path_is_404(self, client, tmp_path):
        res = client.post(
            "/api/recipes/import", json={"path": str(tmp_path / "nowhere")}
        )
        assert res.status_code == 404

    def test_wrong_layout_is_400(self, client, tmp_path):
        (tmp_path / "plain").mkdir()
        res = client.post("/api/recipes/import", json={"path": str(tmp_path / "plain")})
        assert res.status_code == 400

    def test_clone_failure_is_500(self, client, monkeypatch):
        def boom(url, ref=None, dest=None):
            raise RuntimeError("git clone failed: boom")

        monkeypatch.setattr(recipe_import, "import_from_git", boom)
        res = client.post(
            "/api/recipes/import", json={"url": "https://example.invalid/x.git"}
        )
        assert res.status_code == 500
        assert "boom" in res.json()["detail"]
