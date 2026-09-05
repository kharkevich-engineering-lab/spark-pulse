"""``GET /api/deployments/{id}/metrics`` and the simulated sampler behind it.

The endpoint is the only way the UI sees the engine's own numbers, and its
contract is as much about the *absence* of numbers as about their presence: an
unreadable endpoint must come back as a stated reason with an empty sample
list, never as a chart of nothing.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from spark_pulse import tools
from spark_pulse.app import create_app


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A deployment store of this test's own."""
    path = tmp_path / "deployments.json"
    monkeypatch.setattr(tools.deployment_records, "RECORDS_FILE", path)
    return path


@pytest.fixture
def sampler():
    """A fresh simulated sampler; the singleton is dropped afterwards."""
    tools.engine_metrics.stop_sampler()
    yield tools.engine_metrics.get_sampler()
    tools.engine_metrics.stop_sampler()


@pytest.fixture
def client(store, sampler):
    with TestClient(create_app()) as test_client:
        yield test_client


def write(store, *records):
    store.write_text(json.dumps(list(records)))


def record(**overrides):
    base = {
        "id": "dep-1",
        "name": "qwen",
        "recipe_id": "bundled/qwen",
        "status": "running",
        "runtime": "native",
        "engine": "vllm",
        "variant": "default",
        "model": "Qwen/Qwen3-8B",
        "created_at": "2026-01-01T00:00:00Z",
        "port": 9000,
        "readiness_url": "http://127.0.0.1:9000/v1/models",
        "metrics_path": "/metrics",
        "launch_command": "vllm serve Qwen/Qwen3-8B",
        "container_name": "spark-pulse-dep-1-r0-g1",
        "ranks": [],
        "orphans": [],
    }
    base.update(overrides)
    return base


class TestEndpoint:
    def test_an_unknown_deployment_is_a_404(self, client, store):
        write(store)

        assert client.get("/api/deployments/nope/metrics").status_code == 404

    def test_a_deployment_never_sampled_reports_no_samples_and_a_reason(
        self, client, store
    ):
        write(store, record())

        body = client.get("/api/deployments/dep-1/metrics").json()

        assert body["deployment_id"] == "dep-1"
        assert body["available"] is False
        assert body["reason"]
        assert body["detail"]
        assert body["samples"] == []

    def test_a_sampled_deployment_carries_the_engine_numbers(
        self, client, store, sampler
    ):
        write(store, record())
        sampler.sample_once()
        sampler.sample_once()

        body = client.get("/api/deployments/dep-1/metrics").json()

        assert body["available"] is True
        assert body["reason"] is None
        assert len(body["samples"]) == 2
        latest = body["samples"][-1]
        assert latest["running"] is not None
        assert latest["kv_fraction"] is not None
        assert latest["generation_tokens_per_second"] is not None

    def test_the_window_is_declared_volatile_so_the_ui_can_say_so(
        self, client, store, sampler
    ):
        write(store, record())
        sampler.sample_once()

        body = client.get("/api/deployments/dep-1/metrics").json()

        assert body["volatile"] is True
        assert body["sample_interval_seconds"] > 0
        assert body["window_seconds"] > 0

    def test_a_stopped_deployment_says_there_is_no_engine_to_ask(
        self, client, store, sampler
    ):
        write(store, record(status="stopped"))
        sampler.sample_once()

        body = client.get("/api/deployments/dep-1/metrics").json()

        assert body["available"] is False
        assert body["reason"] == tools.engine_metrics.REASON_NOT_RUNNING
        assert body["samples"] == []

    def test_an_sglang_deployment_explains_that_it_publishes_nothing(
        self, client, store, sampler
    ):
        """The engine spec does not pass ``--enable-metrics``; say so."""
        write(
            store,
            record(
                engine="sglang",
                launch_command="python3 -m sglang.launch_server --port 9000",
            ),
        )
        sampler.sample_once()

        body = client.get("/api/deployments/dep-1/metrics").json()

        assert body["available"] is False
        assert body["reason"] == tools.engine_metrics.REASON_NOT_ENABLED
        assert "--enable-metrics" in body["detail"]
        assert body["samples"] == []

    def test_deleting_a_deployment_drops_its_window(self, client, store, sampler):
        write(store, record(status="stopped"))
        sampler._set_status("dep-1", tools.engine_metrics.AVAILABLE)
        sampler._ring("dep-1").append(tools.engine_metrics.Reading(t=1.0, running=1.0))

        assert client.delete("/api/deployments/dep-1").json()["deleted"] is True

        assert sampler.snapshot("dep-1")["samples"] == []


class TestSimulatedEngine:
    """What simulation publishes, and what it deliberately refuses to."""

    def test_the_simulated_body_goes_through_the_real_parser(self, sampler, store):
        body = tools.engine_metrics.render_vllm_body(3)

        families = tools.engine_metrics.parse_prometheus_text(body)

        assert families["vllm:num_requests_running"][0].value == 3.0
        assert families["vllm:prompt_tokens_total"][0].value == 1000 + 3 * 340

    def test_the_simulated_counters_only_ever_grow(self, sampler, store):
        write(store, record())
        for _ in range(5):
            sampler.sample_once()

        samples = sampler.snapshot("dep-1")["samples"]

        totals = [s["generation_tokens_total"] for s in samples]
        assert totals == sorted(totals)
        assert not any(s["counter_reset"] for s in samples)

    def test_simulation_never_pretends_sglang_serves_metrics(self, sampler, store):
        """Hiding this in the simulator would hide the feature's one caveat."""
        write(
            store,
            record(engine="sglang", launch_command="python3 -m sglang.launch_server"),
        )

        sampler.sample_once()

        assert sampler.availability("dep-1").reason == (
            tools.engine_metrics.REASON_NOT_ENABLED
        )

    def test_a_departed_deployment_is_forgotten_including_its_tick(
        self, sampler, store
    ):
        write(store, record())
        sampler.sample_once()
        write(store)

        sampler.sample_once()

        assert sampler._ticks == {}

    def test_forgetting_one_deployment_forgets_its_tick(self, sampler, store):
        write(store, record())
        sampler.sample_once()

        sampler.forget("dep-1")

        assert sampler._ticks == {}

    def test_the_simulated_scrape_never_reaches_the_network(self, sampler):
        assert "vllm:num_requests_running" in tools.engine_metrics.scrape("http://x")

    def test_the_simulated_singleton_is_the_switched_one(self, sampler):
        assert tools.engine_metrics.get_sampler() is sampler
        assert type(sampler).__module__ == "spark_pulse.mock.engine_metrics"

    def test_starting_and_stopping_the_simulated_sampler(self):
        tools.engine_metrics.stop_sampler()
        started = tools.engine_metrics.start_sampler()
        try:
            assert started.running
        finally:
            tools.engine_metrics.stop_sampler()

    def test_reset_forgets_the_simulated_sampler(self):
        first = tools.engine_metrics.get_sampler()
        tools.engine_metrics.reset()

        assert tools.engine_metrics.get_sampler() is not first
        tools.engine_metrics.stop_sampler()

    def test_the_module_level_snapshot_and_forget_reach_the_singleton(
        self, sampler, store
    ):
        write(store, record())
        sampler.sample_once()

        assert tools.engine_metrics.snapshot("dep-1")["samples"]

        tools.engine_metrics.forget("dep-1")

        assert tools.engine_metrics.snapshot("dep-1")["samples"] == []


class TestStartup:
    def test_the_app_starts_the_sampler_which_is_the_whole_point(self, store):
        """The subsystem this replaced was built at startup and never started."""
        write(store)
        tools.engine_metrics.stop_sampler()

        with TestClient(create_app()):
            assert tools.engine_metrics.get_sampler().running

        assert tools.engine_metrics._sampler is None
