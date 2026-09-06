"""Unit tests for the REAL ``spark_pulse.tools.benchmarking`` module.

pytest-env forces ``SIMULATION_MODE=1``, so ``from spark_pulse.tools import
benchmarking`` (and every ``mock.patch("spark_pulse.tools.benchmarking...")``
string target) resolves to ``spark_pulse/mock/benchmarking.py``.  This module
therefore imports the real submodule explicitly and patches it through the
module *object*, never through a dotted string.

Importing the real submodule rebinds ``spark_pulse.tools.benchmarking`` for the
rest of the process, which would silently re-point every other test module at
the real store, so the package attribute is restored immediately below and
re-asserted by an autouse fixture.

Usage:
    pytest tests/test_tools_benchmarking_real.py -v
"""

from __future__ import annotations

import importlib
import json
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from filelock import FileLock
from sqlalchemy import event

import spark_pulse.tools as tools_pkg

# ── Import the real module without disturbing the simulation-mode binding ────

_SIM_BENCHMARKING = tools_pkg.benchmarking

benchmarking = importlib.import_module("spark_pulse.tools.benchmarking")

# Put the package attribute back the way ``tools/__init__`` left it.  The
# sys.modules entry stays (harmless, and it keeps coverage able to resolve the
# module by name); attribute lookup is what monkeypatch/mock.patch resolve
# against, and that is what other test modules rely on.
tools_pkg.benchmarking = _SIM_BENCHMARKING

assert benchmarking.__name__ == "spark_pulse.tools.benchmarking"
assert benchmarking.__file__.endswith("spark_pulse/tools/benchmarking.py")
assert benchmarking is not _SIM_BENCHMARKING


@pytest.fixture(autouse=True)
def _keep_simulation_binding():
    """Guarantee ``tools.benchmarking`` still resolves to the mock afterwards."""
    yield
    tools_pkg.benchmarking = _SIM_BENCHMARKING


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _iso(days_ago: float) -> str:
    """An ISO-8601 UTC timestamp ``days_ago`` days before now."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


NOW = _iso(0)
YESTERDAY = _iso(1)
LAST_WEEK = _iso(7)
EXPIRED = _iso(120)  # outside the 90-day retention window


def _record(bid: str, **overrides) -> dict:
    """A complete benchmark record, shaped exactly like create_benchmark's."""
    rec = {
        "benchmark_id": bid,
        "deployment_id": "dep-1",
        "recipe_id": "qwen3-30b",
        "recipe_name": "Qwen3 30B",
        "baseline_id": None,
        "status": "completed",
        "started_at": YESTERDAY,
        "completed_at": NOW,
        "params": {},
        "results": {"throughput": 40.0, "latency_ms": 12.0},
    }
    rec.update(overrides)
    return rec


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point the real module's on-disk store (and its file lock) at tmp_path."""
    path = tmp_path / "benchmarks.json"
    monkeypatch.setattr(benchmarking, "_BENCHMARKS_PATH", path)
    monkeypatch.setattr(
        benchmarking, "_BENCHMARKS_LOCK", FileLock(f"{path}.lock", timeout=5)
    )
    benchmarking._reset_cache()
    yield path
    benchmarking._reset_cache()


def _seed(path, records: list[dict]) -> None:
    """Put records straight into the store and drop the in-memory cache."""
    del path  # kept so callers still scope state to their fixture
    benchmarking._save(records)
    benchmarking._reset_cache()


def _on_disk(path) -> list[dict]:
    del path  # kept so callers still scope state to their fixture
    return benchmarking._load()


@contextmanager
def _rows_written():
    """Collect the INSERT/UPDATE/DELETE statements the block sends to ``benchmarks``.

    The difference between a whole-set save and a per-row write is invisible in
    the data — both leave the store correct — and visible only in how much of
    the table was rewritten to get there. Counting statements is the only way
    to assert the property the per-row path exists for.
    """
    from spark_pulse.db import engine

    statements: list[str] = []

    def _watch(conn, cursor, statement, parameters, context, executemany):
        head = statement.strip().split(maxsplit=1)[0].upper()
        if head in ("INSERT", "UPDATE", "DELETE") and "benchmarks" in statement:
            statements.append(statement)

    live = engine()
    event.listen(live, "before_cursor_execute", _watch)
    try:
        yield statements
    finally:
        event.remove(live, "before_cursor_execute", _watch)


class _FakeBenchy:
    """Stand-in for the llama_benchy package — records how it was called."""

    def __init__(self, result=None, exc: Exception | None = None):
        self.result = result if result is not None else {"throughput": 51.5}
        self.exc = exc
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.fixture()
def benchy(monkeypatch):
    """Install a fake llama_benchy in sys.modules; return it for assertions."""

    def _install(result=None, exc: Exception | None = None) -> _FakeBenchy:
        fake = _FakeBenchy(result, exc)
        monkeypatch.setitem(sys.modules, "llama_benchy", fake)
        return fake

    return _install


# ── create_benchmark ─────────────────────────────────────────────────────────


class TestCreateBenchmark:
    def test_persists_a_running_record_with_the_supplied_metadata(self, store):
        record = benchmarking.create_benchmark(
            deployment_id="dep-42",
            baseline_id="base-1",
            params={"port": 8001, "model": "qwen"},
            recipe_id="qwen3-30b",
            recipe_name="Qwen3 30B",
        )

        assert record["status"] == "running"
        assert record["deployment_id"] == "dep-42"
        assert record["baseline_id"] == "base-1"
        assert record["recipe_id"] == "qwen3-30b"
        assert record["recipe_name"] == "Qwen3 30B"
        assert record["params"] == {"port": 8001, "model": "qwen"}
        assert record["results"] is None
        assert record["completed_at"] is None
        # started_at is a parseable UTC timestamp
        assert datetime.fromisoformat(record["started_at"]).tzinfo is not None

        assert _on_disk(store) == [record]

    def test_generates_a_unique_id_and_adds_to_the_existing_records(self, store):
        """As a set: row order is not part of what the store promises.

        ``_load`` orders by id so both backends answer the same way, which is
        not insertion order and was never guaranteed to be. What ordering
        callers see is ``list_benchmarks``', and that sorts by ``started_at``.
        """
        _seed(store, [_record("old-1")])

        first = benchmarking.create_benchmark("dep-1")
        second = benchmarking.create_benchmark("dep-2")

        assert first["benchmark_id"] != second["benchmark_id"]
        ids = {b["benchmark_id"] for b in _on_disk(store)}
        assert ids == {"old-1", first["benchmark_id"], second["benchmark_id"]}

    def test_defaults_params_to_an_empty_dict(self, store):
        assert benchmarking.create_benchmark("dep-1")["params"] == {}

    def test_new_record_is_visible_immediately_through_get_benchmark(self, store):
        """Regression: a warm cache used to hide records created after it loaded.

        ``_atomic_benchmarks`` marks the cache dirty after every write; without
        that, POST /api/benchmarks followed by GET /api/benchmarks/{id} 404s.
        """
        benchmarking.list_benchmarks()  # warms the cache

        record = benchmarking.create_benchmark("dep-1", recipe_id="r1")

        assert benchmarking.get_benchmark(record["benchmark_id"]) == record
        assert benchmarking.get_benchmarks_for_recipe("r1") == [record]


# ── execute_benchmark ────────────────────────────────────────────────────────


class TestExecuteBenchmark:
    def test_runs_llama_benchy_with_the_recorded_params_and_stores_results(
        self, store, benchy
    ):
        fake = benchy(result={"throughput": 51.5, "latency_ms": 9.4})
        record = benchmarking.create_benchmark(
            "dep-1",
            params={
                "port": 8003,
                "model": "Qwen/Qwen3-30B",
                "benchmarks": ["throughput"],
                "context_length": 8192,
            },
        )

        benchmarking.execute_benchmark(record["benchmark_id"])

        assert fake.calls == [
            {
                "target": "http://localhost:8003",
                "model_name": "Qwen/Qwen3-30B",
                "benchmarks": ["throughput"],
                "context_length": 8192,
            }
        ]
        stored = _on_disk(store)[0]
        assert stored["status"] == "completed"
        assert stored["results"] == {"throughput": 51.5, "latency_ms": 9.4}
        assert stored["completed_at"] is not None
        # and the update is visible through the read API, not just on disk
        assert benchmarking.get_benchmark(record["benchmark_id"])["status"] == (
            "completed"
        )

    def test_falls_back_to_default_target_model_and_context(self, store, benchy):
        fake = benchy()
        record = benchmarking.create_benchmark("dep-1")

        benchmarking.execute_benchmark(record["benchmark_id"])

        assert fake.calls[0] == {
            "target": "http://localhost:8000",
            "model_name": "unknown",
            "benchmarks": ["throughput", "latency"],
            "context_length": 4096,
        }

    def test_records_an_error_when_llama_benchy_is_not_installed(
        self, store, monkeypatch
    ):
        # ``None`` in sys.modules makes ``import llama_benchy`` raise ImportError.
        monkeypatch.setitem(sys.modules, "llama_benchy", None)
        record = benchmarking.create_benchmark("dep-1")

        benchmarking.execute_benchmark(record["benchmark_id"])

        stored = _on_disk(store)[0]
        assert stored["status"] == "error"
        assert "llama-benchy is not installed" in stored["results"]["error"]
        assert "spark-pulse[benchmarking]" in stored["results"]["error"]
        assert stored["completed_at"] is not None

    def test_records_an_error_when_the_benchmark_run_raises(self, store, benchy):
        benchy(exc=ConnectionError("connection refused"))
        record = benchmarking.create_benchmark("dep-1")

        benchmarking.execute_benchmark(record["benchmark_id"])

        stored = _on_disk(store)[0]
        assert stored["status"] == "error"
        assert stored["results"] == {"error": "connection refused"}

    def test_propagates_keyboard_interrupt_and_leaves_the_record_running(
        self, store, benchy
    ):
        benchy(exc=KeyboardInterrupt())
        record = benchmarking.create_benchmark("dep-1")

        with pytest.raises(KeyboardInterrupt):
            benchmarking.execute_benchmark(record["benchmark_id"])

        assert _on_disk(store)[0]["status"] == "running"

    def test_unknown_id_is_a_no_op(self, store, benchy):
        fake = benchy()
        _seed(store, [_record("known")])

        benchmarking.execute_benchmark("does-not-exist")

        assert fake.calls == []
        assert _on_disk(store) == [_record("known")]


# ── list / get ───────────────────────────────────────────────────────────────


class TestListAndGet:
    def test_lists_newest_first(self, store):
        _seed(
            store,
            [
                _record("b-old", started_at=LAST_WEEK),
                _record("b-new", started_at=NOW),
                _record("b-mid", started_at=YESTERDAY),
            ],
        )

        assert [b["benchmark_id"] for b in benchmarking.list_benchmarks()] == [
            "b-new",
            "b-mid",
            "b-old",
        ]

    def test_hides_records_older_than_the_retention_window(self, store):
        _seed(
            store,
            [
                _record("fresh", started_at=YESTERDAY),
                _record("stale", started_at=EXPIRED),
            ],
        )

        listed = benchmarking.list_benchmarks()

        assert [b["benchmark_id"] for b in listed] == ["fresh"]
        # the purge also drops it from the cache the point reads go through
        assert benchmarking.get_benchmark("stale") is None

    def test_retention_window_is_configurable(self, store, monkeypatch):
        monkeypatch.setattr(benchmarking, "_RETENTION_DAYS", 3)
        _seed(
            store,
            [
                _record("fresh", started_at=YESTERDAY),
                _record("old", started_at=LAST_WEEK),
            ],
        )

        assert [b["benchmark_id"] for b in benchmarking.list_benchmarks()] == ["fresh"]

    def test_returns_empty_when_the_store_does_not_exist(self, store):
        assert not store.exists()
        assert benchmarking.list_benchmarks() == []
        assert benchmarking.get_benchmark("anything") is None

    def test_survives_a_corrupt_store_file(self, store):
        store.write_text("{not json")
        benchmarking._reset_cache()

        assert benchmarking.list_benchmarks() == []

    def test_get_benchmark_returns_the_record_or_none(self, store):
        _seed(store, [_record("b-1")])

        assert benchmarking.get_benchmark("b-1")["deployment_id"] == "dep-1"
        assert benchmarking.get_benchmark("nope") is None

    def test_get_benchmarks_for_recipe_filters_by_recipe_id(self, store):
        _seed(
            store,
            [
                _record("a", recipe_id="r1"),
                _record("b", recipe_id="r2"),
                _record("c", recipe_id="r1"),
            ],
        )

        got = {b["benchmark_id"] for b in benchmarking.get_benchmarks_for_recipe("r1")}

        assert got == {"a", "c"}
        assert benchmarking.get_benchmarks_for_recipe("unknown") == []


# ── latest-completed lookups ─────────────────────────────────────────────────


class TestLatestLookups:
    def test_get_recipe_latest_picks_the_newest_completed_run_with_results(self, store):
        _seed(
            store,
            [
                _record("old-done", recipe_id="r1", started_at=LAST_WEEK),
                _record(
                    "newer-running",
                    recipe_id="r1",
                    status="running",
                    results=None,
                    started_at=YESTERDAY,
                ),
                _record(
                    "newest-error",
                    recipe_id="r1",
                    status="error",
                    results={"error": "boom"},
                    started_at=NOW,
                ),
                _record("mid-done", recipe_id="r1", started_at=_iso(2)),
            ],
        )

        assert benchmarking.get_recipe_latest("r1")["benchmark_id"] == "mid-done"

    def test_get_recipe_latest_returns_none_without_a_completed_run(self, store):
        _seed(
            store, [_record("running", recipe_id="r1", status="running", results=None)]
        )

        assert benchmarking.get_recipe_latest("r1") is None
        assert benchmarking.get_recipe_latest("no-such-recipe") is None

    def test_get_latest_by_recipe_keys_the_newest_completed_run_per_recipe(self, store):
        _seed(
            store,
            [
                _record("r1-old", recipe_id="r1", started_at=LAST_WEEK),
                _record("r1-new", recipe_id="r1", started_at=NOW),
                _record("r2-only", recipe_id="r2", started_at=LAST_WEEK),
                _record("r3-running", recipe_id="r3", status="running", results=None),
                _record("r4-no-results", recipe_id="r4", results=None),
                _record(
                    "r5-failed",
                    recipe_id="r5",
                    status="error",
                    results={"error": "boom"},
                ),
                _record("no-recipe", recipe_id="", started_at=NOW),
            ],
        )

        latest = benchmarking.get_latest_by_recipe()

        assert set(latest) == {"r1", "r2"}
        assert latest["r1"]["benchmark_id"] == "r1-new"
        assert latest["r2"]["benchmark_id"] == "r2-only"


# ── compare_runs ─────────────────────────────────────────────────────────────


class TestCompareRuns:
    def test_compares_shared_numeric_metrics_pairwise(self, store):
        _seed(
            store,
            [
                _record(
                    "a",
                    recipe_name="A",
                    results={"throughput": 40.0, "latency_ms": 10.0},
                ),
                _record(
                    "b",
                    recipe_name="B",
                    results={"throughput": 50.0, "latency_ms": 8.0},
                ),
            ],
        )

        result = benchmarking.compare_runs(["a", "b"])

        assert result["run_ids"] == ["a", "b"]
        assert set(result["runs"]) == {"a", "b"}
        throughput = result["comparison"]["throughput"]
        assert throughput["values"]["a"] == {
            "value": 40.0,
            "recipe_name": "A",
            "started_at": YESTERDAY,
        }
        # a is 20% slower than b: (40 - 50) / 50 * 100
        assert throughput["differences"]["a_vs_b"] == {"difference_pct": -20.0}
        assert result["comparison"]["latency_ms"]["differences"]["a_vs_b"] == {
            "difference_pct": 25.0
        }

    def test_rounds_percentages_to_two_decimals(self, store):
        _seed(
            store,
            [
                _record("a", results={"throughput": 100.0}),
                _record("b", results={"throughput": 33.0}),
            ],
        )

        diff = benchmarking.compare_runs(["a", "b"])["comparison"]["throughput"]
        assert diff["differences"]["a_vs_b"]["difference_pct"] == 203.03

    def test_skips_the_error_key_and_non_numeric_metrics(self, store):
        _seed(
            store,
            [
                _record(
                    "a", results={"throughput": 40.0, "error": "x", "engine": "vllm"}
                ),
                _record(
                    "b", results={"throughput": 50.0, "error": "y", "engine": "sglang"}
                ),
            ],
        )

        comparison = benchmarking.compare_runs(["a", "b"])["comparison"]

        assert "error" not in comparison
        assert "engine" not in comparison
        assert set(comparison) == {"throughput"}

    def test_drops_metrics_present_in_only_one_run(self, store):
        _seed(
            store,
            [
                _record("a", results={"throughput": 40.0, "prefill_speed": 300.0}),
                _record("b", results={"throughput": 50.0}),
            ],
        )

        assert set(benchmarking.compare_runs(["a", "b"])["comparison"]) == {
            "throughput"
        }

    def test_guards_against_division_by_zero(self, store):
        _seed(
            store,
            [
                _record("a", results={"throughput": 40.0}),
                _record("b", results={"throughput": 0}),
            ],
        )

        throughput = benchmarking.compare_runs(["a", "b"])["comparison"]["throughput"]

        assert throughput["differences"] == {}
        assert set(throughput["values"]) == {"a", "b"}

    def test_compares_three_runs_pairwise(self, store):
        _seed(
            store,
            [
                _record("a", results={"throughput": 100.0}),
                _record("b", results={"throughput": 50.0}),
                _record("c", results={"throughput": 200.0}),
            ],
        )

        differences = benchmarking.compare_runs(["a", "b", "c"])["comparison"][
            "throughput"
        ]["differences"]

        assert set(differences) == {"a_vs_b", "a_vs_c", "b_vs_c"}
        assert differences["a_vs_b"]["difference_pct"] == 100.0
        assert differences["a_vs_c"]["difference_pct"] == -50.0

    def test_returns_none_for_missing_or_resultless_or_too_few_runs(self, store):
        _seed(
            store,
            [
                _record("a"),
                _record("pending", status="running", results=None),
            ],
        )

        assert benchmarking.compare_runs(["a", "missing"]) is None
        assert benchmarking.compare_runs(["a", "pending"]) is None
        assert benchmarking.compare_runs(["a"]) is None
        assert benchmarking.compare_runs(["a", "a"]) is None


# ── get_baseline_comparison ──────────────────────────────────────────────────


class TestBaselineComparison:
    def test_computes_the_percentage_change_against_the_baseline(self, store):
        _seed(
            store,
            [
                _record("base", results={"throughput": 40.0, "latency_ms": 10.0}),
                _record(
                    "run",
                    baseline_id="base",
                    results={"throughput": 50.0, "latency_ms": 12.5},
                ),
            ],
        )

        comparison = benchmarking.get_baseline_comparison("run")

        assert comparison["throughput"] == {
            "current": 50.0,
            "baseline": 40.0,
            "difference_pct": 25.0,
        }
        assert comparison["latency_ms"]["difference_pct"] == 25.0

    def test_skips_error_non_numeric_one_sided_and_zero_baseline_metrics(self, store):
        _seed(
            store,
            [
                _record(
                    "base",
                    results={
                        "throughput": 40.0,
                        "gpu_utilization": 0,
                        "error": "e",
                        "engine": "vllm",
                        "only_baseline": 1.0,
                    },
                ),
                _record(
                    "run",
                    baseline_id="base",
                    results={
                        "throughput": 50.0,
                        "gpu_utilization": 90,
                        "error": "e",
                        "engine": "sglang",
                        "only_current": 2.0,
                    },
                ),
            ],
        )

        assert set(benchmarking.get_baseline_comparison("run")) == {"throughput"}

    def test_returns_none_without_a_usable_baseline(self, store):
        _seed(
            store,
            [
                _record("no-baseline"),
                _record("dangling", baseline_id="gone"),
                _record("empty-base", baseline_id="pending"),
                _record("pending", status="running", results=None),
            ],
        )

        assert benchmarking.get_baseline_comparison("no-baseline") is None
        assert benchmarking.get_baseline_comparison("dangling") is None
        assert benchmarking.get_baseline_comparison("empty-base") is None
        assert benchmarking.get_baseline_comparison("missing-id") is None


# ── persistence primitives ───────────────────────────────────────────────────


class TestPersistence:
    def test_a_created_benchmark_is_readable_again(self, store):
        """Was: the temp file was renamed and left none behind.

        Storage is the database now, so the property worth pinning is the one
        the file mechanics existed to provide — a create is durable and reads
        back — rather than how it reached the disk.
        """
        created = benchmarking.create_benchmark("dep-1")

        stored = _on_disk(store)
        assert isinstance(stored, list)
        assert [b["benchmark_id"] for b in stored] == [created["benchmark_id"]]

    def test_a_failed_write_leaves_the_previous_contents_intact(self, store):
        _seed(store, [_record("keep")])

        with pytest.raises(RuntimeError):
            with benchmarking._atomic_benchmarks() as data:
                data.append(_record("never-saved"))
                raise RuntimeError("boom")

        assert [b["benchmark_id"] for b in _on_disk(store)] == ["keep"]

    def test_reset_cache_forces_a_reload_from_the_store(self, store):
        _seed(store, [_record("first")])
        assert benchmarking.get_benchmark("first") is not None

        benchmarking._save([_record("second")])
        assert benchmarking.get_benchmark("second") is None  # still cached

        benchmarking._reset_cache()
        assert benchmarking.get_benchmark("second") is not None


# ── Per-row writes ───────────────────────────────────────────────────────────


class TestPerRowWrites:
    """One benchmark changing must cost one row, not the whole history."""

    def test_creating_a_benchmark_writes_only_its_own_row(self, store):
        _seed(store, [_record("a"), _record("b"), _record("c")])

        with _rows_written() as statements:
            benchmarking.create_benchmark("dep-1")

        assert len(statements) == 1

    def test_finishing_a_benchmark_writes_only_its_own_row(self, store, benchy):
        benchy()
        _seed(store, [_record("a"), _record("b")])
        record = benchmarking.create_benchmark("dep-1")

        with _rows_written() as statements:
            benchmarking.execute_benchmark(record["benchmark_id"])

        assert len(statements) == 1

    def test_the_whole_set_save_writes_only_the_records_that_changed(self, store):
        """The kept API, no longer paying for the rows it was handed unchanged."""
        _seed(store, [_record("a"), _record("b"), _record("c")])

        with _rows_written() as statements:
            benchmarking._save(
                [
                    _record("a"),
                    _record("b", status="error", results={"error": "boom"}),
                    _record("c"),
                ]
            )

        assert len(statements) == 1
        assert benchmarking._get_row("b")["status"] == "error"

    def test_the_whole_set_save_still_removes_records_absent_from_the_set(self, store):
        """The one thing a per-row write must not quietly take away."""
        _seed(store, [_record("gone"), _record("kept")])

        benchmarking._save([_record("kept")])

        assert [b["benchmark_id"] for b in _on_disk(store)] == ["kept"]

    def test_a_record_with_no_id_is_refused_rather_than_written_nowhere(self, store):
        """The whole-set save skips these as noise; one on its own is a caller bug."""
        with pytest.raises(ValueError):
            benchmarking._upsert_row({"deployment_id": "dep-1"})

    def test_a_create_waits_for_a_whole_set_save_already_in_flight(self, store):
        """The lost update the whole-set path was serialised to prevent.

        A whole-set save deletes every row absent from the list it loaded, so a
        create that landed while one was in flight would be deleted by it. The
        per-row write takes the same mutex, so it waits instead.
        """
        _seed(store, [_record("existing")])
        holding = threading.Event()
        release = threading.Event()
        created: list[dict] = []

        def whole_set_save():
            with benchmarking._atomic_benchmarks() as data:
                holding.set()
                release.wait(5)
                data.append(_record("from-the-whole-set"))

        def create():
            created.append(benchmarking.create_benchmark("dep-1"))

        writer = threading.Thread(target=whole_set_save)
        writer.start()
        assert holding.wait(5)
        creator = threading.Thread(target=create)
        creator.start()
        creator.join(0.3)
        assert creator.is_alive(), "the create did not wait for the mutex"

        release.set()
        writer.join(5)
        creator.join(5)

        assert {b["benchmark_id"] for b in _on_disk(store)} == {
            "existing",
            "from-the-whole-set",
            created[0]["benchmark_id"],
        }


# ── Retention ────────────────────────────────────────────────────────────────


class TestRetention:
    def test_expired_records_are_deleted_from_the_store_not_merely_hidden(self, store):
        """The purge used to filter the answer and leave the rows.

        It logged "Purged N expired benchmark records" every time it ran, on a
        store that never lost one — so retention was a claim in the log and the
        table grew without bound.
        """
        _seed(
            store,
            [
                _record("fresh", started_at=YESTERDAY),
                _record("stale", started_at=EXPIRED),
            ],
        )

        benchmarking.list_benchmarks()

        assert [b["benchmark_id"] for b in _on_disk(store)] == ["fresh"]

    def test_purging_the_last_record_does_not_re_import_the_json_file(self, store):
        """The import is keyed on the ``meta`` table, never on an empty table.

        Emptying the store by retention is exactly the state a "is the table
        empty" check would mistake for a fresh install — and it would import
        the ninety-day-old records straight back.
        """
        store.write_text(json.dumps([_record("stale", started_at=EXPIRED)]))
        benchmarking._reset_cache()

        assert benchmarking.list_benchmarks() == []
        assert benchmarking.list_benchmarks() == []
        assert _on_disk(store) == []

    def test_a_point_read_triggers_the_one_time_json_import(self, store, benchy):
        """``execute_benchmark`` reads one row now, so the import must happen there."""
        store.write_text(
            json.dumps(
                [
                    _record(
                        "from-json",
                        status="running",
                        completed_at=None,
                        results=None,
                        params={"port": 8000},
                    )
                ]
            )
        )
        benchmarking._reset_cache()
        benchy(result={"throughput": 12.0})

        benchmarking.execute_benchmark("from-json")

        assert benchmarking.get_benchmark("from-json")["status"] == "completed"
