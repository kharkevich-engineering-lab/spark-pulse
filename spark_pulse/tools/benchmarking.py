"""Benchmarking tools — run llama-benchy against model deployments and store results.

Benchmarks are tracked per recipe/model, enabling historical comparison
across different configurations and runs.
"""

from __future__ import annotations


import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from sqlalchemy import JSON, String, select
from sqlalchemy.orm import Mapped, mapped_column

from sqlalchemy.exc import IntegrityError

from spark_pulse.db import Base, is_done, mark_done_within, session_scope

logger = logging.getLogger(__name__)

_BENCHMARKS_PATH = Path.home() / ".config" / "spark-pulse" / "benchmarks.json"
_BENCHMARKS_LOCK = FileLock(f"{_BENCHMARKS_PATH}.lock", timeout=30)
_RETENTION_DAYS = 90

# ── In-memory cache ──────────────────────────────────────────────────────────

# Cached data: {benchmark_id: record}
_bench_cache: dict[str, dict] = {}
_bench_cache_lock = threading.RLock()  # Reentrant to allow nested lock acquisition
_bench_cache_dirty = True  # True when cache needs reload from disk


def _ensure_cache_loaded() -> None:
    """Load benchmarks from disk into memory if dirty."""
    global _bench_cache_dirty
    with _bench_cache_lock:
        if not _bench_cache_dirty:
            return
        data = _load()
        _bench_cache.clear()
        _bench_cache.update({b["benchmark_id"]: b for b in data})
        _bench_cache_dirty = False


def _reset_cache() -> None:
    """Reset the in-memory cache. Used by tests."""
    global _bench_cache, _bench_cache_dirty
    with _bench_cache_lock:
        _bench_cache.clear()
        _bench_cache_dirty = True


class _BenchmarkRow(Base):
    """One benchmark result."""

    __tablename__ = "benchmarks"

    benchmark_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    record: Mapped[dict] = mapped_column(JSON, default=dict)


#: Recorded once ``benchmarks.json`` has been imported. See the deployment
#: store for why this is a row rather than "are the tables empty".
_IMPORT_KEY = "benchmarks.imported_from_json"


def _migrate_from_json() -> None:
    if is_done(_IMPORT_KEY):
        return
    if not _BENCHMARKS_PATH.exists():
        return
    try:
        with open(_BENCHMARKS_PATH) as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, list):
        return
    # Marker and rows in one transaction, and ``get``-then-write rather than
    # ``merge`` (see the house rule in :mod:`spark_pulse.db`). Marking in its
    # own transaction leaves a window in which the marker says the file was
    # taken while none of it was — and the marker is what stops it being
    # retried, so the file would be discarded for good.
    try:
        with session_scope() as db:
            if not mark_done_within(db, _IMPORT_KEY):
                return
            for record in data:
                if not (isinstance(record, dict) and record.get("benchmark_id")):
                    continue
                key = str(record["benchmark_id"])
                row = db.get(_BenchmarkRow, key)
                if row is None:
                    db.add(_BenchmarkRow(benchmark_id=key, record=record))
                else:
                    _apply(row, record)
    except IntegrityError:
        # Another instance claimed the import between our read and our insert.
        # Our whole transaction rolled back; theirs stands.
        return


def _apply(row: _BenchmarkRow, record: dict) -> bool:
    """Copy ``record`` onto an existing row; ``False`` if nothing differed.

    Assigning an equal-but-distinct dict to a JSON column still marks the
    attribute dirty, so without this comparison a whole-set save that changed
    one benchmark emits an UPDATE for every other one — on PostgreSQL a dead
    tuple and a WAL record per untouched benchmark, on every backend a write
    that grows with the size of the history rather than the size of the change.
    """
    if row.record == record:
        return False
    row.record = record
    return True


def _transaction(*, timeout: float | None = None) -> AbstractContextManager[Any]:
    """Serialise a read-modify-write of the benchmark set.

    Every caller that reads records, changes them and writes them back holds
    this for the whole sequence — not merely around the write, which is where
    the transaction already is. A single write being one transaction is a
    different guarantee and not the one that was missing: two callers that
    each *load, change, save* still lose one of the changes, because the
    second writes back a list it read before the first landed. The whole-set
    path makes that worse than a lost field — it deletes every row absent from
    the list it loaded, so a benchmark created while it was running is not
    stale, it is gone. The per-row writers take the same lock for exactly that
    reason.

    ``_BENCHMARKS_LOCK`` rather than a mutex because it is the one of the two
    that also holds when a second process — a CLI invocation beside the
    server — writes the same database. ``filelock`` counts acquisitions per
    thread, so it is reentrant for the helpers that call one another and still
    exclusive between threads.

    ``timeout`` overrides the lock's own for one acquisition; ``0`` means
    "take it or raise", which is how a read path declines to wait behind a
    running benchmark.
    """
    if timeout is None:
        return _BENCHMARKS_LOCK
    return _BENCHMARKS_LOCK.acquire(timeout=timeout)


# ── The whole set ────────────────────────────────────────────────────────────


def _load() -> list[dict]:
    """Every stored record.

    Ordered by id, so the sequence is the same on both backends. Without an
    ``ORDER BY`` a row order is whatever the storage engine last did to the
    heap, which is not a property to let ``list_benchmarks``' tie-breaking
    depend on.
    """
    _migrate_from_json()
    with session_scope() as db:
        rows = db.execute(select(_BenchmarkRow).order_by(_BenchmarkRow.benchmark_id))
        return [dict(row.record) for row in rows.scalars()]


def _save(data: list[dict]) -> None:
    """Replace the whole set, in one transaction.

    Kept because callers still hold the whole list, and because "the set is
    now exactly this" — records absent from ``data`` are removed — is a
    guarantee :func:`_upsert_row` deliberately does not make. What it no
    longer does is *write* the whole set: only rows that actually differ are
    updated, so saving a list in which one benchmark changed costs one row
    rather than the table.
    """
    wanted = {
        str(r["benchmark_id"]): r
        for r in data
        if isinstance(r, dict) and r.get("benchmark_id")
    }
    with _transaction():
        # Before the write, not after: an import that ran later would merge
        # the old JSON file back in and resurrect every record this save had
        # just removed.
        _migrate_from_json()
        with session_scope() as db:
            stored = {
                row.benchmark_id: row
                for row in db.execute(select(_BenchmarkRow)).scalars()
            }
            for benchmark_id, row in stored.items():
                if benchmark_id not in wanted:
                    db.delete(row)
            for benchmark_id, record in wanted.items():
                row = stored.get(benchmark_id)
                if row is None:
                    db.add(_BenchmarkRow(benchmark_id=benchmark_id, record=record))
                else:
                    _apply(row, record)


@contextmanager
def _atomic_benchmarks():
    """Context manager for atomic read-modify-write of the whole benchmark set."""
    global _bench_cache_dirty
    with _transaction():
        data = _load()
        yield data
        _save(data)
        _bench_cache_dirty = True


# ── One row at a time ────────────────────────────────────────────────────────
#
# Everything above treats the benchmark history as one value. That is the right
# shape for a caller that genuinely rewrote it, and the wrong one for the case
# that dominates here: one benchmark being created, and that same benchmark
# finishing. Rewriting ninety days of history to record either is what these
# replace.


def _get_row(benchmark_id: str) -> dict | None:
    """One record by id, read by primary key rather than by loading the table."""
    _migrate_from_json()
    with session_scope() as db:
        row = db.get(_BenchmarkRow, benchmark_id)
        return dict(row.record) if row is not None else None


def _upsert_row(record: dict) -> None:
    """Store one record, leaving every other row exactly as it was.

    Unlike :func:`_save` this never deletes: a benchmark this call was not
    given is one it has no opinion about, not one to remove. That difference
    is the point — it is what makes the write safe to issue from a caller that
    never loaded the rest of the history.
    """
    benchmark_id = str(record.get("benchmark_id") or "")
    if not benchmark_id:
        # ``_save`` skips these silently because in a whole set they are
        # noise. A per-row write of one is a caller bug that would otherwise
        # look like a successful write and lose the record.
        raise ValueError("a benchmark record needs a benchmark_id to be stored")
    with _transaction():
        _migrate_from_json()
        with session_scope() as db:
            row = db.get(_BenchmarkRow, benchmark_id)
            if row is None:
                db.add(_BenchmarkRow(benchmark_id=benchmark_id, record=record))
            else:
                _apply(row, record)


def _delete_row(benchmark_id: str, *, timeout: float | None = None) -> bool:
    """Drop one record. ``False`` when there was nothing to drop.

    Naming the row is what lets retention express "delete this expired one"
    instead of "the set is now these other forty", which would also delete
    whatever was created while it was deciding.

    ``timeout=0`` asks for the lock without waiting, and raises
    ``filelock.Timeout`` rather than blocking — which is what a *read* path
    wants, since the write mutex is held for the length of a benchmark run.
    """
    with _transaction(timeout=timeout):
        _migrate_from_json()
        with session_scope() as db:
            row = db.get(_BenchmarkRow, benchmark_id)
            if row is None:
                return False
            db.delete(row)
            return True


def _cache_put(record: dict) -> None:
    """Keep the point-read cache in step with a single-row write.

    Marking the whole cache dirty would also be correct, and is what the
    whole-set path does — but then the next ``get_benchmark`` reloads every
    benchmark in order to learn about one, which is the cost the per-row write
    exists to avoid. A cache that has never been loaded is left dirty: there is
    nothing yet to keep in step.
    """
    with _bench_cache_lock:
        if not _bench_cache_dirty:
            _bench_cache[str(record["benchmark_id"])] = record


def _purge_expired(data: list[dict]) -> list[dict]:
    if not data:
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - _RETENTION_DAYS * 86400
    before = len(data)
    data = [
        b for b in data if datetime.fromisoformat(b["started_at"]).timestamp() > cutoff
    ]
    purged = before - len(data)
    if purged > 0:
        logger.info(
            "Purged %d expired benchmark records (>%d days old)",
            purged,
            _RETENTION_DAYS,
        )
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

    # One insert. Appending to the loaded list and saving it back would have
    # rewritten every benchmark ever run to record this one starting.
    _upsert_row(record)
    _cache_put(record)
    logger.info(
        "Created benchmark %s for deployment %s (recipe: %s)",
        benchmark_id,
        deployment_id,
        recipe_name,
    )

    return record


def execute_benchmark(benchmark_id: str) -> None:
    """Load a 'running' benchmark record, run llama-benchy, and update its status."""
    logger.info("Executing benchmark %s", benchmark_id)

    # The mutex spans the run, exactly as it did when this was a whole-set
    # read-modify-write. The record read here is written back when the run
    # ends, so releasing in between would let a concurrent whole-set save
    # delete the benchmark and this call quietly resurrect it.
    with _transaction():
        record = _get_row(benchmark_id)
        if record is None:
            logger.warning("Benchmark %s not found for execution", benchmark_id)
            return

        record["status"] = "running"
        params = record.get("params", {})

        try:
            bench_results = _run_llama_benchy(record, params)

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

        # One row: the benchmark that just finished. The record this writes is
        # the one read above, so it can only clobber fields of the benchmark
        # this call owns.
        _upsert_row(record)
        _cache_put(record)


#: What a benchmark asks of the engine when the request names nothing. Modest
#: on purpose — a run should finish in a couple of minutes on one GB10, and an
#: operator who wants the long tail passes ``pp``/``tg``/``concurrency``.
BENCHY_DEFAULTS: dict[str, Any] = {
    "pp": [512, 2048],
    "tg": [128],
    "runs": 3,
    "concurrency": [1, 4],
    "depth": [0],
    "timeout": 1800,
}


def _benchy_command(base_url: str, out_path: str, params: dict) -> list[str]:
    """The ``llama-benchy`` invocation for one run.

    Invoked as ``python -m llama_benchy`` with this process's interpreter, not
    as the ``llama-benchy`` console script: the script lands in ``~/.local/bin``
    for a user install, which the systemd unit's PATH never has, whereas the
    module is importable wherever ``spark-pulse[benchmarking]`` was installed.
    """

    def _list(key: str) -> list[str]:
        value = params.get(key, BENCHY_DEFAULTS[key])
        if not isinstance(value, (list, tuple)):
            value = [value]
        return [str(int(v)) for v in value]

    cmd = [
        sys.executable,
        "-m",
        "llama_benchy",
        "--base-url",
        base_url,
        "--format",
        "json",
        "--save-result",
        out_path,
        # The coherence probe asks the model to reproduce a passage and grades
        # it; a performance run does not need a grade, and a reasoning model
        # can fail it on style alone.
        "--skip-coherence",
        "--runs",
        str(int(params.get("runs", BENCHY_DEFAULTS["runs"]))),
        "--pp",
        *_list("pp"),
        "--tg",
        *_list("tg"),
        "--depth",
        *_list("depth"),
        "--concurrency",
        *_list("concurrency"),
    ]
    model = str(params.get("model") or "").strip()
    if model and model != "unknown":
        cmd += ["--model", model]
    return cmd


def _mean(entry: dict, key: str) -> float | None:
    stat = entry.get(key)
    if isinstance(stat, dict) and isinstance(stat.get("mean"), (int, float)):
        return float(stat["mean"])
    if isinstance(stat, (int, float)):
        return float(stat)
    return None


def _summarize_benchy(raw: dict) -> dict:
    """Flatten a llama-benchy report into the fields the page and compare read.

    ``throughput`` and ``prefill_speed`` are tokens/s averaged over the
    generation tests (the context-prefill phases are excluded: they measure a
    different thing). ``latency_ms`` is the tool's own API round-trip probe,
    ``ttft_ms`` the mean time to first token, ``decode_latency_ms`` the
    per-token cost implied by the generation rate. The per-test table is kept
    under ``tests`` and the whole report under ``raw`` minus its time series,
    which are large and only meaningful in the tool's own plots.
    """
    tests = []
    for entry in raw.get("benchmarks") or []:
        if not isinstance(entry, dict):
            continue
        tests.append(
            {
                "concurrency": entry.get("concurrency"),
                "prompt_size": entry.get("prompt_size"),
                "response_size": entry.get("response_size"),
                "context_size": entry.get("context_size"),
                "prefill_phase": bool(entry.get("is_context_prefill_phase")),
                "pp_tps": _mean(entry, "pp_throughput"),
                "tg_tps": _mean(entry, "tg_throughput"),
                "peak_tps": _mean(entry, "peak_throughput"),
                "ttft_ms": _mean(entry, "e2e_ttft"),
            }
        )
    generation = [t for t in tests if not t["prefill_phase"]] or tests

    def _avg(key: str) -> float | None:
        values = [t[key] for t in generation if isinstance(t.get(key), float)]
        return round(sum(values) / len(values), 2) if values else None

    throughput = _avg("tg_tps")
    summary: dict[str, Any] = {
        "tool": "llama-benchy",
        "tool_version": raw.get("version"),
        "model": raw.get("model"),
        "throughput": throughput,
        "prefill_speed": _avg("pp_tps"),
        "peak_throughput": _avg("peak_tps"),
        "ttft_ms": _avg("ttft_ms"),
        "latency_ms": (
            round(float(raw["latency_ms"]), 3)
            if isinstance(raw.get("latency_ms"), (int, float))
            else None
        ),
        "decode_latency_ms": round(1000.0 / throughput, 2) if throughput else None,
        "tests": tests,
        "raw": {
            **{k: v for k, v in raw.items() if k != "benchmarks"},
            "benchmarks": [
                {k: v for k, v in e.items() if not str(k).endswith("_over_time")}
                for e in (raw.get("benchmarks") or [])
                if isinstance(e, dict)
            ],
        },
    }
    return summary


def _run_llama_benchy(record: dict, params: dict) -> dict:
    """Run llama-benchy against the deployment the record names, and summarise.

    The endpoint is the deployment's own: its port from the record (a request
    may override it), on this machine — rank zero of a native deployment runs
    here, which is also what the readiness probe assumes.
    """
    # A plain import is the availability check: nothing is called on the
    # module (the tool runs as a subprocess), and an import failure is the one
    # signal that survives every way the package can be absent.
    try:
        import llama_benchy  # noqa: F401,PLC0415
    except ImportError:
        raise RuntimeError(
            "llama-benchy is not installed. Install it with: "
            "pip install spark-pulse[benchmarking]"
        ) from None
    from spark_pulse import tools  # noqa: PLC0415 — the switched package

    deployment = tools.deploy_dispatch.get_deployment(record.get("deployment_id", ""))
    deployment = deployment or {}
    port = int(params.get("port") or deployment.get("port") or 8000)
    base_url = f"http://127.0.0.1:{port}/v1"
    run_params = dict(params)
    run_params.setdefault("model", deployment.get("model") or "")

    fd, out_path = tempfile.mkstemp(prefix="spark-pulse-benchy-", suffix=".json")
    os.close(fd)
    try:
        cmd = _benchy_command(base_url, out_path, run_params)
        logger.info("Running benchmark: %s", " ".join(cmd))
        try:
            done = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=int(params.get("timeout", BENCHY_DEFAULTS["timeout"])),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"llama-benchy did not finish within {exc.timeout:.0f}s"
            ) from exc
        if done.returncode != 0:
            tail = (done.stderr or done.stdout or "").strip().splitlines()[-6:]
            raise RuntimeError(
                f"llama-benchy exited {done.returncode}: " + " | ".join(tail)
            )
        try:
            with open(out_path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"llama-benchy wrote no readable result: {exc}") from exc
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    if not isinstance(raw, dict):
        raise RuntimeError("llama-benchy result is not a JSON object")
    return _summarize_benchy(raw)


def list_benchmarks() -> list[dict]:
    """Return all benchmarks sorted by started_at descending."""
    global _bench_cache_dirty

    with _bench_cache_lock:
        records = _load()
        kept = _purge_expired(records)
        _bench_cache.clear()
        _bench_cache.update({b["benchmark_id"]: b for b in kept})
        _bench_cache_dirty = False
        listed = sorted(
            _bench_cache.values(),
            key=lambda b: b.get("started_at", ""),
            reverse=True,
        )

    # The purge is *offered*, never waited for. `_delete_row` takes the file
    # mutex, and `execute_benchmark` holds that mutex for the whole length of a
    # benchmark run — minutes. Blocking here meant that listing benchmarks
    # while one was running waited 30 seconds and then raised
    # `filelock.Timeout`, i.e. a 500 on a read-only endpoint for the duration
    # of every run. Reading is not the moment to insist on housekeeping: the
    # expired records are already absent from the answer, and the next attempt
    # will take them.
    if len(kept) != len(records):
        surviving = {b["benchmark_id"] for b in kept}
        for record in records:
            if record["benchmark_id"] in surviving:
                continue
            try:
                _delete_row(record["benchmark_id"], timeout=0)
            except FileLockTimeout:
                logger.debug(
                    "benchmark %s is past retention but the store is busy; "
                    "leaving it for the next sweep",
                    record["benchmark_id"],
                )
                break

    return listed


def get_benchmark(benchmark_id: str) -> dict | None:
    """Return a single benchmark by ID, or None if not found."""
    with _bench_cache_lock:
        _ensure_cache_loaded()
        return _bench_cache.get(benchmark_id)


def delete_benchmark(benchmark_id: str) -> bool:
    """Remove one benchmark for good. ``False`` when there was nothing to remove.

    Two stores have to agree for a delete to *look* like one: the row and the
    point-read cache every read path answers from. Dropping only the row leaves
    ``get_benchmark`` and ``list_benchmarks`` still returning the record until
    something else happens to mark the cache dirty — a delete an operator
    watched succeed and then watched undo itself.

    The row goes first, and the cache entry after it, under the cache lock but
    *not* holding that lock across the write. The order is what makes the
    sequence safe against a concurrent reader: ``_ensure_cache_loaded`` and
    ``list_benchmarks`` both hold the cache lock across load-and-populate, so
    the eviction below either lands before such a reader starts (and its load
    then misses the deleted row) or after it finishes (and evicts what it just
    put there). Holding the cache lock over ``_delete_row`` instead would
    serialise every read behind the write mutex, which a benchmark run holds
    for minutes.

    One entry, not :func:`_reset_cache`: reloading ninety days of history to
    learn that one record is gone is the cost the per-row writes exist to
    avoid — the same reasoning as :func:`_cache_put`.
    """
    removed = _delete_row(benchmark_id)
    with _bench_cache_lock:
        _bench_cache.pop(benchmark_id, None)
    if removed:
        logger.info("Deleted benchmark %s", benchmark_id)
    return removed


def get_benchmarks_for_recipe(recipe_id: str) -> list[dict]:
    """Return all benchmarks for a specific recipe, sorted by date descending."""
    with _bench_cache_lock:
        _ensure_cache_loaded()
        return [b for b in _bench_cache.values() if b.get("recipe_id") == recipe_id]


def get_recipe_latest(recipe_id: str) -> dict | None:
    """Return the latest completed benchmark for a recipe, or None."""
    with _bench_cache_lock:
        _ensure_cache_loaded()
        recipes = [b for b in _bench_cache.values() if b.get("recipe_id") == recipe_id]
        for b in sorted(recipes, key=lambda x: x.get("started_at", ""), reverse=True):
            if b.get("status") == "completed" and b.get("results"):
                return b
        return None


def get_latest_by_recipe() -> dict[str, dict]:
    """Return the latest completed benchmark for each recipe.

    Returns a dict keyed by recipe_id.
    """
    with _bench_cache_lock:
        _ensure_cache_loaded()
        latest: dict[str, dict] = {}
        for b in _bench_cache.values():
            rid = b.get("recipe_id")
            if not rid or not b.get("results"):
                continue
            if b.get("status") != "completed":
                continue
            # Update if this is newer than what we have
            if rid not in latest or b.get("started_at", "") > latest[rid].get(
                "started_at", ""
            ):
                latest[rid] = b
        return latest


def compare_runs(ids: list[str]) -> dict | None:
    """Compare multiple benchmark runs against each other.

    Returns a dict with all runs and their pairwise differences.
    Returns None if any run is not found or has no results.
    """
    with _bench_cache_lock:
        _ensure_cache_loaded()
        runs: dict[str, dict] = {}
        for bid in ids:
            b = _bench_cache.get(bid)
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
        if (
            isinstance(current_val, (int, float))
            and isinstance(baseline_val, (int, float))
            and baseline_val != 0
        ):
            diff_pct = ((current_val - baseline_val) / baseline_val) * 100
            comparison[key] = {
                "current": current_val,
                "baseline": baseline_val,
                "difference_pct": round(diff_pct, 2),
            }

    return comparison
