"""Mock engine metrics — a simulated engine's Prometheus endpoint.

Simulation has no engine to scrape, so the parsing, the ring, the rate
arithmetic and the counter-reset rule are all the real module's — imported
here, not reimplemented — and only the *fetch* is replaced. That keeps the one
piece of logic worth getting right (a counter that goes backwards is a restart,
not a negative rate) under a single set of tests.

The simulated engine is deliberately not uniformly healthy. A vLLM deployment
publishes a growing set of counters, and an **SGLang deployment publishes
nothing at all**, with the reason attached — because that is what a real SGLang
deployment does today: the engine spec does not pass ``--enable-metrics``, so
the endpoint is not mounted. A simulator that showed SGLang serving metrics
would hide the one thing about this feature an operator most needs to know.
"""

from __future__ import annotations

from typing import Any

from spark_pulse.tools.engine_metrics import (
    AVAILABLE as AVAILABLE,
    REASON_DETAIL as REASON_DETAIL,
    REASON_NOT_ENABLED as REASON_NOT_ENABLED,
    REASON_NOT_RUNNING as REASON_NOT_RUNNING,
    REASON_NO_ENDPOINT as REASON_NO_ENDPOINT,
    REASON_UNREACHABLE as REASON_UNREACHABLE,
    REASON_UNRECOGNISED as REASON_UNRECOGNISED,
    RING_SIZE as RING_SIZE,
    SAMPLE_INTERVAL_SECONDS as SAMPLE_INTERVAL_SECONDS,
    SCRAPE_TIMEOUT_SECONDS as SCRAPE_TIMEOUT_SECONDS,
    SGLANG_METRICS as SGLANG_METRICS,
    VLLM_METRICS as VLLM_METRICS,
    Availability as Availability,
    METRIC_NAMES as METRIC_NAMES,
    MetricNames as MetricNames,
    MetricsRing as MetricsRing,
    MetricsSampler as _RealSampler,
    Reading as Reading,
    Series as Series,
    metrics_disabled_at_launch as metrics_disabled_at_launch,
    metrics_url as metrics_url,
    names_for as names_for,
    parse_prometheus_text as parse_prometheus_text,
    read_families as read_families,
    with_rates as with_rates,
)

#: The exposition body a simulated vLLM answers with. Rendered rather than
#: hand-written per sample so the mock exercises the same parser production
#: does — a change that breaks parsing breaks simulation too.
_VLLM_BODY = """\
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{{engine="0",model_name="{model}"}} {running}
# HELP vllm:num_requests_waiting Number of requests waiting to be processed.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{{engine="0",model_name="{model}"}} {waiting}
# HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{{engine="0",model_name="{model}"}} {kv}
# HELP vllm:prompt_tokens_total Number of prefill tokens processed.
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{{engine="0",model_name="{model}"}} {prompt}
# HELP vllm:generation_tokens_total Number of generation tokens processed.
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{{engine="0",model_name="{model}"}} {generation}
# HELP vllm:num_preemptions_total Cumulative number of preemptions.
# TYPE vllm:num_preemptions_total counter
vllm:num_preemptions_total{{engine="0",model_name="{model}"}} {preemptions}
# HELP vllm:e2e_request_latency_seconds End-to-end request latency.
# TYPE vllm:e2e_request_latency_seconds histogram
vllm:e2e_request_latency_seconds_bucket{{le="1.0",model_name="{model}"}} 12
vllm:e2e_request_latency_seconds_sum{{model_name="{model}"}} 41.5
vllm:e2e_request_latency_seconds_count{{model_name="{model}"}} 17
"""


def render_vllm_body(tick: int, model: str = "sim/model") -> str:
    """One simulated scrape, ``tick`` sweeps into the deployment's life.

    Deterministic on purpose: a simulated chart that wobbled at random would
    make a screenshot useless as evidence of anything.
    """
    running = tick % 5
    waiting = max(0, (tick % 11) - 7)
    kv = round(0.20 + 0.05 * (tick % 7), 4)
    return _VLLM_BODY.format(
        model=model,
        running=running,
        waiting=waiting,
        kv=kv,
        prompt=1000 + tick * 340,
        generation=500 + tick * 190,
        preemptions=tick // 20,
    )


class MetricsSampler(_RealSampler):
    """The real sampler with the network replaced by a rendered body.

    Everything else — which deployments are swept, when a window is dropped,
    how a rate is computed — is inherited, so simulation and production differ
    only in where the text came from.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._ticks: dict[str, int] = {}

    def sample_deployment(self, record: dict[str, Any]) -> Reading | None:
        dep_id = str(record.get("id") or "")
        # The one unavailability simulation must reproduce: SGLang really does
        # publish nothing, so it must look like nothing here too.
        if metrics_disabled_at_launch(record):
            self._set_status(dep_id, Availability(False, REASON_NOT_ENABLED))
            return None
        if metrics_url(record) is None:
            self._set_status(dep_id, Availability(False, REASON_NO_ENDPOINT))
            return None
        tick = self._ticks.get(dep_id, 0)
        self._ticks[dep_id] = tick + 1
        body = render_vllm_body(tick, str(record.get("model") or "sim/model"))
        reading = read_families(parse_prometheus_text(body), "vllm")
        self._set_status(dep_id, AVAILABLE)
        return self._ring(dep_id).append(reading)

    def _forget_all_but(self, keep: set[str]) -> None:
        super()._forget_all_but(keep)
        for gone in [k for k in self._ticks if k not in keep]:
            del self._ticks[gone]

    def forget(self, deployment_id: str) -> None:
        super().forget(deployment_id)
        self._ticks.pop(deployment_id, None)


def scrape(url: str, timeout: float = SCRAPE_TIMEOUT_SECONDS) -> str | None:
    """A simulated scrape. Nothing here reaches the network."""
    return render_vllm_body(0)


# ── Module singleton (mirrors spark_pulse.tools.engine_metrics) ──────────────

_sampler: MetricsSampler | None = None


def get_sampler() -> MetricsSampler:
    """The one simulated sampler."""
    global _sampler
    if _sampler is None:
        _sampler = MetricsSampler()
    return _sampler


def start_sampler() -> MetricsSampler:
    """Start the simulated sampler and return it."""
    sampler = get_sampler()
    sampler.start()
    return sampler


def stop_sampler() -> None:
    """Stop the simulated sampler and drop its window."""
    global _sampler
    sampler = _sampler
    _sampler = None
    if sampler is not None:
        sampler.stop()


def snapshot(deployment_id: str) -> dict[str, Any]:
    """The simulated metrics window for one deployment."""
    return get_sampler().snapshot(deployment_id)


def forget(deployment_id: str) -> None:
    """Forget one simulated deployment's window."""
    get_sampler().forget(deployment_id)


def reset() -> None:
    """Forget the sampler — one simulated machine per test."""
    global _sampler
    _sampler = None
