"""Read the engine's own Prometheus endpoint into a bounded in-memory ring.

Both bundled engines publish a Prometheus text endpoint and we already know
where: ``EngineSpec.runtime.metrics`` carries the path, the head rank listens
on the host port the readiness probe already hits, and ``httpx`` is already a
dependency. Until now that address was computed into ``DeployPlan.metrics_path``
and dropped on the floor. This module is the reader.

**What is stored, and for how long.** One :class:`MetricsRing` per running
deployment, ``RING_SIZE`` readings deep at ``SAMPLE_INTERVAL_SECONDS`` apart —
an hour, tens of kilobytes. It lives in this process and nowhere else. There is
no database, no file, no retention policy and no migration: a restart of the
control plane loses the window, which the UI says out loud rather than
implying a history that survives. Anyone who wants last Tuesday should point
Prometheus at the same endpoint; both engines ship a compose file and a
dashboard for exactly that, and reimplementing it here would be a worse
Prometheus.

**What is *not* computed.** Neither engine exposes a pre-computed percentile —
every latency is a cumulative histogram, and a p95 needs a range query over
several scrapes plus ``histogram_quantile``. Deriving one from a bucket
midpoint would be inventing a number, so this module does not read the
histograms at all. What it reads is what the gauges and counters say directly:
how many requests are running, how many are queued, how full the KV cache is,
how many preemptions there have been, and how many tokens have gone through.

**Throughput is differenced, and a reset is not a spike.** vLLM deliberately
removed its tokens-per-second gauges, so token rates only exist as a difference
of two counter samples. A cumulative counter resets to zero when the engine
restarts; the difference is then negative, which is not a rate and must never
be plotted as one. :func:`_rate` returns ``None`` for such an interval and the
reading is flagged ``counter_reset``, so the chart breaks the line rather than
drawing a cliff. SGLang does publish ``sglang:gen_throughput`` as a gauge
already in tokens/second, but this module differences its counters like vLLM's:
one code path means one reset rule, and a rate an operator reads off two
engines should have been computed the same way in both.

**Honest unavailability.** SGLang only mounts ``/metrics`` when the server is
started with ``--enable-metrics``, and the bundled SGLang spec — which lives in
the separate ``spark-pulse-engine`` repository — does not pass it. A deployment
of that engine therefore publishes nothing at all. That is reported as an
unavailability with the reason attached rather than as an empty chart, and the
same is true of an engine we cannot address, one that refuses the connection,
and one whose endpoint answers with no metric this module recognises. Nothing
here ever appends a placeholder reading.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# ── Sizing ───────────────────────────────────────────────────────────────────

#: Seconds between scrapes. Matches ``/sse/metrics``, so the two live views on
#: a page move together rather than beating against each other.
SAMPLE_INTERVAL_SECONDS = 5.0

#: Readings kept per deployment — one hour at the interval above. A reading is
#: a handful of floats, so four deployments cost well under a megabyte.
RING_SIZE = 720

#: How long a single scrape may take before it is abandoned for this tick.
SCRAPE_TIMEOUT_SECONDS = 3.0


# ── Why there is nothing to show ─────────────────────────────────────────────

REASON_NOT_RUNNING = "not_running"
REASON_NO_ENDPOINT = "no_endpoint"
REASON_NOT_ENABLED = "not_enabled"
REASON_UNREACHABLE = "unreachable"
REASON_UNRECOGNISED = "unrecognised"

#: Reason → the sentence the UI shows. Written for an operator, not a log.
REASON_DETAIL: dict[str, str] = {
    REASON_NOT_RUNNING: (
        "This deployment is not running, so there is no engine to ask. "
        "Metrics are collected only while it serves."
    ),
    REASON_NO_ENDPOINT: (
        "This engine declares no metrics endpoint, so there is nothing to read."
    ),
    REASON_NOT_ENABLED: (
        "SGLang serves /metrics only when it is started with --enable-metrics, "
        "and the bundled SGLang engine spec does not pass that flag. Until the "
        "spec adds it, an SGLang deployment publishes no metrics at all — this "
        "is not a fault on this machine, and no number here is being withheld."
    ),
    REASON_UNREACHABLE: (
        "The engine did not answer its metrics endpoint. It may still be "
        "starting, or it may have been launched with metrics turned off."
    ),
    REASON_UNRECOGNISED: (
        "The endpoint answered, but published no metric this build knows how "
        "to read. Engines rename metrics between releases; an image far from "
        "the one the engine spec pins can do this."
    ),
}


@dataclass(frozen=True)
class Availability:
    """Whether a deployment's metrics can be read, and why not when they cannot."""

    available: bool
    reason: str | None = None

    @property
    def detail(self) -> str | None:
        return REASON_DETAIL.get(self.reason or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "detail": self.detail,
        }


AVAILABLE = Availability(True)


# ── The Prometheus text exposition format, only as far as we need it ─────────


@dataclass(frozen=True)
class Series:
    """One ``name{labels} value`` line."""

    name: str
    labels: dict[str, str]
    value: float


def _parse_labels(raw: str) -> dict[str, str]:
    """Split ``a="1",b="2"`` into a dict, tolerating what we do not understand.

    Label values may contain escaped quotes and commas, so this walks the
    string rather than splitting on punctuation.
    """
    labels: dict[str, str] = {}
    i = 0
    n = len(raw)
    while i < n:
        eq = raw.find("=", i)
        if eq == -1:
            break
        key = raw[i:eq].strip()
        j = raw.find('"', eq)
        if j == -1:
            break
        value_chars: list[str] = []
        j += 1
        while j < n:
            ch = raw[j]
            if ch == "\\" and j + 1 < n:
                nxt = raw[j + 1]
                value_chars.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
                j += 2
                continue
            if ch == '"':
                break
            value_chars.append(ch)
            j += 1
        if key:
            labels[key] = "".join(value_chars)
        comma = raw.find(",", j)
        if comma == -1:
            break
        i = comma + 1
    return labels


def parse_prometheus_text(text: str) -> dict[str, list[Series]]:
    """Group a Prometheus exposition body by metric name.

    Comments, ``HELP``/``TYPE`` lines, blank lines and anything unparseable are
    skipped. ``NaN``/``+Inf``/``-Inf`` are dropped rather than carried into
    arithmetic: a chart of NaN is worse than a chart with a hole in it.
    """
    families: dict[str, list[Series]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("{"):  # a metric with no name is not one we can use
            continue
        brace = line.find("{")
        if brace == -1:
            parts = line.split()
            if len(parts) < 2:
                continue
            name, raw_value, labels = parts[0], parts[1], {}
        else:
            close = line.rfind("}")
            if close < brace:
                continue
            name = line[:brace].strip()
            labels = _parse_labels(line[brace + 1 : close])
            rest = line[close + 1 :].split()
            if not rest:
                continue
            raw_value = rest[0]
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if value != value or value in (float("inf"), float("-inf")):
            continue
        families.setdefault(name, []).append(Series(name, labels, value))
    return families


def _first_present(
    families: dict[str, list[Series]], names: Iterable[str]
) -> list[Series] | None:
    """The first of ``names`` the endpoint actually published.

    Engines rename metrics between releases, so each concept is a list of
    candidates in newest-first order. Nothing is merged across two names: if
    both an old and a new name are present the newer one wins outright, because
    adding them would double-count the same quantity.
    """
    for name in names:
        series = families.get(name)
        if series:
            return series
    return None


def _sum(families: dict[str, list[Series]], names: Iterable[str]) -> float | None:
    """Total across label sets — right for counters and for request counts."""
    series = _first_present(families, names)
    if series is None:
        return None
    return float(sum(s.value for s in series))


def _max(families: dict[str, list[Series]], names: Iterable[str]) -> float | None:
    """Highest across label sets — right for a fraction, which cannot be added."""
    series = _first_present(families, names)
    if series is None:
        return None
    return float(max(s.value for s in series))


# ── What each engine calls the things we read ────────────────────────────────


@dataclass(frozen=True)
class MetricNames:
    """One engine's names for the six quantities this module reads.

    Each field is a tuple of candidates, newest first, because both engines
    have renamed metrics inside the version range an operator can pin.
    """

    running: tuple[str, ...]
    waiting: tuple[str, ...]
    kv_fraction: tuple[str, ...]
    prompt_tokens: tuple[str, ...]
    generation_tokens: tuple[str, ...]
    preemptions: tuple[str, ...]


#: vLLM. ``kv_cache_usage_perc`` is a fraction from 0 to 1 despite its suffix;
#: ``gpu_cache_usage_perc`` is the name it had before v0.12.0 and is kept as a
#: fallback for a recipe pinning an older image.
VLLM_METRICS = MetricNames(
    running=("vllm:num_requests_running",),
    waiting=("vllm:num_requests_waiting",),
    kv_fraction=("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"),
    prompt_tokens=("vllm:prompt_tokens_total",),
    generation_tokens=("vllm:generation_tokens_total",),
    preemptions=("vllm:num_preemptions_total",),
)

#: SGLang. ``token_usage`` is its fraction-shaped KV gauge, and a *retracted*
#: request is its name for what vLLM calls a preemption.
SGLANG_METRICS = MetricNames(
    running=("sglang:num_running_reqs",),
    waiting=("sglang:num_queue_reqs",),
    kv_fraction=("sglang:token_usage",),
    prompt_tokens=("sglang:prompt_tokens_total",),
    generation_tokens=("sglang:generation_tokens_total",),
    preemptions=("sglang:num_retracted_reqs",),
)

METRIC_NAMES: dict[str, MetricNames] = {
    "vllm": VLLM_METRICS,
    "sglang": SGLANG_METRICS,
}


def names_for(engine: str | None) -> tuple[MetricNames, ...]:
    """The name maps to try, most likely first.

    An engine we have a map for is tried first and the other is still tried
    after it, because an engine name is what the recipe called the image, not
    proof of what the image serves.
    """
    known = METRIC_NAMES.get((engine or "").lower())
    rest = tuple(m for m in (VLLM_METRICS, SGLANG_METRICS) if m is not known)
    return ((known,) + rest) if known is not None else rest


# ── One instant, and the rates between two of them ───────────────────────────


@dataclass(frozen=True)
class Reading:
    """What the engine said at one moment, plus the rates since the last one.

    Every field is either a number the endpoint published or a rate computed
    from two published numbers. ``None`` means the engine did not say, and is
    never filled in.
    """

    t: float
    running: float | None = None
    waiting: float | None = None
    kv_fraction: float | None = None
    prompt_tokens_total: float | None = None
    generation_tokens_total: float | None = None
    preemptions_total: float | None = None
    prompt_tokens_per_second: float | None = None
    generation_tokens_per_second: float | None = None
    preemptions_per_second: float | None = None
    #: A counter went backwards since the previous reading — the engine
    #: restarted. The rates across that interval are ``None``, not negative.
    counter_reset: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_families(families: dict[str, list[Series]], engine: str | None) -> Reading:
    """Turn one scrape into a reading, with no rates yet."""
    for names in names_for(engine):
        reading = Reading(
            t=time.time(),
            running=_sum(families, names.running),
            waiting=_sum(families, names.waiting),
            kv_fraction=_max(families, names.kv_fraction),
            prompt_tokens_total=_sum(families, names.prompt_tokens),
            generation_tokens_total=_sum(families, names.generation_tokens),
            preemptions_total=_sum(families, names.preemptions),
        )
        if not _is_empty(reading):
            return reading
    return Reading(t=time.time())


def _is_empty(reading: Reading) -> bool:
    """Whether a reading carries no published number at all."""
    return all(
        getattr(reading, field) is None
        for field in (
            "running",
            "waiting",
            "kv_fraction",
            "prompt_tokens_total",
            "generation_tokens_total",
            "preemptions_total",
        )
    )


def _rate(
    previous: float | None,
    previous_t: float,
    current: float | None,
    current_t: float,
) -> tuple[float | None, bool]:
    """``(rate, counter_reset)`` for one counter across one interval.

    A counter that went backwards did not go backwards: the process behind it
    restarted and started again from zero. There is no rate for that interval —
    the tokens served before the restart are unknowable — so this returns
    ``None`` and says a reset happened. It never returns a negative rate.
    """
    if previous is None or current is None:
        return None, False
    if current < previous:
        return None, True
    dt = current_t - previous_t
    if dt <= 0:
        return None, False
    return (current - previous) / dt, False


def with_rates(reading: Reading, previous: Reading | None) -> Reading:
    """``reading`` with its three counter rates filled in against ``previous``."""
    if previous is None:
        return reading
    prompt, reset_prompt = _rate(
        previous.prompt_tokens_total, previous.t, reading.prompt_tokens_total, reading.t
    )
    generation, reset_generation = _rate(
        previous.generation_tokens_total,
        previous.t,
        reading.generation_tokens_total,
        reading.t,
    )
    preemptions, reset_preemptions = _rate(
        previous.preemptions_total, previous.t, reading.preemptions_total, reading.t
    )
    return Reading(
        t=reading.t,
        running=reading.running,
        waiting=reading.waiting,
        kv_fraction=reading.kv_fraction,
        prompt_tokens_total=reading.prompt_tokens_total,
        generation_tokens_total=reading.generation_tokens_total,
        preemptions_total=reading.preemptions_total,
        prompt_tokens_per_second=prompt,
        generation_tokens_per_second=generation,
        preemptions_per_second=preemptions,
        counter_reset=reset_prompt or reset_generation or reset_preemptions,
    )


# ── The ring ─────────────────────────────────────────────────────────────────


class MetricsRing:
    """A bounded, in-memory window of readings for one deployment.

    Losing this on a restart is the deliberate trade: no file, no schema, no
    corruption path, no retention policy.
    """

    def __init__(self, size: int = RING_SIZE):
        self._readings: deque[Reading] = deque(maxlen=size)
        self._lock = threading.Lock()

    def append(self, reading: Reading) -> Reading:
        """Append ``reading``, computing its rates against the previous one."""
        with self._lock:
            previous = self._readings[-1] if self._readings else None
            stamped = with_rates(reading, previous)
            self._readings.append(stamped)
            return stamped

    def last(self) -> Reading | None:
        with self._lock:
            return self._readings[-1] if self._readings else None

    def readings(self) -> list[Reading]:
        with self._lock:
            return list(self._readings)

    def clear(self) -> None:
        with self._lock:
            self._readings.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._readings)


# ── Addressing the engine ────────────────────────────────────────────────────


def _engine_metrics_path(record: dict[str, Any]) -> str | None:
    """The metrics path for a deployment record.

    Records written by this build carry it. An older record does not, so the
    engine registry is asked — and if the spec has since gone, there is no path
    and that is said rather than guessed.
    """
    persisted = record.get("metrics_path")
    if persisted:
        return str(persisted)
    engine = record.get("engine")
    if not engine:
        return None
    try:
        from spark_pulse.engines.registry import get_registry

        return (
            get_registry()
            .engine(str(engine), str(record.get("variant") or "default"))
            .metrics_path()
        )
    except Exception as exc:  # pragma: no cover - registry specific
        logger.debug("no engine spec for %s: %s", engine, exc)
        return None


def metrics_url(record: dict[str, Any]) -> str | None:
    """Where to scrape a deployment, or ``None`` if it has no endpoint.

    The head rank runs with the host's network, so its API port is a host port
    and the readiness probe already reaches it at ``127.0.0.1``. The scrape is
    the same address with the metrics path on it — no SSH, no credentials, no
    new state.
    """
    path = _engine_metrics_path(record)
    if not path:
        return None
    if not path.startswith("/"):
        path = "/" + path
    readiness = record.get("readiness_url")
    if readiness:
        parts = urlsplit(str(readiness))
        if parts.scheme and parts.netloc:
            return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    port = record.get("port")
    if not port:
        return None
    return f"http://127.0.0.1:{port}{path}"


def metrics_disabled_at_launch(record: dict[str, Any]) -> bool:
    """Whether this deployment was started with its metrics endpoint off.

    Only SGLang can be: it mounts ``/metrics`` from ``--enable-metrics`` and
    serves nothing without it. The flag is looked for in the rendered launch
    command rather than assumed absent, so the day the engine spec adds it this
    stops reporting a problem on its own.
    """
    if str(record.get("engine") or "").lower() != "sglang":
        return False
    command = str(record.get("launch_command") or "")
    if not command:
        return False
    return "--enable-metrics" not in command


def scrape(url: str, timeout: float = SCRAPE_TIMEOUT_SECONDS) -> str | None:
    """The endpoint's body, or ``None`` when it did not give one."""
    import httpx

    try:
        response = httpx.get(url, timeout=timeout)
    except Exception as exc:
        logger.debug("metrics scrape failed for %s: %s", url, exc)
        return None
    if response.status_code >= 400:
        logger.debug("metrics scrape got %s from %s", response.status_code, url)
        return None
    return response.text


# ── The sampler ──────────────────────────────────────────────────────────────


class MetricsSampler:
    """One background thread scraping every running deployment on a timer.

    Unlike what it replaces, this is actually started — see
    ``spark_pulse.app.lifespan`` — and it discovers its subjects rather than
    being told about them, so nothing has to remember to register a deployment
    at creation and forget it at deletion.
    """

    def __init__(
        self,
        interval: float = SAMPLE_INTERVAL_SECONDS,
        ring_size: int = RING_SIZE,
    ):
        self._interval = interval
        self._ring_size = ring_size
        self._rings: dict[str, MetricsRing] = {}
        self._status: dict[str, Availability] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="engine-metrics", daemon=True
            )
            self._thread.start()
        logger.info("Engine metrics sampler started (every %.0fs)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=5)
        logger.info("Engine metrics sampler stopped")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Engine metrics sweep failed: %s", exc)
            self._stop.wait(self._interval)

    # -- sampling -------------------------------------------------------------

    def sample_once(self) -> None:
        """One sweep: scrape every running deployment, forget the departed."""
        from spark_pulse import tools

        try:
            records = tools.deployment_records.load()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Engine metrics could not list deployments: %s", exc)
            return

        live: set[str] = set()
        for record in records:
            dep_id = str(record.get("id") or "")
            if not dep_id:
                continue
            live.add(dep_id)
            if record.get("status") != "running":
                self._set_status(dep_id, Availability(False, REASON_NOT_RUNNING))
                continue
            self.sample_deployment(record)
        self._forget_all_but(live)

    def sample_deployment(self, record: dict[str, Any]) -> Reading | None:
        """Scrape one deployment, appending a reading only if it gave one."""
        dep_id = str(record.get("id") or "")
        if metrics_disabled_at_launch(record):
            self._set_status(dep_id, Availability(False, REASON_NOT_ENABLED))
            return None
        url = metrics_url(record)
        if not url:
            self._set_status(dep_id, Availability(False, REASON_NO_ENDPOINT))
            return None
        body = scrape(url)
        if body is None:
            self._set_status(dep_id, Availability(False, REASON_UNREACHABLE))
            return None
        reading = read_families(parse_prometheus_text(body), record.get("engine"))
        if _is_empty(reading):
            self._set_status(dep_id, Availability(False, REASON_UNRECOGNISED))
            return None
        self._set_status(dep_id, AVAILABLE)
        return self._ring(dep_id).append(reading)

    # -- state ----------------------------------------------------------------

    def _ring(self, deployment_id: str) -> MetricsRing:
        with self._lock:
            ring = self._rings.get(deployment_id)
            if ring is None:
                ring = MetricsRing(self._ring_size)
                self._rings[deployment_id] = ring
            return ring

    def _set_status(self, deployment_id: str, availability: Availability) -> None:
        with self._lock:
            self._status[deployment_id] = availability

    def _forget_all_but(self, keep: set[str]) -> None:
        """Drop the window of a deployment that no longer exists.

        A ring is bounded but the *number* of rings is not, so a control plane
        left running for a month would otherwise accumulate one per deployment
        ever made.
        """
        with self._lock:
            for gone in [k for k in self._rings if k not in keep]:
                del self._rings[gone]
            for gone in [k for k in self._status if k not in keep]:
                del self._status[gone]

    def forget(self, deployment_id: str) -> None:
        """Drop one deployment's window — called when it is deleted."""
        with self._lock:
            self._rings.pop(deployment_id, None)
            self._status.pop(deployment_id, None)

    def availability(self, deployment_id: str) -> Availability:
        with self._lock:
            return self._status.get(
                deployment_id, Availability(False, REASON_UNREACHABLE)
            )

    def snapshot(self, deployment_id: str) -> dict[str, Any]:
        """Everything the API says about one deployment's metrics window."""
        with self._lock:
            ring = self._rings.get(deployment_id)
            availability = self._status.get(deployment_id)
        readings = ring.readings() if ring is not None else []
        if availability is None:
            availability = Availability(False, REASON_UNREACHABLE)
        return {
            "deployment_id": deployment_id,
            **availability.to_dict(),
            "sample_interval_seconds": self._interval,
            "window_seconds": self._interval * self._ring_size,
            # Said out loud so the UI can: this window is in memory only.
            "volatile": True,
            "samples": [r.to_dict() for r in readings],
        }


# ── Module singleton ─────────────────────────────────────────────────────────

_sampler: MetricsSampler | None = None
_sampler_lock = threading.Lock()


def get_sampler() -> MetricsSampler:
    """The one sampler. There is deliberately no second accessor."""
    global _sampler
    with _sampler_lock:
        if _sampler is None:
            _sampler = MetricsSampler()
        return _sampler


def start_sampler() -> MetricsSampler:
    """Start the sampler and return it."""
    sampler = get_sampler()
    sampler.start()
    return sampler


def stop_sampler() -> None:
    """Stop the sampler and drop it, so the next start is a fresh window."""
    global _sampler
    with _sampler_lock:
        sampler = _sampler
        _sampler = None
    if sampler is not None:
        sampler.stop()


def snapshot(deployment_id: str) -> dict[str, Any]:
    """The metrics window for one deployment, as the API returns it."""
    return get_sampler().snapshot(deployment_id)


def forget(deployment_id: str) -> None:
    """Forget one deployment's window."""
    get_sampler().forget(deployment_id)
