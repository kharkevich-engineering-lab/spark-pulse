"""Functional tests for the custom_files router.

These tests use the FastAPI TestClient with real temp directories.

Usage:
    pytest tests/test_router_custom_files.py -v
"""

from __future__ import annotations


import pytest

from spark_pulse.app import create_app
from spark_pulse.tools import custom_files


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """Create a test FastAPI app with temp custom file directories."""
    monkeypatch.setattr(custom_files, "_CUSTOM_RECIPES_DIR", tmp_path / "cr")
    monkeypatch.setattr(custom_files, "_CUSTOM_MODS_DIR", tmp_path / "cm")
    monkeypatch.setattr(custom_files, "_CUSTOM_RECIPES_DIR", tmp_path / "cr")
    monkeypatch.setattr(custom_files, "_CUSTOM_MODS_DIR", tmp_path / "cm")
    (tmp_path / "cr").mkdir()
    (tmp_path / "cm").mkdir()
    app = create_app()
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


# ── Recipes ────────────────────────────────────────────────────────────────


class TestListCustomRecipes:
    """Test GET /api/custom-files/recipes/list."""

    def test_empty(self, app_client, tmp_path, monkeypatch):
        """Should return empty list when no custom recipes."""
        resp = app_client.get("/api/custom-files/recipes/list")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_recipes(self, app_client, tmp_path, monkeypatch):
        """Should return list of custom recipes."""
        custom_files.save_custom_recipe("custom/test", "name: Test\nmodel: foo")

        resp = app_client.get("/api/custom-files/recipes/list")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "custom/test"
        assert data[0]["name"] == "Test"


class TestGetCustomRecipeContent:
    """Test GET /api/custom-files/recipes/{recipe_id}."""

    def test_200_with_content(self, app_client, tmp_path, monkeypatch):
        """Should return YAML content."""
        custom_files.save_custom_recipe("custom/my-recipe", "name: My\nmodel: bar")

        resp = app_client.get("/api/custom-files/recipes/custom/my-recipe")
        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == "name: My\nmodel: bar"
        assert data["id"] == "custom/my-recipe"

    def test_404_not_found(self, app_client, tmp_path, monkeypatch):
        """Should return 404 for nonexistent recipe."""
        resp = app_client.get("/api/custom-files/recipes/nonexistent")
        assert resp.status_code == 404


class TestSaveCustomRecipe:
    """Test PUT /api/custom-files/recipes/{recipe_id}."""

    def test_save_success(self, app_client, tmp_path, monkeypatch):
        """Should save YAML content."""
        resp = app_client.put(
            "/api/custom-files/recipes/custom/new",
            json={"content": "name: New\nmodel: baz"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"saved": True}

    def test_400_empty_content(self, app_client, tmp_path, monkeypatch):
        """Should reject empty YAML content."""
        resp = app_client.put(
            "/api/custom-files/recipes/custom/empty",
            json={"content": ""},
        )
        assert resp.status_code == 400

    def test_400_invalid_yaml(self, app_client, tmp_path, monkeypatch):
        """Should reject invalid YAML."""
        resp = app_client.put(
            "/api/custom-files/recipes/custom/bad",
            json={"content": "not: [[valid"},
        )
        assert resp.status_code == 400


class TestDeleteCustomRecipe:
    """Test DELETE /api/custom-files/recipes/{recipe_id}."""

    def test_delete_success(self, app_client, tmp_path, monkeypatch):
        """Should delete a custom recipe."""
        custom_files.save_custom_recipe("custom/del", "name: Del\n")

        resp = app_client.delete("/api/custom-files/recipes/del")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": True}

    def test_404_not_found(self, app_client, tmp_path, monkeypatch):
        """Should return 404 for nonexistent recipe."""
        resp = app_client.delete("/api/custom-files/recipes/nonexistent")
        assert resp.status_code == 404


# ── Mods ──────────────────────────────────────────────────────────────────


class TestListCustomMods:
    """Test GET /api/custom-files/mods/list."""

    def test_empty(self, app_client, tmp_path, monkeypatch):
        """Should return empty list when no custom mods."""
        resp = app_client.get("/api/custom-files/mods/list")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_mods(self, app_client, tmp_path, monkeypatch):
        """Should return list of custom mods."""
        (tmp_path / "cm" / "test-mod").mkdir(parents=True)
        (tmp_path / "cm" / "test-mod" / "run.sh").write_text(
            "#!/bin/bash", encoding="utf-8"
        )

        resp = app_client.get("/api/custom-files/mods/list")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "custom/test-mod"
        assert data[0]["has_run_sh"] is True


class TestGetCustomModFiles:
    """Test GET /api/custom-files/mods/{mod_id}."""

    def test_200_with_files(self, app_client, tmp_path, monkeypatch):
        """Should return mod files."""
        mpath = tmp_path / "cm" / "get-mod"
        mpath.mkdir(parents=True)
        (mpath / "run.sh").write_text("#!/bin/bash", encoding="utf-8")
        (mpath / "jinja.j2").write_text("{{ hello }}", encoding="utf-8")

        resp = app_client.get("/api/custom-files/mods/get-mod")
        assert resp.status_code == 200
        data = resp.json()
        assert "run.sh" in data["files"]
        assert "jinja.j2" in data["files"]

    def test_404_not_found(self, app_client, tmp_path, monkeypatch):
        """Should return 404 for nonexistent mod."""
        resp = app_client.get("/api/custom-files/mods/nonexistent")
        assert resp.status_code == 404


class TestSaveCustomMod:
    """Test PUT /api/custom-files/mods/{mod_id}."""

    def test_save_success(self, app_client, tmp_path, monkeypatch):
        """Should save mod files."""
        resp = app_client.put(
            "/api/custom-files/mods/custom/new-mod",
            json={"run.sh": "#!/bin/bash\necho hello", "extra.txt": "data"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"saved": True}


class TestDeleteCustomMod:
    """Test DELETE /api/custom-files/mods/{mod_id}."""

    def test_delete_success(self, app_client, tmp_path, monkeypatch):
        """Should delete a custom mod."""
        mpath = tmp_path / "cm" / "del-mod"
        mpath.mkdir(parents=True)
        (mpath / "run.sh").write_text("#!/bin/bash", encoding="utf-8")

        resp = app_client.delete("/api/custom-files/mods/del-mod")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": True}

    def test_404_not_found(self, app_client, tmp_path, monkeypatch):
        """Should return 404 for nonexistent mod."""
        resp = app_client.delete("/api/custom-files/mods/nonexistent")
        assert resp.status_code == 404


# ── Upload endpoints ──────────────────────────────────────────────────────


class TestUploadRecipe:
    """Test POST /api/custom-files/recipes/upload."""

    def test_upload_yaml(self, app_client, tmp_path, monkeypatch):
        """Should upload a YAML recipe file."""
        file_content = b"name: Uploaded\nmodel: intel/qwen\ncontainer: vllm"
        resp = app_client.post(
            "/api/custom-files/recipes/upload",
            files={"file": ("uploaded.yaml", file_content, "application/x-yaml")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "custom/uploaded"
        assert data["name"] == "Uploaded"


class TestUploadMod:
    """Test POST /api/custom-files/mods/upload."""

    def test_upload_mod_with_name(self, app_client, tmp_path, monkeypatch):
        """Should create a stub mod with given name."""
        resp = app_client.post(
            "/api/custom-files/mods/upload",
            data={"name": "stub-mod"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "custom/stub-mod"
        assert data["name"] == "stub-mod"
        assert data["saved"] is True


# ── Validation ──────────────────────────────────────────────────────────────


V1_RECIPE = """
name: A v1 Recipe
model: org/model-name
container: vllm-node
command: vllm serve org/model-name --port {port}
defaults:
  port: 8000
""".strip()

V2_RECIPE = """
recipe_version: "2"
name: A v2 Recipe
model: org/model-name
engine: vllm
params:
  port: 8000
engines:
  vllm:
    args: --enable-prefix-caching
""".strip()


class TestValidateRecipe:
    """``POST /api/custom-files/recipes/validate``.

    The endpoint used to check three fields of its own — ``name``, ``model``
    and ``container`` — and ``container`` is a *v1* field. A v2 recipe names an
    ``engine`` and has no container, so the editor refused every valid one with
    "container is required", which is not a thing wrong with the recipe. It
    answers through the same parser a deploy uses now.
    """

    def test_a_v1_recipe_is_accepted(self, app_client):
        response = app_client.post(
            "/api/custom-files/recipes/validate", json={"content": V1_RECIPE}
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "valid": True,
            "name": "A v1 Recipe",
            "recipe_version": "1",
            "model": "org/model-name",
        }

    def test_a_v2_recipe_is_accepted(self, app_client):
        """The bug this endpoint had: a valid v2 recipe was refused outright."""
        response = app_client.post(
            "/api/custom-files/recipes/validate", json={"content": V2_RECIPE}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["valid"] is True
        assert body["recipe_version"] == "2"
        assert body["name"] == "A v2 Recipe"

    def test_a_v2_recipe_is_not_asked_for_a_container(self, app_client):
        response = app_client.post(
            "/api/custom-files/recipes/validate", json={"content": V2_RECIPE}
        )

        assert "container" not in response.text

    def test_each_problem_is_reported_against_its_own_field(self, app_client):
        """One sentence tells you something is wrong; a field tells you where."""
        response = app_client.post(
            "/api/custom-files/recipes/validate", json={"content": "name: Only a name"}
        )

        assert response.status_code == 400
        detail = response.json()["detail"]
        paths = {e["path"] for e in detail["errors"]}
        assert "container" in paths
        assert "command" in paths
        assert detail["message"]

    def test_a_version_nothing_implements_is_refused_by_name(self, app_client):
        response = app_client.post(
            "/api/custom-files/recipes/validate",
            json={"content": 'recipe_version: "9"\nname: Future\n'},
        )

        assert response.status_code == 400
        assert "recipe_version" in str(response.json()["detail"])

    def test_yaml_that_is_not_a_mapping_is_refused(self, app_client):
        response = app_client.post(
            "/api/custom-files/recipes/validate", json={"content": "- just\n- a list\n"}
        )

        assert response.status_code == 400

    def test_broken_yaml_is_refused_rather_than_raised(self, app_client):
        response = app_client.post(
            "/api/custom-files/recipes/validate", json={"content": "name: [unclosed\n"}
        )

        assert response.status_code == 400

    def test_empty_content_says_so(self, app_client):
        response = app_client.post(
            "/api/custom-files/recipes/validate", json={"content": "   "}
        )

        assert response.status_code == 400
        assert "empty" in str(response.json()["detail"]).lower()
