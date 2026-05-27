"""Mock benchmarking — returns realistic pre-canned results for simulation mode."""

import json
import logging
import random
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

logger = logging.getLogger(__name__)

_BENCHMARKS_PATH = Path(__file__).resolve().parent.parent / "data" / "benchmarks.json"
_BENCHMARKS_LOCK = FileLock(f"{_BENCHMARKS_PATH}.lock", timeout=30)

# Realistic pre-canned benchmark results keyed by model family
_BENCHMARK_MODELS: dict[str, dict] = {
    "qwen": {
        "throughput": 45.2,
        "latency_ms": 12.3,
        "decode_latency_ms": 7.1,
        "gpu_memory_gb": 67.4,
        "gpu_utilization": 94.2,
        "prefill_speed": 320.5,
    },
    "gpt": {
        "throughput": 52.8,
        "latency_ms": 10.1,
        "decode_latency_ms": 5.9,
        "gpu_memory_gb": 58.1,
        "gpu_utilization": 91.7,
        "prefill_speed": 385.2,
    },
    "minimax": {
        "throughput": 38.5,
        "latency_ms": 14.7,
        "decode_latency_ms": 8.3,
        "gpu_memory_gb": 72.3,
        "gpu_utilization": 88.9,
        "prefill_speed": 290.1,
    },
    "glm": {
        "throughput": 41.3,
        "latency_ms": 13.2,
        "decode_latency_ms": 7.8,
        "gpu_memory_gb": 62.8,
        "gpu_utilization": 90.5,
        "prefill_speed": 310.4,
    },
}


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


def _pick_model_name(recipe_id: str, recipe_name: str) -> dict:
    """Pick canned results based on recipe model name."""
    text = (recipe_id + " " + recipe_name).lower()
    for key, result in _BENCHMARK_MODELS.items():
        if key in text:
            return result
    return _BENCHMARK_MODELS["qwen"]


def run_benchmark(
    deployment_id: str,
    baseline_id: str | None = None,
    params: dict | None = None,
    recipe_id: str = "",
    recipe_name: str = "",
) -> dict:
    """Run a mock benchmark — returns completed results immediately."""
    params = params or {}
    benchmark_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    model_results = _pick_model_name(recipe_id, recipe_name)
    variation = 0.95 + random.random() * 0.1

    benchmark_record: dict = {
        "benchmark_id": benchmark_id,
        "deployment_id": deployment_id,
        "recipe_id": recipe_id,
        "recipe_name": recipe_name,
        "baseline_id": baseline_id,
        "status": "completed",
        "started_at": now,
        "completed_at": now,
        "params": params,
        "results": {
            "throughput": round(model_results["throughput"] * variation, 2),
            "latency_ms": round(model_results["latency_ms"] * variation, 2),
            "decode_latency_ms": round(model_results["decode_latency_ms"] * variation, 2),
            "gpu_memory_gb": round(model_results["gpu_memory_gb"] * variation, 1),
            "gpu_utilization": round(model_results["gpu_utilization"] * variation, 1),
            "prefill_speed": round(model_results["prefill_speed"] * variation, 1),
        },
    }

    with _atomic_benchmarks() as benchmarks:
        benchmarks.append(benchmark_record)
        logger.info("Mock benchmark %s completed for deployment %s", benchmark_id, deployment_id)

    return benchmark_record


def create_benchmark(
    deployment_id: str,
    baseline_id: str | None = None,
    params: dict | None = None,
    recipe_id: str = "",
    recipe_name: str = "",
) -> dict:
    """Mock: creates and immediately completes a benchmark record."""
    logger.info("Mock create_benchmark called for deployment %s", deployment_id)
    return run_benchmark(deployment_id, baseline_id, params, recipe_id, recipe_name)


def execute_benchmark(benchmark_id: str) -> None:
    """Mock: no-op — benchmark is already completed during create_benchmark."""


def list_benchmarks() -> list[dict]:
    """Return all mock benchmarks sorted by started_at descending."""
    data = _load()
    data.sort(key=lambda b: b["started_at"], reverse=True)
    return data


def get_benchmark(benchmark_id: str) -> dict | None:
    """Return a single mock benchmark by ID."""
    for b in list_benchmarks():
        if b["benchmark_id"] == benchmark_id:
            return b
    return None


def get_benchmarks_for_recipe(recipe_id: str) -> list[dict]:
    """Return all mock benchmarks for a recipe."""
    benchmarks = list_benchmarks()
    return [b for b in benchmarks if b.get("recipe_id") == recipe_id]


def get_recipe_latest(recipe_id: str) -> dict | None:
    """Return the latest completed benchmark for a recipe."""
    benchmarks = get_benchmarks_for_recipe(recipe_id)
    for b in benchmarks:
        if b.get("status") == "completed" and b.get("results"):
            return b
    return None


def get_latest_by_recipe() -> dict[str, dict]:
    """Return the latest completed benchmark for each recipe."""
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
    """Compare multiple benchmark runs against each other."""
    runs: dict[str, dict] = {}
    for bid in ids:
        b = get_benchmark(bid)
        if b is None or not b.get("results"):
            return None
        runs[bid] = b

    if len(runs) < 2:
        return None

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
    """Compute baseline comparison for a mock benchmark."""
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
