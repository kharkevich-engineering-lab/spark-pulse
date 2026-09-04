"""Unit tests for the simulation-mode benchmarking tool.

pytest-env forces SIMULATION_MODE=1, so ``from spark_pulse.tools import
benchmarking`` — and every ``"spark_pulse.tools.benchmarking..."`` patch target
below — resolves to ``spark_pulse/mock/benchmarking.py``.  This file therefore
exercises the *mock*: records, execution, list operations and comparison logic
as simulation mode implements them.  The real module is covered by
``tests/test_tools_benchmarking_real.py``.

Usage:
    pytest tests/test_tools_benchmarking.py -v
"""

from __future__ import annotations

import json
import sys
import time
from unittest import mock

import pytest

# ── Relative timestamps ──────────────────────────────────────────────────────
#
# The retention window is 90 days, so fixture timestamps must be relative to
# now — hardcoded dates silently start failing once they age past the window.


def _ago(days: float) -> str:
    """An ISO-8601 UTC timestamp `days` days before now."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))


_DAY1 = _ago(3)  # oldest of the three recent fixtures
_DAY2 = _ago(2)
_DAY3 = _ago(1)  # newest
_DAY3_LATER = _ago(1 - 1 / 24)  # an hour after _DAY3
_LONG_AGO = _ago(400)  # well outside the retention window


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def _bench_path(monkeypatch, tmp_path):
    """Point the benchmarks file at a temp location and reset cache."""
    monkeypatch.setattr(
        "spark_pulse.tools.benchmarking._BENCHMARKS_PATH",
        tmp_path / "benchmarks.json",
    )
    # Also reset the in-memory cache so tests don't see stale data
    from spark_pulse.tools import benchmarking

    benchmarking._reset_cache()
    monkeypatch.setattr(
        "spark_pulse.tools.benchmarking._RETENTION_DAYS",
        90,
    )
    return tmp_path


@pytest.fixture()
def mock_llama_benchy(monkeypatch):
    """Inject a mock llama_benchy into sys.modules."""
    mock_llama_benchy = mock.MagicMock()
    mock_llama_benchy.run = mock.MagicMock(
        return_value={
            "throughput": 42.0,
            "latency_ms": 10.0,
        }
    )
    monkeypatch.setitem(sys.modules, "llama_benchy", mock_llama_benchy)
    return mock_llama_benchy


# ── create_benchmark ─────────────────────────────────────────────────────────


class TestCreateBenchmark:
    """Test the create_benchmark function."""

    def test_create_returns_id_and_running_status(self, monkeypatch, _bench_path):
        """create_benchmark returns a dict with benchmark_id and status='running'."""
        from spark_pulse.tools import benchmarking

        result = benchmarking.create_benchmark(
            deployment_id="dep-123",
            params={"model": "test"},
            recipe_id="qwen3.5-397b",
            recipe_name="Qwen 3.5 397B",
        )

        assert "benchmark_id" in result
        assert result["status"] == "running"
        assert result["deployment_id"] == "dep-123"
        assert result["recipe_id"] == "qwen3.5-397b"
        assert result["completed_at"] is None
        assert result["params"] == {"model": "test"}

    def test_create_persists_to_disk(self, monkeypatch, _bench_path):
        """create_benchmark persists the record to the benchmarks file."""
        from spark_pulse.tools import benchmarking

        benchmarking.create_benchmark("dep-1", recipe_id="r1", recipe_name="Test")

        path = _bench_path / "benchmarks.json"
        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["deployment_id"] == "dep-1"
        assert data[0]["recipe_id"] == "r1"

    def test_create_with_baseline(self, monkeypatch, _bench_path):
        """create_benchmark stores baseline_id when provided."""
        from spark_pulse.tools import benchmarking

        result = benchmarking.create_benchmark(
            "dep-1",
            baseline_id="baseline-abc",
        )
        assert result["baseline_id"] == "baseline-abc"

    def test_create_default_params_empty(self, monkeypatch, _bench_path):
        """create_benchmark defaults params to empty dict when not provided."""
        from spark_pulse.tools import benchmarking

        result = benchmarking.create_benchmark("dep-1")
        assert result["params"] == {}


# ── execute_benchmark ────────────────────────────────────────────────────────


class TestExecuteBenchmark:
    """Test the execute_benchmark function."""

    def test_execute_completes_with_mock_llama_benchy(
        self, monkeypatch, _bench_path, mock_llama_benchy
    ):
        """execute_benchmark sets status='completed' and stores results."""
        from spark_pulse.tools import benchmarking

        record = benchmarking.create_benchmark(
            "dep-1",
            params={"model": "m", "benchmarks": ["throughput"]},
        )
        benchmark_id = record["benchmark_id"]

        assert record["status"] == "running"

        benchmarking.execute_benchmark(benchmark_id)

        updated = benchmarking.get_benchmark(benchmark_id)
        assert updated is not None
        assert updated["status"] == "completed"
        assert updated["completed_at"] is not None
        assert updated["results"] == {"throughput": 42.0, "latency_ms": 10.0}

    def test_execute_missing_benchmark_id(self, monkeypatch, _bench_path):
        """execute_benchmark silently does nothing for missing ID."""
        from spark_pulse.tools import benchmarking

        # Should not raise
        benchmarking.execute_benchmark("nonexistent-id")

        # File should be unchanged (still empty or whatever it was)
        path = _bench_path / "benchmarks.json"
        if path.exists():
            data = json.loads(path.read_text())
            assert len(data) == 0

    def test_execute_records_error_when_llama_benchy_fails(
        self, monkeypatch, _bench_path
    ):
        """execute_benchmark sets status='error' when llama_benchy raises."""
        mock_llama_benchy = mock.MagicMock()
        mock_llama_benchy.run = mock.MagicMock(
            side_effect=RuntimeError("connection refused")
        )
        monkeypatch.setitem(sys.modules, "llama_benchy", mock_llama_benchy)

        from spark_pulse.tools import benchmarking

        record = benchmarking.create_benchmark("dep-1", params={"model": "m"})
        benchmarking.execute_benchmark(record["benchmark_id"])

        updated = benchmarking.get_benchmark(record["benchmark_id"])
        assert updated["status"] == "error"
        assert "connection refused" in updated["results"]["error"]

    def test_execute_missing_llama_benchy_stores_error(self, monkeypatch, _bench_path):
        """execute_benchmark stores error status when llama_benchy is not installed."""
        monkeypatch.delitem(sys.modules, "llama_benchy", raising=False)

        from spark_pulse.tools import benchmarking

        record = benchmarking.create_benchmark("dep-1")
        benchmarking.execute_benchmark(record["benchmark_id"])

        updated = benchmarking.get_benchmark(record["benchmark_id"])
        assert updated["status"] == "error"
        assert "llama-benchy is not installed" in updated["results"]["error"]


# ── list_benchmarks ──────────────────────────────────────────────────────────


class TestListBenchmarks:
    """Test list_benchmarks and related query functions."""

    def test_list_returns_sorted_descending(self, monkeypatch, _bench_path):
        """list_benchmarks returns results sorted by started_at descending."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(
            json.dumps(
                [
                    {"benchmark_id": "b2", "started_at": _DAY2},
                    {"benchmark_id": "b1", "started_at": _DAY1},
                    {"benchmark_id": "b3", "started_at": _DAY3},
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        result = benchmarking.list_benchmarks()
        ids = [b["benchmark_id"] for b in result]
        assert ids == ["b3", "b2", "b1"]

    def test_list_empty_store(self, monkeypatch, _bench_path):
        """list_benchmarks returns empty list for missing file."""
        from spark_pulse.tools import benchmarking

        assert benchmarking.list_benchmarks() == []

    def test_get_benchmark_by_id(self, monkeypatch, _bench_path):
        """get_benchmark retrieves a single benchmark by ID."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(
            json.dumps(
                [
                    {
                        "benchmark_id": "abc",
                        "deployment_id": "d1",
                        "started_at": _DAY3,
                    },
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        result = benchmarking.get_benchmark("abc")
        assert result is not None
        assert result["deployment_id"] == "d1"

    def test_get_benchmark_not_found(self, monkeypatch, _bench_path):
        """get_benchmark returns None for missing ID."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(json.dumps([]))

        from spark_pulse.tools import benchmarking

        assert benchmarking.get_benchmark("missing") is None

    def test_get_benchmarks_for_recipe(self, monkeypatch, _bench_path):
        """get_benchmarks_for_recipe filters by recipe_id."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(
            json.dumps(
                [
                    {
                        "benchmark_id": "b1",
                        "recipe_id": "r1",
                        "started_at": _DAY1,
                    },
                    {
                        "benchmark_id": "b2",
                        "recipe_id": "r2",
                        "started_at": _DAY1,
                    },
                    {
                        "benchmark_id": "b3",
                        "recipe_id": "r1",
                        "started_at": _DAY1,
                    },
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        result = benchmarking.get_benchmarks_for_recipe("r1")
        assert len(result) == 2
        assert all(b["recipe_id"] == "r1" for b in result)


# ── get_latest_by_recipe ─────────────────────────────────────────────────────


class TestGetLatestByRecipe:
    """Test latest-by-recipe query functions."""

    def test_get_latest_by_recipe_returns_latest_per_recipe(
        self, monkeypatch, _bench_path
    ):
        """get_latest_by_recipe returns one completed benchmark per recipe."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(
            json.dumps(
                [
                    {
                        "benchmark_id": "b1",
                        "recipe_id": "r1",
                        "status": "completed",
                        "started_at": _DAY1,
                        "results": {"throughput": 40},
                    },
                    {
                        "benchmark_id": "b2",
                        "recipe_id": "r1",
                        "status": "completed",
                        "started_at": _DAY3,
                        "results": {"throughput": 45},
                    },
                    {
                        "benchmark_id": "b3",
                        "recipe_id": "r2",
                        "status": "completed",
                        "started_at": _DAY2,
                        "results": {"throughput": 30},
                    },
                    # No results — should be skipped
                    {
                        "benchmark_id": "b4",
                        "recipe_id": "r3",
                        "status": "running",
                        "started_at": _DAY3,
                        "results": None,
                    },
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        result = benchmarking.get_latest_by_recipe()
        assert set(result.keys()) == {"r1", "r2"}
        assert result["r1"]["benchmark_id"] == "b2"  # most recent
        assert result["r2"]["benchmark_id"] == "b3"

    def test_get_recipe_latest(self, monkeypatch, _bench_path):
        """get_recipe_latest returns the latest completed benchmark for a recipe."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(
            json.dumps(
                [
                    {
                        "benchmark_id": "b1",
                        "recipe_id": "r1",
                        "status": "completed",
                        "started_at": _DAY1,
                        "results": {"t": 40},
                    },
                    {
                        "benchmark_id": "b2",
                        "recipe_id": "r1",
                        "status": "completed",
                        "started_at": _DAY3,
                        "results": {"t": 45},
                    },
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        result = benchmarking.get_recipe_latest("r1")
        assert result is not None
        assert result["benchmark_id"] == "b2"

    def test_get_recipe_latest_no_results(self, monkeypatch, _bench_path):
        """get_recipe_latest returns None when no completed benchmarks exist."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(
            json.dumps(
                [
                    {
                        "benchmark_id": "b1",
                        "recipe_id": "r1",
                        "status": "running",
                        "started_at": _DAY3,
                        "results": None,
                    },
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        assert benchmarking.get_recipe_latest("r1") is None


# ── compare_runs ─────────────────────────────────────────────────────────────


class TestCompareRuns:
    """Test the compare_runs function."""

    def test_compare_returns_differences(self, monkeypatch, _bench_path):
        """compare_runs returns values and pairwise differences."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(
            json.dumps(
                [
                    {
                        "benchmark_id": "b1",
                        "recipe_name": "m1",
                        "started_at": _DAY3,
                        "results": {"throughput": 40.0, "latency_ms": 10.0},
                    },
                    {
                        "benchmark_id": "b2",
                        "recipe_name": "m2",
                        "started_at": _DAY3_LATER,
                        "results": {"throughput": 50.0, "latency_ms": 8.0},
                    },
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        result = benchmarking.compare_runs(["b1", "b2"])
        assert result is not None
        assert set(result["run_ids"]) == {"b1", "b2"}
        assert "throughput" in result["comparison"]
        assert "latency_ms" in result["comparison"]

        # Check throughput difference
        tp = result["comparison"]["throughput"]
        assert tp["values"]["b1"]["value"] == 40.0
        assert tp["values"]["b2"]["value"] == 50.0
        # b1_vs_b2 = (40 - 50) / 50 * 100 = -20.0
        assert tp["differences"]["b1_vs_b2"]["difference_pct"] == -20.0

    def test_compare_missing_run_id(self, monkeypatch, _bench_path):
        """compare_runs returns None when any run_id is not found."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(
            json.dumps(
                [
                    {
                        "benchmark_id": "b1",
                        "recipe_name": "m1",
                        "started_at": _DAY3,
                        "results": {"throughput": 40.0},
                    },
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        assert benchmarking.compare_runs(["b1", "missing"]) is None

    def test_compare_run_without_results(self, monkeypatch, _bench_path):
        """compare_runs returns None when a run has no results."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(
            json.dumps(
                [
                    {
                        "benchmark_id": "b1",
                        "recipe_name": "m1",
                        "started_at": _DAY3,
                        "results": {"throughput": 40.0},
                    },
                    {
                        "benchmark_id": "b2",
                        "recipe_name": "m2",
                        "started_at": _DAY3_LATER,
                        "results": None,
                    },
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        assert benchmarking.compare_runs(["b1", "b2"]) is None

    def test_compare_single_run(self, monkeypatch, _bench_path):
        """compare_runs returns None for a single run."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(
            json.dumps(
                [
                    {
                        "benchmark_id": "b1",
                        "recipe_name": "m1",
                        "started_at": _DAY3,
                        "results": {"throughput": 40.0},
                    },
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        assert benchmarking.compare_runs(["b1"]) is None


# ── get_baseline_comparison ──────────────────────────────────────────────────


class TestGetBaselineComparison:
    """Test the get_baseline_comparison function."""

    def test_baseline_comparison_returns_differences(self, monkeypatch, _bench_path):
        """get_baseline_comparison computes diffs between a benchmark and its baseline."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(
            json.dumps(
                [
                    {
                        "benchmark_id": "baseline",
                        "recipe_name": "m1",
                        "started_at": _DAY3,
                        "results": {"throughput": 40.0},
                    },
                    {
                        "benchmark_id": "current",
                        "baseline_id": "baseline",
                        "recipe_name": "m2",
                        "started_at": _DAY3_LATER,
                        "results": {"throughput": 50.0},
                    },
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        result = benchmarking.get_baseline_comparison("current")
        assert result is not None
        assert result["throughput"]["current"] == 50.0
        assert result["throughput"]["baseline"] == 40.0
        # (50 - 40) / 40 * 100 = 25.0
        assert result["throughput"]["difference_pct"] == 25.0

    def test_baseline_comparison_no_baseline_id(self, monkeypatch, _bench_path):
        """get_baseline_comparison returns None when benchmark has no baseline_id."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(
            json.dumps(
                [
                    {
                        "benchmark_id": "b1",
                        "baseline_id": None,
                        "recipe_name": "m1",
                        "started_at": _DAY3,
                        "results": {"throughput": 40.0},
                    },
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        assert benchmarking.get_baseline_comparison("b1") is None

    def test_baseline_comparison_missing_baseline(self, monkeypatch, _bench_path):
        """get_baseline_comparison returns None when baseline benchmark not found."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(
            json.dumps(
                [
                    {
                        "benchmark_id": "b1",
                        "baseline_id": "nonexistent",
                        "recipe_name": "m1",
                        "started_at": _DAY3,
                        "results": {"throughput": 40.0},
                    },
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        assert benchmarking.get_baseline_comparison("b1") is None


# ── _purge_expired ───────────────────────────────────────────────────────────


class TestPurgeExpired:
    """Test the _purge_expired function."""

    def test_purge_removes_old_records(self, monkeypatch, _bench_path):
        """_purge_expired removes records older than retention days."""
        bench_file = _bench_path / "benchmarks.json"
        bench_file.write_text(
            json.dumps(
                [
                    {
                        "benchmark_id": "old",
                        "started_at": _LONG_AGO,  # well over 90 days ago
                        "results": {"throughput": 40.0},
                    },
                    {
                        "benchmark_id": "new",
                        "started_at": _DAY3,
                        "results": {"throughput": 50.0},
                    },
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        result = benchmarking.list_benchmarks()
        assert len(result) == 1
        assert result[0]["benchmark_id"] == "new"

    def test_purge_keeps_recent_records(self, monkeypatch, _bench_path):
        """_purge_expired keeps records within retention window."""
        bench_file = _bench_path / "benchmarks.json"
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        bench_file.write_text(
            json.dumps(
                [
                    {
                        "benchmark_id": "recent",
                        "started_at": recent,
                        "results": {"throughput": 40.0},
                    },
                ]
            )
        )

        from spark_pulse.tools import benchmarking

        result = benchmarking.list_benchmarks()
        assert len(result) == 1
