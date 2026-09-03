"""E2E tests for recipe customization feature using live server.

These tests verify the customization CRUD flow against a running server.

Prerequisites:
    1. Run: pytest tests/test_e2e_custom_recipes.py
    2. Or start the backend server separately

Usage:
    pytest tests/test_e2e_custom_recipes.py -v
"""

from __future__ import annotations

import os
import socket
import threading

import pytest
import httpx

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def temp_dir(tmp_path_factory):
    """Create a temp directory for custom-recipes.json and spark-vllm-docker."""
    return tmp_path_factory.mktemp("custom-recipes-e2e")


@pytest.fixture(scope="module")
def custom_recipe_file(temp_dir):
    """Return path for custom-recipes.json."""
    return temp_dir / "custom-recipes.json"


@pytest.fixture(scope="module")
def spark_vllm_dir(temp_dir):
    """Create a fake spark-vllm-docker with a recipe."""
    spark = temp_dir / "spark-vllm-docker"
    spark.mkdir()
    recipes_dir = spark / "recipes"
    recipes_dir.mkdir()

    (recipes_dir / "simple.yaml").write_text(
        "name: Simple Model\nmodel: test/simple\ncontainer: vllm-node\n"
        "command: vllm serve {model} --port {port}\n"
        "defaults:\n  port: 8000\n  tensor_parallel: 1\n",
        encoding="utf-8",
    )
    (recipes_dir / "complex.yaml").write_text(
        "name: Complex Model\nmodel: test/complex\ncontainer: vllm-advanced\n"
        "command: vllm serve {model}\n"
        "defaults:\n  port: 9000\n  gpu_memory_utilization: 0.9\n"
        "env:\n  HF_TOKEN: dummy\n  DEBUG: '1'\n"
        "build_args: ['--build-arg X=1']\n",
        encoding="utf-8",
    )
    return str(spark)


@pytest.fixture(scope="module")
def e2e_config(spark_vllm_dir, custom_recipe_file):
    """Configure the app for e2e tests."""
    from spark_pulse.config import config

    os.environ["SPARK_PULSE_AUTH_ENABLED"] = "false"
    config._data["spark_vllm_path"] = spark_vllm_dir

    # Patch custom recipes path (both standalone and inline synthetic module)
    import spark_pulse.tools.custom_recipes as cr

    cr._CUSTOM_PATH = custom_recipe_file

    # Also patch the synthetic custom_recipes submodule inside mock/recipes.py
    import spark_pulse.mock.recipes as mock_recipes

    mock_recipes.custom_recipes._CUSTOM_PATH = custom_recipe_file

    return config


@pytest.fixture(scope="module")
def e2e_app(e2e_config):
    """Create test app for e2e tests."""
    from spark_pulse.app import create_app

    return create_app()


@pytest.fixture(scope="module")
def e2e_server(e2e_app):
    """Run a test server for e2e tests."""
    from uvicorn import Config, Server

    # Pick a concrete free port. Using port=0 with Uvicorn doesn't update
    # Config.port, so clients would otherwise try to connect to port 0.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    config = Config(app=e2e_app, host="127.0.0.1", port=port, log_level="error")
    server = Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import time

    base_url = f"http://127.0.0.1:{port}"
    last_error: Exception | None = None
    for _ in range(30):
        try:
            httpx.get(f"{base_url}/health", timeout=1)
            break
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            last_error = exc
            time.sleep(0.2)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError(f"Test server did not start: {last_error}")

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


# ── Tests ────────────────────────────────────────────────────────────────────


class TestRecipeCustomizationE2E:
    """E2E tests for recipe customization API."""

    def test_list_recipes_includes_is_customized(self, e2e_server):
        """Recipe list should include is_customized field."""
        resp = httpx.get(f"{e2e_server}/api/recipes")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 2
        for recipe in data:
            assert "is_customized" in recipe
            assert isinstance(recipe["is_customized"], bool)

    def test_get_customization_empty(self, e2e_server):
        """GET customization should return empty dict when none exist."""
        resp = httpx.get(f"{e2e_server}/api/recipes/customize/simple")
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_save_customization_command(self, e2e_server):
        """Should save a command customization."""
        body = {"command": "custom-serve {model} --port {port}"}
        resp = httpx.put(f"{e2e_server}/api/recipes/customize/simple", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["command"] == "custom-serve {model} --port {port}"

    def test_save_customization_defaults(self, e2e_server):
        """Should save defaults customization."""
        body = {"defaults": {"port": 9999, "tensor_parallel": 2}}
        resp = httpx.put(f"{e2e_server}/api/recipes/customize/simple", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["defaults"]["port"] == 9999

    def test_get_customization_after_save(self, e2e_server):
        """Should return saved customization on GET."""
        body = {"model": "custom-model"}
        httpx.put(f"{e2e_server}/api/recipes/customize/simple", json=body)

        resp = httpx.get(f"{e2e_server}/api/recipes/customize/simple")
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "custom-model"

    def test_save_customization_env(self, e2e_server):
        """Should save env customization."""
        body = {"env": {"CUSTOM_VAR": "value"}}
        resp = httpx.put(f"{e2e_server}/api/recipes/customize/simple", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["env"]["CUSTOM_VAR"] == "value"

    def test_save_customization_build_args(self, e2e_server):
        """Should save build_args customization."""
        body = {"build_args": ["--build-arg NEW=1"]}
        resp = httpx.put(f"{e2e_server}/api/recipes/customize/simple", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["build_args"] == ["--build-arg NEW=1"]

    def test_save_ignores_non_customizable_fields(self, e2e_server):
        """Should ignore fields not in CUSTOMIZABLE_FIELDS."""
        body = {"description": "should be ignored", "command": "keep"}
        resp = httpx.put(f"{e2e_server}/api/recipes/customize/simple", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert "command" in data
        assert "description" not in data

    def test_delete_customization(self, e2e_server):
        """DELETE should remove customization."""
        httpx.put(f"{e2e_server}/api/recipes/customize/simple", json={"command": "cmd"})
        resp = httpx.delete(f"{e2e_server}/api/recipes/customize/simple")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # Verify it's gone
        resp = httpx.get(f"{e2e_server}/api/recipes/customize/simple")
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_delete_nonexistent_returns_false(self, e2e_server):
        """DELETE for non-existent should return false."""
        resp = httpx.delete(f"{e2e_server}/api/recipes/customize/simple")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is False

    def test_customization_persists_across_requests(self, e2e_server):
        """Customization saved on PUT should be visible on subsequent GET."""
        body = {"command": "persist-cmd", "defaults": {"port": 7777}}
        httpx.put(f"{e2e_server}/api/recipes/customize/complex", json=body)

        resp = httpx.get(f"{e2e_server}/api/recipes/customize/complex")
        assert resp.status_code == 200
        data = resp.json()
        assert data["command"] == "persist-cmd"
        assert data["defaults"]["port"] == 7777

    def test_recipe_list_reflects_customization(self, e2e_server):
        """Recipe list should show is_customized=true after save."""
        httpx.put(f"{e2e_server}/api/recipes/customize/simple", json={"command": "x"})

        resp = httpx.get(f"{e2e_server}/api/recipes")
        assert resp.status_code == 200
        simple = next((r for r in resp.json() if r["id"] == "simple"), None)
        assert simple is not None
        assert simple["is_customized"] is True

    def test_delete_reflects_in_recipe_list(self, e2e_server):
        """Deleting customization should remove is_customized flag."""
        httpx.put(f"{e2e_server}/api/recipes/customize/complex", json={"command": "x"})
        httpx.delete(f"{e2e_server}/api/recipes/customize/complex")

        resp = httpx.get(f"{e2e_server}/api/recipes")
        assert resp.status_code == 200
        complex_recipe = next((r for r in resp.json() if r["id"] == "complex"), None)
        assert complex_recipe is not None
        assert complex_recipe["is_customized"] is False
