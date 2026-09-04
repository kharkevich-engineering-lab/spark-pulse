# Engine metrics

Spark Pulse reads each running deployment's engine from the engine's own
Prometheus endpoint and keeps a short window of it in memory. This page says
what that window contains, what it deliberately does not contain, and where to
go when a short window is not enough.

Evidence for every claim below is in `docs/health-history.md` on the
`docs/health-history-sources` branch, which cites file and line.

## What is collected

A background sampler (`spark_pulse/tools/engine_metrics.py`), started by the
app's `lifespan` and stopped on shutdown, sweeps every **running** deployment
every **5 seconds**. For each one it derives the metrics URL from the record —
the head rank runs on the host's network, so it is the same `127.0.0.1:{port}`
the readiness probe already uses — fetches the Prometheus text body, and
appends one reading to a `collections.deque` holding **720 readings**: one hour
per deployment, tens of kilobytes.

Each reading carries only what the engine published directly, plus rates
differenced from its counters:

| Reading field | vLLM | SGLang |
|---|---|---|
| `running` | `vllm:num_requests_running` | `sglang:num_running_reqs` |
| `waiting` | `vllm:num_requests_waiting` | `sglang:num_queue_reqs` |
| `kv_fraction` (0–1) | `vllm:kv_cache_usage_perc` (`gpu_cache_usage_perc` before v0.12.0) | `sglang:token_usage` |
| `prompt_tokens_total` | `vllm:prompt_tokens_total` | `sglang:prompt_tokens_total` |
| `generation_tokens_total` | `vllm:generation_tokens_total` | `sglang:generation_tokens_total` |
| `preemptions_total` | `vllm:num_preemptions_total` | `sglang:num_retracted_reqs` |

`GET /api/deployments/{id}/metrics` returns the window. The Inference page
shows the newest reading as a row of gauges and the window behind them as
sparklines.

## What is deliberately not collected

**No persistence.** The window lives in one process and nowhere else.
Restarting Spark Pulse loses it, and the UI says so on the chart rather than
implying a history that survives. There is no database, no file, no retention
policy, no migration and no corruption-recovery path — because there is nothing
on disk to recover. If you want to know what happened last Tuesday, see
[Longer than an hour](#longer-than-an-hour).

**No percentiles.** Neither engine exposes a pre-computed percentile. Every
latency they publish is a cumulative histogram (`_bucket`/`_sum`/`_count`), and
a p95 requires `histogram_quantile` over a *range* of scrapes. Deriving one
from a bucket midpoint would be making up a number, so the histograms are not
read at all.

**No restart series.** A deployment record carries one `started_at` and one
`stopped_at`, overwritten on every transition, so the timing of every earlier
attempt is already destroyed. `generation` counts attempts; it cannot become a
series, and it is not drawn as one. (Docker's own `RestartCount` is
structurally zero here: ranks run with `restart_policy: no` by design, because
a rank is one member of a sharded gang.)

**No check success rate.** Nothing runs a periodic health check. On a single
box such a rate reads 100% until it reads 0%, which is the status badge with
extra steps.

## Two rules about honesty

**A counter reset is not a spike.** vLLM removed its tokens-per-second gauges,
so throughput exists only as a difference between two counter samples. When an
engine restarts its counters return to zero and the difference goes negative.
That interval has no rate — the tokens served before the restart are
unknowable — so the rate is `null`, the reading is flagged `counter_reset`, and
the chart breaks the line and shades the interval instead of drawing a cliff.

**A gap is not a straight line.** The sparkline's x axis is each sample's
timestamp, not its index. A silence — a browser reconnect, a sampler that could
not reach the engine — is drawn as wide as it was long, with the stroke cut and
the interval shaded. Nothing is interpolated across it.

## When there is nothing to show

The API answers with `available: false`, a machine-readable `reason` and a
sentence in `detail`, and the UI prints the sentence. The reasons:

| `reason` | Means |
|---|---|
| `not_running` | The deployment is not serving, so there is no engine to ask. |
| `no_endpoint` | The engine declares no metrics path. |
| `not_enabled` | SGLang started without `--enable-metrics` — see below. |
| `unreachable` | The endpoint did not answer. It may still be starting. |
| `unrecognised` | It answered, but published no metric this build knows. |

None of these ever appends a placeholder reading.

## Known gap: SGLang publishes nothing

SGLang mounts `/metrics` only when the server is launched with
`--enable-metrics`; without it `add_prometheus_middleware` is never called and
the route does not exist. **The bundled SGLang engine spec does not pass that
flag**, so an SGLang deployment made today publishes no metrics at all.

That spec lives in a different repository —
`spark-pulse-engine`, `engines/sglang/engine.yaml` — so it cannot be fixed from
here. Until it is, Spark Pulse reports the `not_enabled` reason with an
explanation rather than showing an empty chart. The check is made against the
deployment's own rendered launch command, so the day the spec adds the flag
this stops reporting a problem with no change on this side.

vLLM needs no such flag: its Prometheus endpoint is mounted on the same app as
the API and is on by default (`--disable-log-stats` turns it off).

## Longer than an hour

Run Prometheus. Both engines speak it natively and both ship a compose file, a
`prometheus.yaml` and a Grafana dashboard for exactly this; reimplementing that
here would be a worse Prometheus. A minimal scrape config:

```yaml
scrape_configs:
  - job_name: spark-pulse-engines
    scrape_interval: 5s
    static_configs:
      # One target per deployment: the host, and the deployment's API port.
      - targets: ["127.0.0.1:9000"]
```

The port is the one shown on the deployment's row. With Prometheus in front of
the same endpoint, `histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[5m])))`
is a real p95 — which is why this build does not pretend to offer one.
