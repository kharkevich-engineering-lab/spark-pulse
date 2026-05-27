"""Benchmarking tools — run llama-benchy against model deployments and store results.

Benchmarks are tracked per recipe/model, enabling historical comparison
across different configurations and runs.
"""

import json
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

logger = logging.getLogger(__name__)

_BENCHMARKS_PATH = Path.home() / ".config" / "spark-pulse" / "benchmarks.json"
_BENCHMARKS_LOCK = FileLock(f"{_BENCHMARKS_PATH}.lock", timeout=30)
_RETENTION_DAYS = 90


def _load() -> list[dict]:
    if not _BENCHMARKS_PATH.exists():
        return []
    try:
        with open(_BENCHMARKS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(data: list[dict]) -> None:
    _BENCHMARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _BENCHMARKS_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(_BENCHMARKS_PATH)


@contextmanager
def _atomic_benchmarks():
    """Context manager for atomic read-modify-write of the benchmarks file."""
    with _BENCHMARKS_LOCK:
        data = _load()
        yield data
        _save(data)


def _purge_expired(data: list[dict]) -> list[dict]:
    if not data:
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - _RETENTION_DAYS * 86400
    before = len(data)
    data = [
        b for b in data
        if datetime.fromisoformat(b["started_at"]).timestamp() > cutoff
    ]
    purged = before - len(data)
    if purged > 0:
        logger.info("Purged %d expired benchmark records (>%d days old)", purged, _RETENTION_DAYS)
    return data


def create_benchmark(
    deployment_id: str,
    baseline_id: str | None = None,
    params: dict | None = None,
    recipe_id: str = "",
    recipe_name: str = "",
) -> dict:
    """Create a benchmark record with status='running' and persist it."""
    benchmark_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    record: dict = {
        "benchmark_id": benchmark_id,
        "deployment_id": deployment_id,
        "recipe_id": recipe_id,
        "recipe_name": recipe_name,
        "baseline_id": baseline_id,
        "status": "running",
        "started_at": now,
        "completed_at": None,
        "params": params or {},
        "results": None,
    }

    with _atomic_benchmarks() as benchmarks:
        benchmarks.append(record)
        logger.info("Created benchmark %s for deployment %s (recipe: %s)", benchmark_id, deployment_id, recipe_name)

    return record


def execute_benchmark(benchmark_id: str) -> None:
    """Load a 'running' benchmark record, run llama-benchy, and update its status."""
    logger.info("Executing benchmark %s", benchmark_id)

    with _atomic_benchmarks() as benchmarks:
        record = next((b for b in benchmarks if b["benchmark_id"] == benchmark_id), None)
        if record is None:
            logger.warning("Benchmark %s not found for execution", benchmark_id)
            return

        record["status"] = "running"
        params = record.get("params", {})

        try:
            try:
                import llama_benchy  # noqa: PLC0415
            except ImportError:
                raise RuntimeError(
                    "llama-benchy is not installed. Install it with: "
                    "pip install spark-pulse[benchmarking]"
                )

            bench_results = llama_benchy.run(
                target=f"http://localhost:{params.get('port', 8000)}",
                model_name=params.get("model", "unknown"),
                benchmarks=params.get("benchmarks", ["throughput", "latency"]),
                context_length=params.get("context_length", 4096),
            )

            record["status"] = "completed"
            record["completed_at"] = datetime.now(timezone.utc).isoformat()
            record["results"] = bench_results
            logger.info("Benchmark %s completed successfully", benchmark_id)

        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            record["status"] = "error"
            record["completed_at"] = datetime.now(timezone.utc).isoformat()
            record["results"] = {"error": str(e)}
            logger.error("Benchmark %s failed: %s", benchmark_id, e)


def list_benchmarks() -> list[dict]:
    """Return all benchmarks sorted by started_at descending."""
    data = _load()
    data = _purge_expired(data)
    data.sort(key=lambda b: b["started_at"], reverse=True)
    return data


def get_benchmark(benchmark_id: str) -> dict | None:
    """Return a single benchmark by ID, or None if not found."""
    for b in list_benchmarks():
        if b["benchmark_id"] == benchmark_id:
            return b
    return None


def get_benchmarks_for_recipe(recipe_id: str) -> list[dict]:
    """Return all benchmarks for a specific recipe, sorted by date descending."""
    benchmarks = list_benchmarks()
    return [
        b for b in benchmarks
        if b.get("recipe_id") == recipe_id
    ]


def get_recipe_latest(recipe_id: str) -> dict | None:
    """Return the latest completed benchmark for a recipe, or None."""
    recipes = get_benchmarks_for_recipe(recipe_id)
    for b in recipes:
        if b.get("status") == "completed" and b.get("results"):
            return b
    return None


def get_latest_by_recipe() -> dict[str, dict]:
    """Return the latest completed benchmark for each recipe.

    Returns a dict keyed by recipe_id.
    """
    benchmarks = list_benchmarks()
    latest: dict[str, dict] = {}
    for b in benchmarks:
        rid = b.get("recipe_id")
        if not rid or not b.get("results"):
            continue
        if b.get("status") != "completed":
            continue
        if rid not in latest:
            latest[rid] = b
    return latest


def compare_runs(ids: list[str]) -> dict | None:
    """Compare multiple benchmark runs against each other.

    Returns a dict with all runs and their pairwise differences.
    Returns None if any run is not found or has no results.
    """
    runs: dict[str, dict] = {}
    for bid in ids:
        b = get_benchmark(bid)
        if b is None or not b.get("results"):
            return None
        runs[bid] = b

    if len(runs) < 2:
        return None

    # Build comparison matrix
    comparison: dict[str, dict[str, Any]] = {}
    all_metric_keys: set[str] = set()
    for b in runs.values():
        if b.get("results"):
            all_metric_keys.update(b["results"].keys())

    for metric in all_metric_keys:
        if metric == "error":
            continue
        values: dict[str, dict] = {}
        for bid, b in runs.items():
            val = b.get("results", {}).get(metric)
            if isinstance(val, (int, float)):
                values[bid] = {
                    "value": val,
                    "recipe_name": b.get("recipe_name", ""),
                    "started_at": b["started_at"],
                }

        # Add pairwise differences
        if len(values) >= 2:
            ids_list = list(values.keys())
            differences: dict[str, dict[str, float]] = {}
            for i in range(len(ids_list)):
                for j in range(i + 1, len(ids_list)):
                    a, b = ids_list[i], ids_list[j]
                    v_a = values[a]["value"]
                    v_b = values[b]["value"]
                    if v_b != 0:
                        diff_pct = ((v_a - v_b) / v_b) * 100
                        differences[f"{a}_vs_{b}"] = {
                            "difference_pct": round(diff_pct, 2),
                        }
            comparison[metric] = {"values": values, "differences": differences}

    return {
        "runs": runs,
        "comparison": comparison,
        "run_ids": list(runs.keys()),
    }


def get_baseline_comparison(benchmark_id: str) -> dict | None:
    """Given a benchmark ID, look up its baseline and compute difference."""
    benchmark = get_benchmark(benchmark_id)
    if not benchmark or not benchmark.get("baseline_id"):
        return None

    baseline = get_benchmark(benchmark["baseline_id"])
    if not baseline or not baseline.get("results"):
        return None

    results = benchmark.get("results") or {}
    baseline_results = baseline.get("results") or {}

    comparison: dict[str, dict] = {}
    for key in set(list(results.keys()) + list(baseline_results.keys())):
        if key == "error":
            continue
        current_val = results.get(key)
        baseline_val = baseline_results.get(key)
        if isinstance(current_val, (int, float)) and isinstance(baseline_val, (int, float)) and baseline_val != 0:
            diff_pct = ((current_val - baseline_val) / baseline_val) * 100
            comparison[key] = {
                "current": current_val,
                "baseline": baseline_val,
                "difference_pct": round(diff_pct, 2),
            }

    return comparison
