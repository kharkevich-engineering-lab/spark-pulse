"""The engine metrics reader: parsing, rates, the ring, and honest silence.

The module under test is reached with ``importlib.import_module`` rather than
``import spark_pulse.tools.engine_metrics``. Both give the real module here —
``mock/engine_metrics.py`` imports it at module scope, so it is in
``sys.modules`` before the switch runs — but only ``import_module`` is
guaranteed not to rebind ``spark_pulse.tools.engine_metrics`` for the rest of
the process, and the router tests in this file need the switch left alone.

The invariant every test in this file exists to protect: **nothing here ever
invents a number.** A counter that goes backwards produces no rate, an engine
that says nothing produces no sample, and an endpoint that cannot be addressed
produces a stated reason rather than an empty chart.
"""

from __future__ import annotations

import importlib
import threading
import time

import pytest

from spark_pulse import tools

em = importlib.import_module("spark_pulse.tools.engine_metrics")


VLLM_BODY = """\
# HELP vllm:num_requests_running Requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="Qwen/Qwen3-8B"} 3.0
vllm:num_requests_waiting{engine="0",model_name="Qwen/Qwen3-8B"} 11.0
vllm:kv_cache_usage_perc{engine="0",model_name="Qwen/Qwen3-8B"} 0.42
vllm:prompt_tokens_total{engine="0",model_name="Qwen/Qwen3-8B"} 1000.0
vllm:generation_tokens_total{engine="0",model_name="Qwen/Qwen3-8B"} 500.0
vllm:num_preemptions_total{engine="0",model_name="Qwen/Qwen3-8B"} 2.0
"""

SGLANG_BODY = """\
sglang:num_running_reqs{model_name="m"} 4.0
sglang:num_queue_reqs{model_name="m"} 0.0
sglang:token_usage{model_name="m"} 0.31
sglang:prompt_tokens_total{model_name="m"} 90.0
sglang:generation_tokens_total{model_name="m"} 45.0
sglang:num_retracted_reqs{model_name="m"} 1.0
"""


def record(**overrides):
    """A native deployment record, running, with a metrics path."""
    base = {
        "id": "dep-1",
        "status": "running",
        "runtime": "native",
        "engine": "vllm",
        "variant": "default",
        "port": 9000,
        "readiness_url": "http://127.0.0.1:9000/v1/models",
        "metrics_path": "/metrics",
        "launch_command": "vllm serve Qwen/Qwen3-8B",
    }
    base.update(overrides)
    return base


# ── Parsing ──────────────────────────────────────────────────────────────────


class TestParsing:
    def test_a_labelled_sample_keeps_its_labels(self):
        families = em.parse_prometheus_text(VLLM_BODY)

        (series,) = families["vllm:num_requests_running"]
        assert series.value == 3.0
        assert series.labels == {"engine": "0", "model_name": "Qwen/Qwen3-8B"}

    def test_help_and_type_lines_are_not_samples(self):
        families = em.parse_prometheus_text(VLLM_BODY)

        assert all(not name.startswith("#") for name in families)
        assert len(families) == 6

    def test_a_sample_with_no_labels_parses(self):
        families = em.parse_prometheus_text("some_metric 7\n")

        assert families["some_metric"][0].value == 7.0
        assert families["some_metric"][0].labels == {}

    def test_a_label_value_may_contain_a_quote_a_comma_and_a_brace(self):
        text = 'm{a="x,\\"y\\"",b="}z"} 1\n'

        (series,) = em.parse_prometheus_text(text)["m"]

        assert series.labels == {"a": 'x,"y"', "b": "}z"}
        assert series.value == 1.0

    def test_an_escaped_newline_and_tab_are_decoded(self):
        (series,) = em.parse_prometheus_text('m{a="x\\ny\\tz"} 1\n')["m"]

        assert series.labels == {"a": "x\ny\tz"}

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "# a bare comment",
            '{a="1"} 5',  # a metric with no name
            "no_value",
            "m{unterminated 1",
            'm{a="1"}',
            "m not_a_number",
        ],
    )
    def test_a_line_we_cannot_read_is_skipped_not_guessed(self, line):
        assert em.parse_prometheus_text(line + "\n") == {}

    @pytest.mark.parametrize("line", ["m{a} 1", "m{a=} 1"])
    def test_an_unreadable_label_block_costs_the_labels_not_the_value(self, line):
        """The name and the number are still real; nothing here reads a label."""
        (series,) = em.parse_prometheus_text(line + "\n")["m"]

        assert series.value == 1.0
        assert series.labels == {}

    @pytest.mark.parametrize("value", ["NaN", "+Inf", "-Inf"])
    def test_nan_and_infinity_are_dropped_rather_than_charted(self, value):
        """A chart of NaN is worse than a chart with a hole in it."""
        assert em.parse_prometheus_text(f"m 1\nm2 {value}\n").keys() == {"m"}

    def test_repeated_names_are_kept_as_separate_series(self):
        text = 'm{le="1"} 2\nm{le="2"} 5\n'

        assert [s.value for s in em.parse_prometheus_text(text)["m"]] == [2.0, 5.0]


# ── Which names mean what ────────────────────────────────────────────────────


class TestNames:
    def test_the_named_engine_is_tried_first(self):
        assert em.names_for("vllm")[0] is em.VLLM_METRICS
        assert em.names_for("sglang")[0] is em.SGLANG_METRICS

    def test_the_other_engine_is_still_tried(self):
        """An engine name is what the recipe called the image, not a promise."""
        assert set(em.names_for("vllm")) == {em.VLLM_METRICS, em.SGLANG_METRICS}

    @pytest.mark.parametrize("engine", [None, "", "llamacpp"])
    def test_an_unknown_engine_falls_back_to_trying_both(self, engine):
        assert set(em.names_for(engine)) == {em.VLLM_METRICS, em.SGLANG_METRICS}

    def test_an_engine_name_is_matched_case_insensitively(self):
        assert em.names_for("VLLM")[0] is em.VLLM_METRICS

    def test_a_body_from_the_other_engine_is_still_read(self):
        """A vLLM-labelled deployment serving SGLang metrics still charts."""
        reading = em.read_families(em.parse_prometheus_text(SGLANG_BODY), "vllm")

        assert reading.running == 4.0
        assert reading.kv_fraction == 0.31

    def test_the_older_vllm_cache_gauge_is_read_when_the_new_one_is_absent(self):
        text = 'vllm:gpu_cache_usage_perc{engine="0"} 0.9\n'

        assert (
            em.read_families(em.parse_prometheus_text(text), "vllm").kv_fraction == 0.9
        )

    def test_the_new_name_wins_outright_when_both_are_present(self):
        """Adding two names for one quantity would double-count it."""
        text = "vllm:kv_cache_usage_perc 0.4\nvllm:gpu_cache_usage_perc 0.9\n"

        assert (
            em.read_families(em.parse_prometheus_text(text), "vllm").kv_fraction == 0.4
        )


class TestReading:
    def test_a_vllm_body_becomes_the_six_numbers(self):
        reading = em.read_families(em.parse_prometheus_text(VLLM_BODY), "vllm")

        assert reading.running == 3.0
        assert reading.waiting == 11.0
        assert reading.kv_fraction == 0.42
        assert reading.prompt_tokens_total == 1000.0
        assert reading.generation_tokens_total == 500.0
        assert reading.preemptions_total == 2.0

    def test_requests_are_summed_across_engines_but_a_fraction_is_not(self):
        """Two data-parallel engines run 5 requests between them and are not
        180 percent full."""
        text = (
            'vllm:num_requests_running{engine="0"} 2\n'
            'vllm:num_requests_running{engine="1"} 3\n'
            'vllm:kv_cache_usage_perc{engine="0"} 0.9\n'
            'vllm:kv_cache_usage_perc{engine="1"} 0.9\n'
        )

        reading = em.read_families(em.parse_prometheus_text(text), "vllm")

        assert reading.running == 5.0
        assert reading.kv_fraction == 0.9

    def test_an_endpoint_with_nothing_we_know_reads_as_empty(self):
        reading = em.read_families(
            em.parse_prometheus_text("python_gc_objects_collected_total 12\n"), "vllm"
        )

        assert reading.running is None
        assert em._is_empty(reading)

    def test_a_missing_metric_stays_none_rather_than_becoming_zero(self):
        reading = em.read_families(
            em.parse_prometheus_text("vllm:num_requests_running 1\n"), "vllm"
        )

        assert reading.running == 1.0
        assert reading.waiting is None
        assert reading.kv_fraction is None

    def test_a_reading_serialises_every_field(self):
        payload = em.Reading(t=1.0, running=2.0).to_dict()

        assert payload["t"] == 1.0
        assert payload["running"] == 2.0
        assert payload["counter_reset"] is False
        assert "generation_tokens_per_second" in payload


# ── Rates, and the counter reset that is not a spike ─────────────────────────


class TestRates:
    def test_a_rate_is_the_difference_over_the_elapsed_time(self):
        assert em._rate(100.0, 10.0, 150.0, 15.0) == (10.0, False)

    def test_a_counter_that_went_backwards_yields_no_rate_and_says_so(self):
        """The engine restarted. The tokens before it are unknowable."""
        rate, reset = em._rate(500.0, 10.0, 3.0, 15.0)

        assert rate is None
        assert reset is True

    def test_a_reset_never_becomes_a_negative_rate(self):
        rate, _ = em._rate(500.0, 10.0, 0.0, 15.0)

        assert rate is None or rate >= 0

    @pytest.mark.parametrize(
        "previous, current", [(None, 1.0), (1.0, None), (None, None)]
    )
    def test_a_rate_needs_two_numbers(self, previous, current):
        assert em._rate(previous, 1.0, current, 2.0) == (None, False)

    @pytest.mark.parametrize("dt", [0.0, -1.0])
    def test_no_elapsed_time_is_no_rate_rather_than_a_division(self, dt):
        assert em._rate(1.0, 10.0, 2.0, 10.0 + dt) == (None, False)

    def test_the_first_reading_has_no_rates_because_it_has_no_predecessor(self):
        first = em.Reading(t=1.0, prompt_tokens_total=10.0)

        assert em.with_rates(first, None) is first
        assert first.prompt_tokens_per_second is None

    def test_all_three_counters_are_differenced(self):
        previous = em.Reading(
            t=0.0,
            prompt_tokens_total=0.0,
            generation_tokens_total=0.0,
            preemptions_total=0.0,
        )
        current = em.Reading(
            t=10.0,
            prompt_tokens_total=100.0,
            generation_tokens_total=50.0,
            preemptions_total=5.0,
        )

        rated = em.with_rates(current, previous)

        assert rated.prompt_tokens_per_second == 10.0
        assert rated.generation_tokens_per_second == 5.0
        assert rated.preemptions_per_second == 0.5
        assert rated.counter_reset is False

    def test_one_counter_resetting_flags_the_whole_reading(self):
        previous = em.Reading(
            t=0.0, prompt_tokens_total=900.0, generation_tokens_total=0.0
        )
        current = em.Reading(
            t=5.0, prompt_tokens_total=10.0, generation_tokens_total=50.0
        )

        rated = em.with_rates(current, previous)

        assert rated.counter_reset is True
        assert rated.prompt_tokens_per_second is None
        # The counter that did not reset still has an honest rate.
        assert rated.generation_tokens_per_second == 10.0

    def test_the_gauges_survive_a_reset_because_they_are_not_differenced(self):
        previous = em.Reading(t=0.0, prompt_tokens_total=900.0)
        current = em.Reading(t=5.0, prompt_tokens_total=1.0, running=2.0, waiting=7.0)

        rated = em.with_rates(current, previous)

        assert rated.running == 2.0
        assert rated.waiting == 7.0


# ── The ring ─────────────────────────────────────────────────────────────────


class TestRing:
    def test_it_is_bounded_and_drops_the_oldest(self):
        ring = em.MetricsRing(size=3)
        for i in range(10):
            ring.append(em.Reading(t=float(i)))

        assert len(ring) == 3
        assert [r.t for r in ring.readings()] == [7.0, 8.0, 9.0]

    def test_appending_computes_the_rate_against_the_previous_reading(self):
        ring = em.MetricsRing()
        ring.append(em.Reading(t=0.0, generation_tokens_total=0.0))

        stamped = ring.append(em.Reading(t=10.0, generation_tokens_total=200.0))

        assert stamped.generation_tokens_per_second == 20.0
        assert ring.last() is stamped

    def test_an_empty_ring_has_no_last_reading(self):
        assert em.MetricsRing().last() is None
        assert em.MetricsRing().readings() == []

    def test_clearing_empties_it(self):
        ring = em.MetricsRing()
        ring.append(em.Reading(t=1.0))

        ring.clear()

        assert len(ring) == 0

    def test_concurrent_appends_all_land(self):
        ring = em.MetricsRing(size=200)

        def writer():
            for i in range(50):
                ring.append(em.Reading(t=float(i)))

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(ring) == 200


# ── Addressing ───────────────────────────────────────────────────────────────


class TestAddressing:
    def test_the_url_is_the_readiness_host_with_the_metrics_path_on_it(self):
        assert em.metrics_url(record()) == "http://127.0.0.1:9000/metrics"

    def test_a_path_without_a_leading_slash_still_makes_a_url(self):
        assert em.metrics_url(record(metrics_path="metrics")) == (
            "http://127.0.0.1:9000/metrics"
        )

    def test_without_a_readiness_url_the_port_is_enough(self):
        assert em.metrics_url(record(readiness_url=None)) == (
            "http://127.0.0.1:9000/metrics"
        )

    def test_a_readiness_url_we_cannot_split_falls_back_to_the_port(self):
        assert em.metrics_url(record(readiness_url="not a url")) == (
            "http://127.0.0.1:9000/metrics"
        )

    def test_no_path_and_no_engine_is_no_url(self):
        assert em.metrics_url(record(metrics_path=None, engine=None)) is None

    def test_no_port_and_no_readiness_url_is_no_url(self):
        assert em.metrics_url(record(readiness_url=None, port=None)) is None

    def test_an_older_record_falls_back_to_the_engine_registry(self):
        """Records written before ``metrics_path`` was persisted still resolve."""
        url = em.metrics_url(record(metrics_path=None))

        assert url == "http://127.0.0.1:9000/metrics"

    def test_an_engine_the_registry_does_not_know_yields_no_path(self):
        assert em._engine_metrics_path(record(metrics_path=None, engine="nope")) is None


class TestDisabledAtLaunch:
    def test_sglang_without_the_flag_publishes_nothing(self):
        assert em.metrics_disabled_at_launch(
            record(engine="sglang", launch_command="python3 -m sglang.launch_server")
        )

    def test_sglang_with_the_flag_is_expected_to_publish(self):
        """The day the engine spec adds the flag, this stops reporting a fault."""
        assert not em.metrics_disabled_at_launch(
            record(
                engine="sglang",
                launch_command="python3 -m sglang.launch_server --enable-metrics",
            )
        )

    def test_vllm_is_never_disabled_this_way(self):
        assert not em.metrics_disabled_at_launch(record(engine="vllm"))

    def test_a_record_with_no_command_is_not_accused(self):
        assert not em.metrics_disabled_at_launch(
            record(engine="sglang", launch_command="")
        )


# ── Scraping ─────────────────────────────────────────────────────────────────


class Response:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class TestScrape:
    def test_a_body_comes_back_whole(self, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "get", lambda url, timeout: Response(200, "m 1\n"))

        assert em.scrape("http://x/metrics") == "m 1\n"

    def test_a_refused_connection_is_no_body_not_an_exception(self, monkeypatch):
        import httpx

        def _boom(url, timeout):
            raise OSError("connection refused")

        monkeypatch.setattr(httpx, "get", _boom)

        assert em.scrape("http://x/metrics") is None

    @pytest.mark.parametrize("status", [404, 500])
    def test_an_error_status_is_no_body(self, monkeypatch, status):
        import httpx

        monkeypatch.setattr(httpx, "get", lambda url, timeout: Response(status, "no"))

        assert em.scrape("http://x/metrics") is None


# ── The sampler ──────────────────────────────────────────────────────────────


@pytest.fixture
def sampler(monkeypatch):
    """A sampler whose scrape is a dict lookup, not a socket."""
    bodies: dict[str, str | None] = {"http://127.0.0.1:9000/metrics": VLLM_BODY}
    monkeypatch.setattr(em, "scrape", lambda url, timeout=3.0: bodies.get(url))
    s = em.MetricsSampler(interval=0.01, ring_size=5)
    s.bodies = bodies  # type: ignore[attr-defined]
    return s


class TestSampling:
    def test_a_healthy_engine_yields_a_reading(self, sampler):
        reading = sampler.sample_deployment(record())

        assert reading is not None
        assert reading.running == 3.0
        assert sampler.availability("dep-1").available is True

    def test_a_second_sample_carries_the_rate(self, sampler):
        sampler.sample_deployment(record())
        sampler.bodies["http://127.0.0.1:9000/metrics"] = VLLM_BODY.replace(
            'vllm:generation_tokens_total{engine="0",model_name="Qwen/Qwen3-8B"} 500.0',
            'vllm:generation_tokens_total{engine="0",model_name="Qwen/Qwen3-8B"} 900.0',
        )

        second = sampler.sample_deployment(record())

        assert second.generation_tokens_per_second > 0
        assert second.counter_reset is False

    def test_an_engine_restart_is_a_break_not_a_negative_spike(self, sampler):
        sampler.sample_deployment(record())
        sampler.bodies["http://127.0.0.1:9000/metrics"] = (
            "vllm:num_requests_running 0\n"
            "vllm:prompt_tokens_total 0\n"
            "vllm:generation_tokens_total 0\n"
        )

        after = sampler.sample_deployment(record())

        assert after.counter_reset is True
        assert after.prompt_tokens_per_second is None
        assert after.generation_tokens_per_second is None

    def test_an_sglang_deployment_says_metrics_were_never_enabled(self, sampler):
        reading = sampler.sample_deployment(
            record(engine="sglang", launch_command="python3 -m sglang.launch_server")
        )

        assert reading is None
        availability = sampler.availability("dep-1")
        assert availability.available is False
        assert availability.reason == em.REASON_NOT_ENABLED
        assert "--enable-metrics" in availability.detail

    def test_a_deployment_with_no_endpoint_says_so(self, sampler):
        assert sampler.sample_deployment(record(metrics_path=None, engine=None)) is None

        assert sampler.availability("dep-1").reason == em.REASON_NO_ENDPOINT

    def test_an_engine_that_does_not_answer_says_so(self, sampler):
        assert sampler.sample_deployment(record(port=9999, readiness_url=None)) is None

        assert sampler.availability("dep-1").reason == em.REASON_UNREACHABLE

    def test_an_endpoint_with_nothing_we_recognise_says_so(self, sampler):
        sampler.bodies["http://127.0.0.1:9000/metrics"] = 'python_info{v="3"} 1\n'

        assert sampler.sample_deployment(record()) is None

        assert sampler.availability("dep-1").reason == em.REASON_UNRECOGNISED

    def test_no_unavailability_ever_appends_a_placeholder_reading(self, sampler):
        for rec in (
            record(engine="sglang", launch_command="python3 -m sglang.launch_server"),
            record(metrics_path=None, engine=None),
            record(port=9999, readiness_url=None),
        ):
            sampler.sample_deployment(rec)

        assert sampler.snapshot("dep-1")["samples"] == []

    def test_every_reason_has_a_sentence_for_the_operator(self):
        for reason in (
            em.REASON_NOT_RUNNING,
            em.REASON_NO_ENDPOINT,
            em.REASON_NOT_ENABLED,
            em.REASON_UNREACHABLE,
            em.REASON_UNRECOGNISED,
        ):
            assert em.Availability(False, reason).detail

    def test_an_available_reading_has_no_reason_or_detail(self):
        assert em.AVAILABLE.to_dict() == {
            "available": True,
            "reason": None,
            "detail": None,
        }


class TestSweep:
    def test_a_running_deployment_is_sampled_and_a_stopped_one_is_not(
        self, sampler, monkeypatch
    ):
        records = [record(), record(id="dep-2", status="stopped")]
        monkeypatch.setattr(tools.deployment_records, "load", lambda: list(records))

        sampler.sample_once()

        assert sampler.snapshot("dep-1")["available"] is True
        stopped = sampler.snapshot("dep-2")
        assert stopped["available"] is False
        assert stopped["reason"] == em.REASON_NOT_RUNNING
        assert stopped["samples"] == []

    def test_a_record_with_no_id_is_skipped(self, sampler, monkeypatch):
        monkeypatch.setattr(tools.deployment_records, "load", lambda: [record(id="")])

        sampler.sample_once()

        assert sampler.snapshot("")["samples"] == []

    def test_a_departed_deployment_stops_costing_memory(self, sampler, monkeypatch):
        records = [record()]
        monkeypatch.setattr(tools.deployment_records, "load", lambda: list(records))
        sampler.sample_once()
        assert sampler.snapshot("dep-1")["samples"]

        records.clear()
        sampler.sample_once()

        assert sampler.snapshot("dep-1")["samples"] == []
        assert sampler._rings == {}
        assert sampler._status == {}

    def test_a_store_that_cannot_be_read_does_not_kill_the_sweep(
        self, sampler, monkeypatch
    ):
        def _boom():
            raise OSError("deployments.json is a directory")

        monkeypatch.setattr(tools.deployment_records, "load", _boom)

        sampler.sample_once()  # must not raise

    def test_forgetting_one_deployment_leaves_the_others(self, sampler, monkeypatch):
        monkeypatch.setattr(
            tools.deployment_records,
            "load",
            lambda: [record(), record(id="dep-2", port=9001, readiness_url=None)],
        )
        sampler.bodies["http://127.0.0.1:9001/metrics"] = VLLM_BODY
        sampler.sample_once()

        sampler.forget("dep-1")

        assert sampler.snapshot("dep-1")["samples"] == []
        assert sampler.snapshot("dep-2")["samples"]


class TestSnapshot:
    def test_it_names_the_window_and_admits_it_is_volatile(self, sampler):
        sampler.sample_deployment(record())

        snapshot = sampler.snapshot("dep-1")

        assert snapshot["deployment_id"] == "dep-1"
        assert snapshot["available"] is True
        assert snapshot["sample_interval_seconds"] == 0.01
        assert snapshot["window_seconds"] == pytest.approx(0.05)
        # The UI has to be able to say "this is lost on a restart".
        assert snapshot["volatile"] is True
        assert len(snapshot["samples"]) == 1

    def test_a_deployment_never_swept_reads_as_unreachable_with_no_samples(
        self, sampler
    ):
        snapshot = sampler.snapshot("never-seen")

        assert snapshot["available"] is False
        assert snapshot["reason"] == em.REASON_UNREACHABLE
        assert snapshot["samples"] == []

    def test_samples_are_plain_json(self, sampler):
        import json

        sampler.sample_deployment(record())

        json.dumps(sampler.snapshot("dep-1"))  # must not raise


class TestLifecycle:
    def test_the_thread_actually_runs_which_is_the_whole_point(self, monkeypatch):
        """The subsystem this replaced was constructed and never started."""
        swept = threading.Event()
        sampler = em.MetricsSampler(interval=0.01)
        monkeypatch.setattr(
            tools.deployment_records, "load", lambda: (swept.set(), [])[1]
        )

        sampler.start()
        try:
            assert swept.wait(2.0), "the sampler thread never swept"
            assert sampler.running
        finally:
            sampler.stop()

        assert not sampler.running

    def test_starting_twice_does_not_make_a_second_thread(self, monkeypatch):
        monkeypatch.setattr(tools.deployment_records, "load", lambda: [])
        sampler = em.MetricsSampler(interval=0.01)

        sampler.start()
        first = sampler._thread
        sampler.start()
        try:
            assert sampler._thread is first
        finally:
            sampler.stop()

    def test_stopping_one_that_never_started_is_harmless(self):
        em.MetricsSampler().stop()

    def test_a_failing_sweep_does_not_end_the_loop(self, monkeypatch):
        calls = []

        def _boom():
            calls.append(1)
            raise RuntimeError("nope")

        sampler = em.MetricsSampler(interval=0.01)
        monkeypatch.setattr(sampler, "sample_once", _boom)

        sampler.start()
        try:
            deadline = time.time() + 2
            while len(calls) < 2 and time.time() < deadline:
                time.sleep(0.01)
            assert len(calls) >= 2, "the loop stopped after one failure"
        finally:
            sampler.stop()


class TestSingleton:
    def test_there_is_exactly_one_accessor_and_it_is_stable(self):
        em.stop_sampler()
        try:
            assert em.get_sampler() is em.get_sampler()
        finally:
            em.stop_sampler()

    def test_start_returns_the_same_singleton(self):
        em.stop_sampler()
        sampler = em.start_sampler()
        try:
            assert sampler is em.get_sampler()
            assert sampler.running
        finally:
            em.stop_sampler()

    def test_stopping_drops_it_so_the_next_start_is_a_fresh_window(self):
        first = em.get_sampler()
        em.stop_sampler()

        assert em.get_sampler() is not first
        em.stop_sampler()

    def test_the_module_helpers_reach_the_singleton(self, monkeypatch):
        em.stop_sampler()
        try:
            em.get_sampler()._set_status("d", em.AVAILABLE)

            assert em.snapshot("d")["available"] is True

            em.forget("d")

            assert em.snapshot("d")["available"] is False
        finally:
            em.stop_sampler()
