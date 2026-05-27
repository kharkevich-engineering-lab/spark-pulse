"""Functional tests for the benchmarking router.

These tests use the FastAPI TestClient with mocked benchmarking tools.

Usage:
    pytest tests/test_router_benchmarking.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from spark_pulse.app import create_app


@pytest.fixture
def app_client():
    """Create a test FastAPI app and return a TestClient."""
    app = create_app()
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


# ── Test: GET /api/benchmarks ───────────────────────────────────────────────


class TestListBenchmarks:
    """Test the list benchmarks endpoint."""

    def test_list_benchmarks_endpoint(self, app_client):
        """GET /api/benchmarks returns a list of benchmarks."""
        mock_benchmarks = [
            {
                "benchmark_id": "b1",
                "deployment_id": "dep-1",
                "recipe_id": "qwen3.5-397b",
                "baseline_id": None,
                "status": "completed",
                "started_at": "2026-05-27T10:00:00Z",
                "completed_at": "2026-05-27T10:05:00Z",
                "params": {"benchmarks": ["throughput"]},
                "results": {"throughput": 45.2},
            }
        ]

        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.list_benchmarks",
            return_value=mock_benchmarks,
        ):
            resp = app_client.get("/api/benchmarks")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["benchmark_id"] == "b1"
        assert data[0]["status"] == "completed"

    def test_list_empty_benchmarks(self, app_client):
        """GET /api/benchmarks returns empty list when none exist."""
        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.list_benchmarks",
            return_value=[],
        ):
            resp = app_client.get("/api/benchmarks")

        assert resp.status_code == 200
        assert resp.json() == []


# ── Test: GET /api/benchmarks/latest-by-recipe ──────────────────────────────


class TestLatestByRecipe:
    """Test the latest-by-recipe endpoint."""

    def test_latest_by_recipe_endpoint(self, app_client):
        """GET /api/benchmarks/latest-by-recipe returns dict keyed by recipe_id."""
        mock_result = {
            "r1": {
                "benchmark_id": "b1",
                "recipe_id": "r1",
                "recipe_name": "Model A",
                "status": "completed",
                "started_at": "2026-05-27T00:00:00Z",
                "completed_at": "2026-05-27T01:00:00Z",
                "results": {"throughput": 45.0},
            },
            "r2": {
                "benchmark_id": "b2",
                "recipe_id": "r2",
                "recipe_name": "Model B",
                "status": "completed",
                "started_at": "2026-05-27T02:00:00Z",
                "completed_at": "2026-05-27T03:00:00Z",
                "results": {"throughput": 50.0},
            },
        }

        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.get_latest_by_recipe",
            return_value=mock_result,
        ):
            resp = app_client.get("/api/benchmarks/latest-by-recipe")

        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"r1", "r2"}
        assert data["r1"]["recipe_name"] == "Model A"

    def test_latest_by_recipe_empty(self, app_client):
        """GET /api/benchmarks/latest-by-recipe returns empty dict when none exist."""
        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.get_latest_by_recipe",
            return_value={},
        ):
            resp = app_client.get("/api/benchmarks/latest-by-recipe")

        assert resp.status_code == 200
        assert resp.json() == {}


# ── Test: GET /api/benchmarks/recipe/{recipe_id} ────────────────────────────


class TestRecipeBenchmarks:
    """Test the recipe-specific benchmarks endpoints."""

    def test_recipe_benchmarks_endpoint(self, app_client):
        """GET /api/benchmarks/recipe/{id} returns benchmarks for a recipe."""
        mock_result = [
            {
                "benchmark_id": "b1",
                "recipe_id": "r1",
                "status": "completed",
                "started_at": "2026-05-25T00:00:00Z",
                "results": {"throughput": 40.0},
            },
            {
                "benchmark_id": "b2",
                "recipe_id": "r1",
                "status": "completed",
                "started_at": "2026-05-27T00:00:00Z",
                "results": {"throughput": 45.0},
            },
        ]

        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.get_benchmarks_for_recipe",
            return_value=mock_result,
        ):
            resp = app_client.get("/api/benchmarks/recipe/r1")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert all(b["recipe_id"] == "r1" for b in data)

    def test_recipe_benchmarks_empty(self, app_client):
        """GET /api/benchmarks/recipe/{id} returns empty list when no benchmarks."""
        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.get_benchmarks_for_recipe",
            return_value=[],
        ):
            resp = app_client.get("/api/benchmarks/recipe/missing")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_recipe_latest_endpoint(self, app_client):
        """GET /api/benchmarks/recipe/{id}/latest returns latest for a recipe."""
        mock_result = {
            "benchmark_id": "b2",
            "recipe_id": "r1",
            "status": "completed",
            "started_at": "2026-05-27T00:00:00Z",
            "results": {"throughput": 45.0},
        }

        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.get_recipe_latest",
            return_value=mock_result,
        ):
            resp = app_client.get("/api/benchmarks/recipe/r1/latest")

        assert resp.status_code == 200
        data = resp.json()
        assert data["benchmark_id"] == "b2"

    def test_recipe_latest_not_found(self, app_client):
        """GET /api/benchmarks/recipe/{id}/latest returns 404 when no completed benchmark."""
        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.get_recipe_latest",
            return_value=None,
        ):
            resp = app_client.get("/api/benchmarks/recipe/missing/latest")

        assert resp.status_code == 404
        data = resp.json()
        assert "benchmark data" in data["detail"].lower()


# ── Test: GET /api/benchmarks/{id} ──────────────────────────────────────────


class TestGetBenchmark:
    """Test the get benchmark endpoint."""

    def test_get_benchmark_endpoint(self, app_client):
        """GET /api/benchmarks/{id} returns a single benchmark."""
        mock_result = {
            "benchmark_id": "abc-def",
            "deployment_id": "dep-x",
            "recipe_id": "model-v1",
            "baseline_id": None,
            "status": "completed",
            "started_at": "2026-05-27T08:00:00Z",
            "completed_at": "2026-05-27T08:02:00Z",
            "params": {"context_length": 2048},
            "results": {"throughput": 38.5},
        }

        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.get_benchmark",
            return_value=mock_result,
        ):
            resp = app_client.get("/api/benchmarks/abc-def")

        assert resp.status_code == 200
        data = resp.json()
        assert data["benchmark_id"] == "abc-def"
        assert data["results"]["throughput"] == 38.5

    def test_get_benchmark_not_found(self, app_client):
        """GET /api/benchmarks/{id} returns 404 for missing ID."""
        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.get_benchmark",
            return_value=None,
        ):
            resp = app_client.get("/api/benchmarks/nonexistent")

        assert resp.status_code == 404
        data = resp.json()
        assert "not found" in data["detail"]


# ── Test: POST /api/benchmarks ──────────────────────────────────────────────


class TestRunBenchmark:
    """Test the run benchmark endpoint."""

    def test_run_benchmark_endpoint(self, app_client):
        """POST /api/benchmarks creates a benchmark with status='running'."""
        mock_result = {
            "benchmark_id": "new-bench-id",
            "deployment_id": "dep-123",
            "recipe_id": "qwen3.5-397b",
            "baseline_id": None,
            "status": "running",
            "started_at": "2026-05-27T12:00:00Z",
            "completed_at": None,
            "params": {"benchmarks": ["throughput"]},
            "results": None,
        }

        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.create_benchmark",
            return_value=mock_result,
        ), patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.execute_benchmark",
        ):
            resp = app_client.post(
                "/api/benchmarks",
                json={
                    "deployment_id": "dep-123",
                    "params": {"benchmarks": ["throughput"]},
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["benchmark_id"] == "new-bench-id"
        assert data["status"] == "running"

    def test_run_benchmark_with_baseline(self, app_client):
        """POST /api/benchmarks with baseline_id includes it in the result."""
        mock_result = {
            "benchmark_id": "b2",
            "deployment_id": "dep-1",
            "baseline_id": "baseline-1",
            "status": "running",
            "started_at": "2026-05-27T12:00:00Z",
            "completed_at": None,
            "params": {"benchmarks": ["throughput"]},
            "results": None,
        }

        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.create_benchmark",
            return_value=mock_result,
        ), patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.execute_benchmark",
        ):
            resp = app_client.post(
                "/api/benchmarks",
                json={
                    "deployment_id": "dep-1",
                    "baseline_id": "baseline-1",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["baseline_id"] == "baseline-1"

    def test_run_benchmark_missing_deployment_id(self, app_client):
        """POST /api/benchmarks without deployment_id returns 422."""
        resp = app_client.post(
            "/api/benchmarks",
            json={"params": {"benchmarks": ["throughput"]}},
        )

        assert resp.status_code == 422

    def test_run_benchmark_port_not_allowed(self, app_client):
        """POST /api/benchmarks with disallowed port returns 422."""
        resp = app_client.post(
            "/api/benchmarks",
            json={
                "deployment_id": "dep-1",
                "params": {"port": 22, "benchmarks": ["throughput"]},
            },
        )

        assert resp.status_code == 422
        # Pydantic validation errors return a list of error objects
        data = resp.json()
        detail = str(data)
        assert "not allowed" in detail.lower()

    def test_run_benchmark_port_allowed(self, app_client):
        """POST /api/benchmarks with allowed port (8000) succeeds."""
        mock_result = {
            "benchmark_id": "b1",
            "deployment_id": "dep-1",
            "status": "running",
            "started_at": "2026-05-27T12:00:00Z",
            "completed_at": None,
            "params": {"port": 8000, "benchmarks": ["throughput"]},
            "results": None,
        }

        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.create_benchmark",
            return_value=mock_result,
        ), patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.execute_benchmark",
        ):
            resp = app_client.post(
                "/api/benchmarks",
                json={
                    "deployment_id": "dep-1",
                    "params": {"port": 8000, "benchmarks": ["throughput"]},
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["params"]["port"] == 8000


# ── Test: POST /api/benchmarks/compare ──────────────────────────────────────


class TestCompareRuns:
    """Test the compare runs endpoint."""

    def test_compare_success(self, app_client):
        """POST /api/benchmarks/compare returns comparison data."""
        mock_result = {
            "runs": {
                "b1": {"benchmark_id": "b1", "recipe_name": "m1", "started_at": "2026-05-27T00:00:00Z", "results": {"throughput": 40.0}},
                "b2": {"benchmark_id": "b2", "recipe_name": "m2", "started_at": "2026-05-27T01:00:00Z", "results": {"throughput": 50.0}},
            },
            "comparison": {
                "throughput": {
                    "values": {
                        "b1": {"value": 40.0, "recipe_name": "m1", "started_at": "2026-05-27T00:00:00Z"},
                        "b2": {"value": 50.0, "recipe_name": "m2", "started_at": "2026-05-27T01:00:00Z"},
                    },
                    "differences": {"b1_vs_b2": {"difference_pct": -20.0}},
                }
            },
            "run_ids": ["b1", "b2"],
        }

        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.compare_runs",
            return_value=mock_result,
        ):
            resp = app_client.post(
                "/api/benchmarks/compare",
                json={"run_ids": ["b1", "b2"]},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["run_ids"] == ["b1", "b2"]
        assert data["comparison"]["throughput"]["differences"]["b1_vs_b2"]["difference_pct"] == -20.0

    def test_compare_too_few_runs(self, app_client):
        """POST /api/benchmarks/compare with <2 IDs returns 400."""
        resp = app_client.post(
            "/api/benchmarks/compare",
            json={"run_ids": ["b1"]},
        )

        assert resp.status_code == 400
        data = resp.json()
        assert "at least 2" in data["detail"].lower()

    def test_compare_missing_runs(self, app_client):
        """POST /api/benchmarks/compare with missing IDs returns 404."""
        with patch(
            "spark_pulse.routers.benchmarking.tools.benchmarking.compare_runs",
            return_value=None,
        ):
            resp = app_client.post(
                "/api/benchmarks/compare",
                json={"run_ids": ["b1", "b2"]},
            )

        assert resp.status_code == 404
        data = resp.json()
        assert "not found" in data["detail"]

    def test_compare_empty_run_ids(self, app_client):
        """POST /api/benchmarks/compare with empty IDs list returns 400."""
        resp = app_client.post(
            "/api/benchmarks/compare",
            json={"run_ids": []},
        )

        assert resp.status_code == 400


# ── Test: Settings include benchmarking_enabled ──────────────────────────────


class TestSettingsBenchmarkingField:
    """Test that settings API includes benchmarking_enabled."""

    def test_settings_includes_benchmarking_enabled(self, app_client):
        """Settings should include benchmarking_enabled field."""
        resp = app_client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "benchmarking_enabled" in data
        assert isinstance(data["benchmarking_enabled"], bool)

    def test_config_includes_benchmarking_enabled(self, app_client):
        """Config endpoint should include benchmarking_enabled."""
        resp = app_client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "benchmarking_enabled" in data
        assert isinstance(data["benchmarking_enabled"], bool)
