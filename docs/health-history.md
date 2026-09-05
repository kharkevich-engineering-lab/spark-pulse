# What a health history can honestly be built from

Status: research, 2026-09-04. Basis: the tree at `feat/phase-e-multinode`
(`e7c5f70`), the vLLM and SGLang metric exporters at their current releases, and
a survey of what comparable tools store versus scrape.

## Recommendation

**Build one thing: a backend scrape of the engine's own `/metrics`, kept in a
bounded in-memory ring in the control plane, exposed as a per-deployment series
on the Jobs page.** That is the only source in this system that is real,
already addressed (`engine.yaml` carries `metrics: /metrics`, the head rank is
reachable at `127.0.0.1:{port}`), and worth charting — queue depth, KV-cache
occupancy, token throughput and TTFT are the numbers that tell an operator
whether a model is healthy under load. Cost: roughly a 150-line tool module, a
router, and a chart; no new dependency, because `httpx` is already a first-party
dependency (`pyproject.toml:23`). **Keep the GPU sparkline that exists**, move
its accumulation from the browser into the same ring so it survives a reload,
and fix the one dishonesty in it (see §2.4). **Do not build a time-series
database, do not add SQLite, and do not persist samples to disk** — a
single-operator box managing one to four machines does not need retention, and
the moment somebody wants retention the answer is Prometheus, which the engines
already speak natively and we would only reimplement badly. **Stop advertising
restarts and check success rate.** Neither exists as a series, and one of the
two cannot exist because the record that would carry it is overwritten in place
(§3). For anything past an hour, add one paragraph of documentation pointing at
`prometheus.yaml` scraping `host:port/metrics` — the engines ship the compose
files and the dashboards already (§5.1).

There is a prerequisite the recommendation depends on and which is worth doing
regardless: **the health monitor does not run.** It is constructed and never
started, tracks nothing, broadcasts nowhere, and the SSE stream that reads it
calls a method that does not exist. §2.2 gives the details. Any "check success
rate" story starts by making the checks happen at all.

---

## 1. The engines publish Prometheus metrics, and we already know the URL

### 1.1 What we have wired

`EngineSpec.runtime.metrics` is a real field (`spark_pulse/engines/base.py:78`),
surfaced by `Engine.metrics_path()` (`base.py:357-358`) and returned by the
engines router as `"metrics"` (`spark_pulse/routers/engines.py:135`). Both
bundled engines declare it:

| Engine | `metrics` | API port | File |
|---|---|---|---|
| vLLM | `/metrics` | 8000 | `spark_pulse/engines/defaults/vllm.yaml:46,47` |
| SGLang | `/metrics` | 30000 | `spark_pulse/engines/defaults/sglang.yaml:23,24` |

`DeployPlan` carries `metrics_path` (`spark_pulse/tools/native_runtime.py:252`),
set from the engine at `native_runtime.py:1093`. **It is never read.** Nothing
in the tree fetches it; the field is planned and dropped. It is also not
persisted into the deployment record (`_record_from_plan`,
`native_runtime.py:1113-1149`) — but `engine`, `variant` and `port` are, so the
path is re-derivable from the record without a schema change.

Both engines run with `network_host: true` (`vllm.yaml:55`, `sglang.yaml:37`),
so the API port is a host port, and the head rank is assumed local: the plan's
`readiness_url` is hardcoded to `http://127.0.0.1:{port}{readiness}`
(`native_runtime.py:1092`) and `status()` probes it directly
(`native_runtime.py:2043-2044`). **A scrape of the head's `/metrics` is
therefore the same one-line HTTP GET as the readiness probe already in
`probe_ready` (`native_runtime.py:1260-1275`), against the same host, with no
SSH and no credentials.**

### 1.2 What vLLM actually exposes

Verified against vLLM v0.28.0 and `main`; the docs page is generated from
[`vllm/v1/metrics/loggers.py`](https://github.com/vllm-project/vllm/blob/main/vllm/v1/metrics/loggers.py),
so the source is the documentation
([Production Metrics](https://docs.vllm.ai/en/latest/usage/metrics/),
[design doc](https://docs.vllm.ai/en/latest/design/metrics/)). Every metric
carries labels `model_name` and `engine`.

| What an operator wants | Metric | Type |
|---|---|---|
| Queue depth | `vllm:num_requests_waiting` (plus `vllm:num_requests_waiting_by_reason`, label `reason` ∈ `capacity`/`deferred`) | Gauge |
| In-flight work | `vllm:num_requests_running` | Gauge |
| KV cache occupancy | `vllm:kv_cache_usage_perc` — a **fraction 0–1** despite the `_perc` suffix | Gauge |
| Prompt / generation throughput | `vllm:prompt_tokens_total`, `vllm:generation_tokens_total` | Counter |
| TTFT | `vllm:time_to_first_token_seconds` | Histogram |
| Inter-token latency | `vllm:inter_token_latency_seconds` | Histogram |
| Per-request TPOT | `vllm:request_time_per_output_token_seconds` | Histogram |
| End-to-end latency | `vllm:e2e_request_latency_seconds` | Histogram |
| Preemptions | `vllm:num_preemptions_total` | Counter |
| Prefix cache | `vllm:prefix_cache_queries_total` and `vllm:prefix_cache_hits_total`, counted in tokens | Counters |
| Outcomes | `vllm:request_success_total`, label **`finished_reason`** ∈ `stop`/`length`/`abort` | Counter |
| Queue time, prefill time, decode time | `vllm:request_queue_time_seconds`, `vllm:request_prefill_time_seconds`, `vllm:request_decode_time_seconds` | Histograms |

Enablement: **none needed.** vLLM's own reference README states Prometheus
metric logging is enabled by default in the OpenAI-compatible server
([`examples/observability/prometheus_grafana/README.md`](https://github.com/vllm-project/vllm/blob/main/examples/observability/prometheus_grafana/README.md)),
mounted onto the same FastAPI app as the API
([`vllm/entrypoints/serve/instrumentator/metrics.py`](https://github.com/vllm-project/vllm/blob/main/vllm/entrypoints/serve/instrumentator/metrics.py)),
i.e. exactly `http://127.0.0.1:8000/metrics`. `--disable-log-stats` turns it
off.

Two version traps worth writing into whatever we build:

* `vllm:gpu_cache_usage_perc`, `vllm:gpu_prefix_cache_queries` and
  `vllm:gpu_prefix_cache_hits` were renamed by
  [PR #18354](https://github.com/vllm-project/vllm/pull/18354), survived to
  v0.11.0 and are **gone as of v0.12.0**. Our pinned build is 0.28.1
  (`vllm.yaml:10`), so the new names are the right ones — but a recipe pointing
  at an older image will expose the old ones.
* `vllm:time_per_output_token_seconds` was split into
  `vllm:inter_token_latency_seconds` and
  `vllm:request_time_per_output_token_seconds`, and is gone as of v0.15.0.
* **There is no tokens-per-second gauge.** `vllm:avg_prompt_throughput_toks_per_s`
  and `vllm:avg_generation_throughput_toks_per_s` were deprecated by
  [PR #2764](https://github.com/vllm-project/vllm/pull/2764) and removed by
  [PR #12383](https://github.com/vllm-project/vllm/pull/12383). Throughput must
  be differenced from the counters ourselves.

### 1.3 What SGLang exposes

Verified against SGLang v0.5.18 and `main`
([Production Metrics](https://docs.sglang.ai/references/production_metrics),
[`metrics_collector.py`](https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/observability/metrics_collector.py)).
Our pinned image is v0.5.10.post1 (`sglang.yaml:8`).

**`/metrics` requires `--enable-metrics`.** Without it `add_prometheus_middleware`
is never called and the route does not exist. Our SGLang spec does not pass it
(`sglang.yaml` `runtime.serve: python3 -m sglang.launch_server`), so **an SGLang
deployment made today exposes nothing**, and any scrape must either add the flag
to the engine's rendered arguments or report "metrics not enabled" honestly.
Same port as the API, default 30000.

The useful gauges: `sglang:num_running_reqs`, `sglang:num_queue_reqs`,
`sglang:token_usage`, `sglang:kv_used_tokens` / `sglang:kv_available_tokens`,
`sglang:num_retracted_reqs`, `sglang:num_paused_reqs`. Counters:
`sglang:prompt_tokens_total`, `sglang:generation_tokens_total`,
`sglang:num_requests_total`, `sglang:num_aborted_requests_total`. Histograms:
`sglang:time_to_first_token_seconds`, `sglang:inter_token_latency_seconds`,
`sglang:e2e_request_latency_seconds`, `sglang:queue_time_seconds`.

Two differences from vLLM that matter to a single-scrape reader:

* **`sglang:gen_throughput` is a gauge already in tokens/second**, and
  **`sglang:cache_hit_rate` is a gauge already expressed as a rate.** vLLM
  deliberately deleted both shapes. So a cross-engine abstraction cannot assume
  symmetry: for vLLM we difference counters, for SGLang we read a gauge.
* `sglang:time_per_output_token_seconds` no longer exists — renamed to
  `sglang:inter_token_latency_seconds`. The published docs example output is
  stale and still shows the old name; do not code against the docs page.

### 1.4 Histograms are not percentiles, and one scrape is not a chart

**Neither engine exposes a single pre-computed percentile.** Grepping both
exporters, `prometheus_client.Summary` is used exactly once in SGLang
(`sglang:eplb_balancedness`, MoE expert balance) and never in vLLM. Everything
latency-shaped is a Histogram, exposed as cumulative `_bucket`/`_sum`/`_count`
series.

That has a hard consequence for anything we build:

* A p95 TTFT requires `histogram_quantile(0.95, sum by(le) (rate(..._bucket[w])))`
  — a **range** query over at least two scrapes. It is not readable from one
  fetch, and it is not something a browser can compute from a single frame.
* From one scrape you can honestly derive: the gauges (queue depth, running,
  KV usage), and a lifetime mean `_sum / _count`. Nothing else.
* From two consecutive scrapes you can honestly derive: throughput
  (`Δcounter / Δt`), preemption rate, prefix-cache hit ratio over the interval,
  and a windowed mean latency (`Δ_sum / Δ_count`). This is the level a modest
  built-in view should stop at.
* Implementing `histogram_quantile` ourselves is reimplementing Prometheus. It
  is also the point at which the honest answer becomes "run Prometheus".

Also: cumulative counters reset to zero when the engine restarts. Any
differencing must detect a decrease and drop that interval rather than plotting
a negative rate. That is a real correctness requirement, not a nicety.

### 1.5 Browser or backend?

**Backend.** Three reasons, in order of decisiveness:

1. The browser cannot reach the engine. The SPA talks only to our own origin
   (`web/src/lib/api.ts` has no absolute URLs at all), the engine listens on the
   Spark's host network on port 8000/30000 with no CORS headers we control, and
   the operator's laptop is not necessarily on the same network as a worker
   node. There is no proxy to the engine in the tree today
   (`spark_pulse/routers/deployments.py` has none).
2. Parsing the Prometheus text exposition format and differencing counters
   across engine restarts belongs in one place, tested, not in a React effect.
3. The backend already holds the address. `record["port"]` plus the engine
   resolved from `record["engine"]`/`record["variant"]` gives the URL with no
   new state.

---

## 2. What we already collect and throw away

### 2.1 `/sse/metrics` — every 5 s, nothing retained

`metrics_generator` (`spark_pulse/sse.py:20-45`) emits one frame every five
seconds, containing exactly `system.get_all_memory()`
(`spark_pulse/tools/system.py:346-353`) plus process tracking:

| Field | Source | Per-GPU? |
|---|---|---|
| `memory_total`, `memory_used`, `memory_free`, `memory_supported` | `nvidia-smi --query-gpu` (`system.py:143-180`) | yes |
| `temperature` | same | yes |
| `utilization` | same | yes |
| `power_draw`, `power_limit` | same | yes |
| `cpu.{total,used,free,available,usage_percent}` | `free -m` (`system.py:243-263`) | no |
| `disk[].{mount,total,used,free,usage_percent}` | `df -B1` (`system.py:266-297`) | no |
| `processes[].{gpu_uuid,pid,process_name,used_memory,is_tracked}` | `nvidia-smi --query-compute-apps` (`system.py:206-240`) plus cgroup matching (`system.py:98-135`) | per process |

**The frame carries no timestamp.** The server does not stamp it, so any series
built from it is stamped on arrival in the browser — receive time, not sample
time. That is fine at five-second cadence on a LAN, and it is a reason the ring
belongs on the server if we ever care.

**Nothing is stored.** The generator computes and yields; there is no buffer,
no file, no cache. Every field above is discarded the instant the frame is
written. The only consumer is `MemoryPage`, which now accumulates GPU
`utilization` and `temperature` into React state capped at 720 samples — one
hour of five-second frames (`web/src/pages/MemoryPage.tsx:17-18, 59-72`).

What is collected and thrown away that a chart could use today, at zero
additional collection cost: **CPU usage percent, disk usage percent, power draw,
and per-process GPU memory.** Power draw in particular is a genuine health
signal on this hardware and is already in every frame.

### 2.2 `HealthMonitor` — polls nothing, stores nothing, and is not running

The stated design is a 30-second poll broadcasting `DeploymentHealth`. What is
actually in the tree:

* `DeploymentHealth` (`spark_pulse/tools/health.py:112-122`) is
  `{deployment_id, container_status, process_status, error, checked_at}` — a
  point-in-time snapshot with no counters and no history.
* `HealthMonitor._check_deployment` (`health.py:234-267`) calls
  `docker.get_container_status` and derives `process_status` from it. That is
  the entire check: no readiness probe, no HTTP, no restart count.
* **The monitor is never started.** `app.py` builds its own singleton
  (`spark_pulse/app.py:51-58`), restores tracked deployments into it at startup
  (`app.py:206-216`) and never calls `.start()`. The router's
  `POST /api/health/monitor/start` (`spark_pulse/routers/health.py:63-70`)
  starts a **different** instance — the module-level singleton in
  `tools/health.py:271-288` — which has nothing tracked.
* **Nothing ever tracks a deployment.** `track_deployment` has exactly three
  call sites: the startup restore, the router endpoint, and tests. The deploy
  path never calls it, and the frontend never calls the health API at all
  (`web/src/lib/api.ts` contains no `/health` route).
* **Even if it ran, it would broadcast nowhere.** `sse_broadcast` defaults to
  `None` (`health.py:138`) and neither construction site passes one.
* **`/sse/health` is broken.** `health_events_generator` calls
  `monitor.get_all_health()` (`spark_pulse/sse.py:317`); no such method exists
  on either the real or the mock `HealthMonitor`. The stream emits an error
  frame every 30 seconds. The only definition of `get_all_health` in the tree is
  a stub inside `tests/test_sse_coverage.py:661`.

So the honest statement is: **the health monitor collects nothing today, because
it does not run.** There is no discarded health series to recover — there is no
series.

The frontend types encode an aspiration the backend never had: `DeploymentHealth`
in `web/src/lib/operations.ts:208-217` declares `status`, `gpu_errors`,
`restart_count`, `last_check`, `warnings`, `errors` — six fields, of which the
backend produces none by that name. `HealthBadge`, `HealthAlert` and
`HealthMonitorControls` are imported only by their own test file. This is where
"restarts" and "check success rate" came from.

### 2.3 The GB10 memory quirk — a memory chart would plot nothing

`nvidia-smi` reports no GPU memory on a DGX Spark: total, used and free all come
back `[N/A]`, because the GPU shares the 121 GB unified pool. NVIDIA confirms
this is expected — "nvidia-smi only reports memory utilization when there is a
dedicated GPU VRAM" — and recommends `top`, `htop`, `free` or the DGX Dashboard
instead
([NVIDIA developer forum](https://forums.developer.nvidia.com/t/dear-nvidia-nvidia-smi-is-broken-on-the-dgx-spark/367765),
[NVIDIA support answer 5775](https://nvidia.custhelp.com/app/answers/detail/a_id/5775/)).
On GB10 `nvmlDeviceGetMemoryInfo` can return ~121 GB, but that figure does not
reflect allocatable memory; usable capacity tracks `MemAvailable`
([NVML unified-memory thread](https://forums.developer.nvidia.com/t/nvml-support-for-dgx-spark-grace-blackwell-unified-memory-community-solution/358869)).

We handle it in two places, and both are correct:

* `tools/preflight.py` preserves `[N/A]` as `None` rather than coercing to zero
  (`parse_gpu_query`, `preflight.py:379-410`), and `_check_gpu`
  (`preflight.py:817-895`) reads `MemAvailable` from `/proc/meminfo` instead,
  never failing a node for a figure `nvidia-smi` declined to give. The plan
  states the reasoning at `preflight.py:38-44` and
  `docs/cluster-agent-plan.md:663-670`.
* `tools/system.py:180` sets `memory_supported: false`, and `MemoryPage`
  renders "Unified memory — usage not reported by nvidia-smi" instead of a bar
  (`web/src/pages/MemoryPage.tsx:134-141`).

**Consequence for a chart: a GPU-memory series is empty on this hardware, always
— not sometimes, not on some models.** It must not be offered. The fields that
do report are `utilization`, `temperature` and `power_draw`; NVIDIA's threads
name only memory as unsupported, and the community GB10 monitor Sparkview reads
utilization, temperature, power and clocks successfully while substituting
`MemAvailable` for memory
([Sparkview thread](https://forums.developer.nvidia.com/t/sparkview-gpu-monitor-tool-with-gb10-aware-unified-memory-handling/366877)).
The two series the current chart plots are exactly the two that work. That was
the right call.

If we want a memory series it has to come from `/proc/meminfo` `MemAvailable`,
labelled as host memory, not GPU memory — which is what `get_cpu_stats`
(`system.py:243-263`) already returns as `available`, and already ships in every
frame.

### 2.4 One dishonesty in the current chart

`sparklinePath` (`web/src/components/HealthBadge.tsx:198-215`) spaces points
**by index**: `x = (i / (samples.length - 1)) * VIEW_W`. The timestamps in
`HealthSample.t` are used for the caption and for nothing else. `EventSource`
reconnects automatically after a network drop, so a stream that stalls for ten
minutes and resumes produces two adjacent points ten minutes apart, drawn as a
straight line one pixel wide from the previous sample — a gap rendered as
continuity. The component's own comment says "It never invents a point", which
is true of the points and false of the line between them.

Two honest fixes, either acceptable: scale `x` by timestamp so a gap is visibly
wide, or break the path when the interval exceeds some multiple of the expected
cadence. The second is better — a gap should look like a gap, not like a long
flat stretch.

Note also that in simulation the mock returns a constant `utilization: 45`
(`spark_pulse/mock/system.py:26`), so the series is flat, and `sparklinePath`
draws a flat series down the middle (`HealthBadge.tsx:206-211`). Nothing wrong
with that; worth knowing before reading a dev screenshot as evidence.

---

## 3. Restarts and check success rate

### 3.1 Restarts: a counter, not a series, and the history is overwritten

`generation` is a real integer on the deployment record
(`native_runtime.py:1145`), incremented per start attempt by `_next_generation`
(`native_runtime.py:318-324`), stamped onto every container as a label
(`tools/labels.py:49,65`) and used to name containers
`spark-pulse-<id>-r<rank>-g<generation>` so an abandoned attempt is reapable
(`native_runtime.py:307-315`).

It counts attempts correctly. It is not a series, for a specific reason: the
record holds **one** `started_at` and **one** `stopped_at`
(`native_runtime.py:1123-1124`), both overwritten on each transition
(`native_runtime.py:1802`, `1762`, `1876`, `1941`). Generation 4 tells you there
were four attempts; it does not tell you when attempts 1 to 3 happened or why
they ended. **The timestamps of past generations are destroyed as they are
superseded.** No amount of reading the current file recovers them.

Additionally, containers never restart on their own — `restart_policy: {"Name":
"no"}` (`tools/docker.py:499-503`), deliberately, because a rank is one member of
a sharded gang. So Docker's own `RestartCount` is structurally always zero and
is not read anywhere. "Restarts" in the Docker sense do not happen in this
product.

**What would make it a series:** append a row per generation instead of
overwriting — `{generation, started_at, stopped_at, outcome, error_message}` —
into the existing record as a bounded list. That is a small, contained change to
`_record_from_plan` and `_update_record`, it reuses the atomic-write and
retention machinery already there (`tools/atomic_json.py`,
`deployment_records.purge_expired` at `deployment_records.py:114-138`,
`job_retention_days: 7` in `config.yaml:8`), and it produces an event list, not
a chart. **An event list is the right shape**: four restarts in an hour is a
row per restart with a reason, not a line graph. Recommended, at low priority,
and explicitly not as a chart.

### 3.2 Check success rate: no checks, therefore no rate

Per §2.2, no health check runs. Two candidate check signals exist in the tree
and neither is sampled periodically today:

* `probe_ready` (`native_runtime.py:1260-1275`) — an HTTP GET on the engine's
  readiness path. Called during deploy (`_wait_ready`, `native_runtime.py:1277-1311`)
  and once per `status()` call (`native_runtime.py:2043-2044`), i.e. whenever the
  UI happens to ask. Not on a timer, not recorded.
* `docker.get_container_status` — what `HealthMonitor` would call if it ran.

**What would make it a series:** start the monitor, give it the readiness probe
rather than only the container state, and append pass/fail into the same ring as
everything else. Then "check success rate" is `passes / total` over the ring's
window, which is honest.

But be clear about what it would show. On a box where an engine either serves or
is down, this series is 100% for days and then 0%, and its information content is
identical to the deployment's status badge. **A success-rate percentage over a
one-hour window is a worse presentation of "is it up" than the word "running".**
The useful artefact is not a rate — it is the *transitions*: a timestamped list
of when readiness went from pass to fail and back. Same data, honest shape,
empty most of the time and correctly so.

---

## 4. Where a series could live

Sizing, so the options are argued against a number rather than a feeling. At a
5-second cadence, one hour is 720 points; 24 hours is 17,280. A point is a
float and a timestamp — call it 16 bytes in memory, more like 40 as JSON.

| Series set | Series | 1 h in RAM | 24 h in RAM |
|---|---|---|---|
| Current chart (util + temp, 1 GPU) | 2 | ~23 KB | ~550 KB |
| Add power, CPU, disk | 5 | ~58 KB | ~1.4 MB |
| Add 6 engine metrics per deployment × 4 deployments | 29 | ~334 KB | ~8 MB |

**In-memory ring buffer in the control plane — recommended.** A
`collections.deque(maxlen=N)` per series, filled by one background thread on the
cadence we already poll at. An hour costs tens of kilobytes; a day costs single-
digit megabytes. It survives a page reload, which is the actual complaint about
the current chart, and it dies on a backend restart, which is honest and easy to
say in a caption. It needs no new dependency, no schema, no migration, no
retention policy, and no corruption-recovery path. It is the same shape as
`sparkDash`, the existing DGX Spark dashboard, which keeps a central in-memory
store for sparklines fed by a 2-second WebSocket poll and accepts losing it on
restart ([MiaAI-Lab/sparkDash](https://github.com/MiaAI-Lab/sparkDash)), and the
same shape as `nvtop`, which draws live GPU graphs in memory only and points at
DCGM-exporter → Prometheus for anything over time
([nvtop](https://github.com/Syllo/nvtop)).

**The existing `~/.config/spark-pulse/` JSON state — no, for samples.** The
machinery is good: `write_json_atomic` with a directory fsync
(`tools/atomic_json.py:67-116`), quarantine of corrupt files, `filelock` in
`benchmarking.py:23`, retention in `deployment_records.purge_expired`. But it
rewrites the whole file on every write. A 5-second sample cadence means a full
rewrite and fsync every five seconds, forever, on the operator's boot SSD, to
store data whose value expires in an hour. That is the wrong trade. This store
is right for the *events* in §3.1 — a handful of rows per deployment, written
when something happens — and wrong for samples.

**SQLite — the strongest of the rejected options, and the documented upgrade
path.** There is no `sqlite3` usage anywhere in the tree today. Adding it buys
durable retention across restarts and indexed queries. It is not expensive:
twelve scalars per node across four nodes is 48 series; at 5 s that is
`86,400 / 5 × 48 = 829,440` points a day, which is **13.3 MB/day** at a naive
16 bytes per point (float64 value plus int64 timestamp) and **3.3 MB/day** at
4 bytes with implicit fixed-step timestamps. A month at raw 5 s resolution with
no rollup at all is a few hundred megabytes.

Three products of roughly our shape did build this, and their designs are the
template if we ever need it:

* **Proxmox VE** — the closest structural analogue, a small self-hosted control
  plane with built-in per-node graphs. `pvestatd` → `rrdcached` → fixed-size RRD
  files. PVE 8's archives were 5 × 70 rows (1 min for an hour, 30 min for a day,
  3 h for a week, 12 h for a month, 7 d for a year) — a few KB per data source,
  fixed size forever. PVE 9 widened them, taking node files from 79 KB to 1.4 MB.
  Anything beyond that, Proxmox pushes to an external InfluxDB or Graphite
  ([PVE devel thread](https://lore.proxmox.com/pve-devel/20250726010626.1496866-26-a.lauterer@proxmox.com/T/)).
* **Beszel** — explicitly positioned as "no Prometheus stack, no external
  database": a single Go binary with embedded SQLite, hub plus agent under
  50 MiB, with a cron-driven rollup ladder (1 min kept 1 hour → 10 min kept
  12 hours → 20 min kept 24 hours → 2 h kept 7 days → 8 h kept 30 days), pruning
  finer data as it goes; roughly 100–500 MB of SQLite per month per server
  ([Beszel](https://beszel.dev/guide/gpu)).
* **GPUStack** — a `system_loads` table (`timestamp, cluster_id, cpu, ram, gpu,
  vram`), a collector snapshotting every 60 s, and a dashboard API returning
  current load plus a **one-hour** historical series
  ([GPUStack](https://github.com/gpustack/gpustack),
  [`system_load.py`](https://github.com/gpustack/gpustack/blob/main/gpustack/schemas/system_load.py)).

I am still saying no, for one reason: **all three serve retention we have not
been asked for.** GPUStack's persisted table serves a one-hour window — the exact
window an in-memory ring serves for a fraction of the code, no schema, no
migration, and no corruption path. Proxmox's and Beszel's ladders exist because
somebody wants last month, and a rollup ladder is a real product with real
decisions (what to average, what to drop, how to draw a gap in an averaged
bucket). Build the ring; if "what happened last night" becomes a question people
actually ask, this section is the design, and the cost is single-digit megabytes
a day. Note also that a 30-day answer at 8-hour resolution is a different product
from a live chart, not a longer version of one.

**Scraping into Prometheus — the right answer for retention, and not our code.**
The engines already speak it, and both ship a `docker-compose.yaml` +
`prometheus.yaml` + Grafana dashboard in-tree (§5.1). Prometheus' own storage
docs give `needed_disk_space = retention_time_seconds × ingested_samples_per_second
× bytes_per_sample` at 1–2 bytes per sample
([storage docs](https://prometheus.io/docs/prometheus/latest/storage/)); four
nodes at roughly 1,500 series each scraped every 5 s is 1,200 samples/s, so
**~155 MB for 24 h and ~1.1 GB for 7 days**, plus 150–300 MB RSS for Prometheus
(~7.5 KiB per series,
[Robust Perception](https://www.robustperception.io/how-much-ram-does-prometheus-2-x-need-for-cardinality-and-ingestion/))
and Grafana's own 512 MB floor
([Grafana install docs](https://grafana.com/docs/grafana/latest/setup-grafana/installation/)).
That is a real cost for a Spark, which is why it should be opt-in and documented
rather than bundled. Our contribution is a `prometheus.yaml` snippet with the
right targets.

Two variants that do not help: **Prometheus agent mode** and **Grafana Alloy**
are shippers, not stores — agent mode "disables ... TSDB, alerting, and rule
evaluations", offers "No local queries", and discards data immediately after a
successful remote write
([agent docs](https://prometheus.io/docs/prometheus/latest/prometheus_agent/)).
They are cheap precisely because they store nothing locally, which is the one
thing a local history chart needs.

**Netdata is the calibration point for "how expensive is your own TSDB", and it
argues for the ring.** Its dbengine is a genuinely good design — multi-tier,
append-only, Gorilla delta-of-delta plus ZSTD, all tiers written simultaneously
with no compaction window, ~0.5–0.6 bytes per tier-0 sample on disk — and it
still defaults to 1 GiB per tier × 3 tiers, about 4 GiB, and
`UNIQUE_METRICS × 16 KiB + 32 MiB` of RAM (≈110 MiB at 5,000 metrics)
([database docs](https://learn.netdata.cloud/docs/netdata-agent/database),
[disk & retention](https://learn.netdata.cloud/docs/netdata-agent/resource-utilization/disk-&-retention),
[RAM](https://learn.netdata.cloud/docs/netdata-agent/resource-utilization/ram)).
That cost buys thousands of dimensions per host and per-metric ML anomaly models
— about 26 KiB per active metric, of which ~5 KiB is the ML model. We would be
paying the architecture and using 48 series of it.

---

## 5. What comparable tools do

### 5.1 The engines and the GPU exporter: scrape-only, by design

**vLLM has no UI.** `/metrics` and nothing else. Its docs say plainly that
history is the operator's problem: "A Prometheus instance can then be configured
to poll this endpoint and record the values in its time-series database.
Prometheus is often used via Grafana, allowing these metrics to be graphed over
time" ([design doc](https://docs.vllm.ai/en/stable/design/metrics/)). The
in-tree example is a `docker-compose.yaml` starting Prometheus on :9090 and
Grafana on :3000, a `prometheus.yaml`, and a 12-panel `grafana.json` you import
by hand
([README](https://github.com/vllm-project/vllm/blob/main/examples/observability/prometheus_grafana/README.md)).
The panel list is the closest thing to a specification of what an LLM operator
actually wants: E2E latency quantiles, token throughput, inter-token latency,
scheduler state (running/waiting), TTFT, cache utilization, prompt and
generation length heatmaps, finish reason, queue time, prefill/decode time.
SGLang's equivalent has 8 panels of the same kind
([`examples/monitoring`](https://github.com/sgl-project/sglang/tree/main/examples/monitoring)).

**NVIDIA's dcgm-exporter stores nothing.** It is a stateless Go process exposing
`/metrics` on :9400; every value is read from DCGM at scrape time
([README](https://github.com/NVIDIA/dcgm-exporter),
[NVIDIA telemetry docs](https://docs.nvidia.com/datacenter/cloud-native/gpu-telemetry/latest/dcgm-exporter.html)).
Its default counter set
([`etc/default-counters.csv`](https://raw.githubusercontent.com/NVIDIA/dcgm-exporter/main/etc/default-counters.csv))
is richer than `nvidia-smi` gives us — SM and memory clocks, GPU and memory
temperature, power usage and cumulative energy, GPU/memory-copy/encoder/decoder
utilization, PCIe replay counters, NVLink bandwidth, and the health counters that
matter most: `DCGM_FI_DEV_XID_ERRORS` and the row-remap counters.

Two caveats before anyone suggests we adopt it:

* **The reference experience is Kubernetes.** Dashboard 12239 lists prerequisites
  of a K8s 1.13+ cluster, dcgm-exporter, a Prometheus with a ServiceMonitor, and
  Grafana, and NVIDIA's own guide installs `kube-prometheus-stack` via Helm
  ([dashboard 12239](https://grafana.com/grafana/dashboards/12239-nvidia-dcgm-exporter-dashboard/),
  [kube-prometheus guide](https://docs.nvidia.com/datacenter/cloud-native/gpu-telemetry/latest/kube-prometheus.html)).
  A single-host path does exist — `docker run --gpus all --cap-add SYS_ADMIN -p
  9400:9400 nvcr.io/nvidia/k8s/dcgm-exporter:<ver>-distroless`, and the package
  ships an `nvidia-dcgm-exporter.service` unit — but it gets you the endpoint and
  no UI at all.
* **Its framebuffer metrics are unusable on GB10 for the same reason ours are.**
  `DCGM_FI_DEV_FB_USED`/`_FREE` come from the same NVML per-device memory query
  that returns "Not Supported" on a unified-memory iGPU (§2.3). DGX OS also
  already runs `nv-hostengine`, so an exporter would have to point at the host
  engine rather than start its own. dcgm-exporter would give us XID errors and
  clocks that we do not have today; it would not solve memory.

**Ray is the strongest precedent, and it punts.** Ray's own docs are blunt: *"Ray
doesn't provide a native storage solution for metrics. Users need to manage the
lifecycle of the metrics by themselves"*, *"Ray doesn't start Prometheus servers
for you"*, and *"The Metrics view requires the Prometheus and Grafana setup"*
([Ray metrics](https://docs.ray.io/en/latest/cluster/metrics.html),
[dashboard config](https://github.com/ray-project/ray/blob/master/doc/source/cluster/configure-manage-dashboard.md)).
The time-series panels in the Ray dashboard are **Grafana iframes**, requiring
`RAY_PROMETHEUS_HOST`, `RAY_GRAFANA_HOST` and `RAY_GRAFANA_IFRAME_HOST`, plus
`allow_embedding = true` and `cookie_samesite = none` in Grafana's own config.
With Prometheus absent, the Metrics view is simply unavailable and the Cluster
view falls back to a **snapshot** of CPU/GPU/memory/disk/network per machine —
no time series of any kind. A far larger project with a far bigger team chose
"snapshot in our own UI, iframe Grafana for history", and made the user stand up
both services.

### 5.2 Single-machine LLM UIs: almost none of them have this

This is the closest category to our product, and the survey is one-sided:

* **Ollama** has no `/metrics` at all. It is a two-year-old open request —
  [#3144](https://github.com/ollama/ollama/issues/3144), opened March 2024, still
  open, with a working community implementation in
  [PR #6537](https://github.com/ollama/ollama/pull/6537) that remains unmerged.
  `GET /api/ps` returns `size`, `size_vram`, `expires_at` — allocation, not
  utilization, and no history. The desktop GUI is a chat window with no resource
  view.
* **LM Studio** is the instructive one: 3.x showed real-time CPU/GPU usage in the
  status bar and **4.0 removed it**
  ([bug-tracker #1422](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/1422),
  January 2026, unresolved). Its REST API has five endpoints, none
  hardware-related.
* **Open WebUI** has no `/metrics`; its only telemetry is an OTLP push to a
  collector, and the instruments are HTTP and GenAI token/cost/TTFT — **no GPU or
  system-resource metrics at all**
  ([OTel reference](https://docs.openwebui.com/reference/monitoring/otel/)). A
  system-monitoring widget request was closed with no maintainer response
  ([#11469](https://github.com/open-webui/open-webui/issues/11469)).
* **llama.cpp** persists conversations to IndexedDB, not metrics; `--metrics` is
  off by default and its gauges are scrape-only
  ([server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)).
* **Jan** shows live CPU/RAM/GPU gauges under Settings → Hardware with no
  historical tracking. **GPT4All** delegates monitoring entirely to OpenTelemetry.
  **LocalAI** exposes `/metrics` and puts history in a community Grafana
  dashboard. **RamaLama** points you at `podman stats` / `nvtop`.
* **Sparkview**, the GB10-specific community monitor, is live-only; it logs
  timestamped snapshots to `~/sparkview_logs/` **only when an anomaly is
  detected**, and the main display has no historical graphing
  ([forum thread](https://forums.developer.nvidia.com/t/sparkview-gpu-monitor-tool-with-gb10-aware-unified-memory-handling/366877)).

Two projects did build it, and they chose the two ends of the cheap spectrum:
**GPUStack** persists 60-second snapshots to a SQLite table and serves a
one-hour window (§4); **sparkDash** keeps an in-memory ring and loses it on
restart. **Neither built a time-series database.** That is the whole finding.

### 5.3 The pattern, and what to do with it

Sorted by what they store, the survey splits cleanly:

| Stores nothing, punts to Prometheus | Keeps its own short history |
|---|---|
| vLLM, SGLang, llama.cpp, dcgm-exporter, Ollama (no endpoint at all), Open WebUI, **Ray** | Proxmox VE (fixed-size RRD), Beszel (SQLite + rollup ladder), GPUStack (SQLite, 1 h window), Netdata (its own TSDB), sparkDash (in-memory only), nvtop (in-memory only) |

The left column is components; the right column is control planes for a handful
of machines with one operator — our shape. So the survey does not say "never
keep history". It says: keep a *short* one, cheaply, and let anyone who wants
retention run Prometheus. The cheapest members of the right column
(sparkDash, nvtop) keep it in memory and lose it on restart; that is where we
should start, and §4 has the design for the next step if it is ever needed.

**So: point people at Prometheus — and show them this instead.**

Yes, for retention. The honest sentence is: *if you want to know what happened
last Tuesday, run Prometheus and Grafana; both engines ship a compose file and a
dashboard.* We should write that sentence in the docs and stop there. Ray, which
is far larger than us, writes exactly that sentence and then iframes somebody
else's Grafana.

But an operator opening our monitoring page has a different question, and it is
never "what happened last Tuesday". It is one of:

1. **Is it working right now, and is it struggling?** → the gauges, live:
   `num_requests_running`, `num_requests_waiting`, `kv_cache_usage_perc`, GPU
   utilization, power draw. A queue that is 40 deep is the single most useful
   number on the page and we do not show it at all today.
2. **Has it been like this for the last few minutes, or did it just change?** →
   a short sparkline behind each of those gauges. This is what the in-memory ring
   is for, and an hour is more than enough.
3. **Did something go wrong, and when?** → an event list: readiness transitions,
   start attempts and their outcomes, preemption spikes. Not a chart. Empty most
   of the time, which is the correct appearance for a healthy machine.

That is the built-in view: **live gauges, short sparklines behind them, and an
event list.** It is honest at every size, it needs no storage, and it does not
pretend to be a fleet monitor.

---

## 6. The three things the placeholder promised

The placeholder advertised GPU utilization, restarts, and check success rate.

| Promise | Verdict |
|---|---|
| **GPU utilization** | **Real, and shipped.** `nvidia-smi` reports `utilization.gpu` on GB10 (unlike memory, §2.3), it arrives in every `/sse/metrics` frame, and `MemoryPage` charts it. Two things to fix: move the accumulation server-side so it survives a reload, and stop drawing straight lines across stream gaps (§2.4). Add power draw and host `MemAvailable` alongside it — both already in the frame, both free. |
| **Restarts** | **Not a series and cannot become one from what is stored.** `generation` counts attempts (`native_runtime.py:318-324`) but `started_at`/`stopped_at` are single fields overwritten on each transition, so every earlier attempt's timing is already destroyed. Docker restarts are structurally zero — `restart_policy: no` by design (`docker.py:499-503`). **Stop advertising it as a chart.** If it is wanted, append a bounded per-generation event list to the record and render it as a list of events with reasons. |
| **Check success rate** | **Not a series, because no check runs.** The health monitor is never started, tracks nothing, broadcasts nowhere, and the SSE endpoint that reads it calls a method that does not exist (§2.2). **Stop advertising it**, and do not resurrect it as a percentage even after the monitor is fixed: on a single box the series is 100% until it is 0%, which is the status badge with extra steps. Fix the monitor, then show readiness *transitions* as events. |

Two further deletions, while we are being blunt: `HealthAlert` and
`HealthMonitorControls` (`web/src/components/HealthBadge.tsx:81-151`) are
imported only by their own test file, and the `DeploymentHealth` interface in
`web/src/lib/operations.ts:208-217` describes a payload the backend has never
produced — it is where `restart_count` and `gpu_errors` came from. Deleting them
removes the last place these promises are written down.

---

## 7. Nothing here interpolates

For the record, since it constrains every option above:

* Counter differencing must discard an interval where the counter decreased
  (engine restart), not plot a negative rate.
* A gap in a sample stream must be drawn as a gap, not bridged (§2.4).
* A GPU-memory series on GB10 is empty always, not sometimes; it must not be
  offered (§2.3).
* A readiness-transition list is empty on a healthy machine for days at a time.
  That is the correct appearance, and it should say "no events in the last N
  hours" rather than being hidden or padded.
* Fewer than two real samples is not a chart. The current component already
  gets this right (`HealthBadge.tsx:191, 276-296`).
