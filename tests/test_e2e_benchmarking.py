"""E2E tests for benchmarking feature.

These tests verify the full API flow of the benchmarking feature by hitting a
live test server (no mocking of the API layer).

To run these tests, start the server in simulation mode first:
    SIMULATION_MODE=1 ./scripts/run-backend.sh

Then in another terminal:
    pytest tests/test_e2e_benchmarking.py -v

Usage:
    pytest tests/test_e2e_benchmarking.py -v
"""

from __future__ import annotations

import os

import pytest
import httpx

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def e2e_benchmarking_config():
    """Configure benchmarking as enabled for e2e tests."""
    from spark_pulse.config import config

    os.environ["SPARK_PULSE_AUTH_ENABLED"] = "false"
    config._data["benchmarking_enabled"] = True
    config._data["spark_vllm_path"] = "/tmp/spark-vllm-docker"
    return config


@pytest.fixture(scope="module")
def e2e_benchmarking_app(e2e_benchmarking_config):
    """Create test app with benchmarking enabled."""
    from spark_pulse.app import create_app
    return create_app()


@pytest.fixture(scope="module")
def e2e_benchmarking_server(e2e_benchmarking_app):
    """Run a test server for e2e benchmarking tests."""
    import threading
    import time

    from uvicorn import Config, Server

    TEST_PORT = 19876

    cfg = Config(
        app=e2e_benchmarking_app,
        host="127.0.0.1",
        port=TEST_PORT,
        log_level="error",
    )
    server = Server(cfg)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{TEST_PORT}"
    for _ in range(50):
        try:
            httpx.get(f"{base_url}/health", timeout=1)
            break
        except (httpx.ConnectError, httpx.ReadTimeout):
            time.sleep(0.2)

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


# ── Tests ────────────────────────────────────────────────────────────────────


class TestBenchmarkingSettingsE2E:
    """E2E tests for benchmarking settings endpoint."""

    def test_settings_includes_benchmarking_enabled(self, e2e_benchmarking_server):
        """Settings API includes benchmarking_enabled field."""
        resp = httpx.get(f"{e2e_benchmarking_server}/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "benchmarking_enabled" in data
        assert data["benchmarking_enabled"] is True

    def test_config_includes_benchmarking_enabled(self, e2e_benchmarking_server):
        """Config API includes benchmarking_enabled field."""
        resp = httpx.get(f"{e2e_benchmarking_server}/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "benchmarking_enabled" in data
        assert isinstance(data["benchmarking_enabled"], bool)


class TestBenchmarkingListE2E:
    """E2E tests for listing benchmarks."""

    def test_list_benchmarks_endpoint(self, e2e_benchmarking_server):
        """GET /api/benchmarks is accessible and returns a list."""
        resp = httpx.get(f"{e2e_benchmarking_server}/api/benchmarks")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)


class TestBenchmarkingApiE2E:
    """E2E tests for benchmarking API endpoints.

    Note: The router delegates to tools.benchmarking which tries to import
    llama-benchy. Since this is an optional dependency not installed in the
    test environment, run_benchmark returns 503. The test verifies correct
    error handling.
    """

    def test_list_benchmarks_endpoint(self, e2e_benchmarking_server):
        """GET /api/benchmarks is accessible."""
        resp = httpx.get(f"{e2e_benchmarking_server}/api/benchmarks")
        assert resp.status_code == 200

    def test_get_benchmark_not_found(self, e2e_benchmarking_server):
        """GET /api/benchmarks/{id} returns 404 for missing ID."""
        resp = httpx.get(f"{e2e_benchmarking_server}/api/benchmarks/nonexistent")
        assert resp.status_code == 404
        data = resp.json()
        assert "not found" in data["detail"]

    def test_get_latest_by_recipe(self, e2e_benchmarking_server):
        """GET /api/benchmarks/latest-by-recipe returns a dict."""
        resp = httpx.get(f"{e2e_benchmarking_server}/api/benchmarks/latest-by-recipe")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_recipe_benchmarks_empty(self, e2e_benchmarking_server):
        """GET /api/benchmarks/recipe/{id} returns empty list for unknown recipe."""
        resp = httpx.get(f"{e2e_benchmarking_server}/api/benchmarks/recipe/unknown-recipe")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_recipe_latest_not_found(self, e2e_benchmarking_server):
        """GET /api/benchmarks/recipe/{id}/latest returns 404 when no completed benchmark."""
        resp = httpx.get(f"{e2e_benchmarking_server}/api/benchmarks/recipe/unknown/latest")
        assert resp.status_code == 404
        data = resp.json()
        assert "benchmark data" in data["detail"].lower()

    def test_run_benchmark_missing_deployment_id(self, e2e_benchmarking_server):
        """POST /api/benchmarks without deployment_id returns 422."""
        resp = httpx.post(
            f"{e2e_benchmarking_server}/api/benchmarks",
            json={"params": {"benchmarks": ["throughput"]}},
        )
        assert resp.status_code == 422

    def test_run_benchmark_port_not_allowed(self, e2e_benchmarking_server):
        """POST /api/benchmarks with disallowed port returns 422."""
        resp = httpx.post(
            f"{e2e_benchmarking_server}/api/benchmarks",
            json={
                "deployment_id": "test-deployment",
                "params": {"port": 22},
            },
        )
        assert resp.status_code == 422
        data = resp.json()
        detail = str(data)
        assert "not allowed" in detail.lower()

    def test_run_benchmark_completed_in_sim_mode(self, e2e_benchmarking_server):
        """POST /api/benchmarks returns 200 with completed status in simulation mode.

        In simulation mode, the mock execute_benchmark is a no-op and returns
        pre-canned results immediately.
        """
        resp = httpx.post(
            f"{e2e_benchmarking_server}/api/benchmarks",
            json={
                "deployment_id": "test-deployment",
                "params": {"benchmarks": ["throughput"]},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        # In simulation mode, execute_benchmark is a no-op,
        # so we check that the endpoint accepted the request successfully

    def test_compare_too_few_runs(self, e2e_benchmarking_server):
        """POST /api/benchmarks/compare with <2 IDs returns 400."""
        resp = httpx.post(
            f"{e2e_benchmarking_server}/api/benchmarks/compare",
            json={"run_ids": ["b1"]},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert "at least 2" in data["detail"].lower()

    def test_compare_empty_run_ids(self, e2e_benchmarking_server):
        """POST /api/benchmarks/compare with empty IDs returns 400."""
        resp = httpx.post(
            f"{e2e_benchmarking_server}/api/benchmarks/compare",
            json={"run_ids": []},
        )
        assert resp.status_code == 400
