import { HealthHistoryChart, type HealthSeries } from "@/components/HealthBadge";
import type { EngineMetricsWindow, EngineMetricSample } from "@/lib/types";

/**
 * What the engine itself says about its own load.
 *
 * Every number here was published by the engine's Prometheus endpoint or
 * differenced from two of its counters. Four things this deliberately does not
 * show:
 *
 * - **No percentiles.** Neither engine publishes one — every latency is a
 *   cumulative histogram, and a p95 needs a range query across several scrapes.
 *   Reading one off a bucket midpoint would be making it up. If you want
 *   percentiles, point Prometheus at the same endpoint.
 * - **No restart count.** The deployment record keeps a single `started_at`
 *   and overwrites it on every transition, so the timing of earlier attempts
 *   is already gone. It cannot be a series and is not drawn as one.
 * - **No check success rate.** On one box that number reads 100% until it
 *   reads 0%, which is the status badge with extra steps.
 * - **No line across a hole.** A counter that went backwards is an engine
 *   restart, so the rate across that interval is unknown, not zero: the sample
 *   carries no value and the chart breaks and shades the interval.
 */

/** Where the window lives, said plainly rather than implied. */
const VOLATILE_CAPTION =
  "Read from the engine's own /metrics endpoint and held in the control " +
  "plane's memory only. Restarting Spark Pulse loses this window; nothing " +
  "here is written to disk.";

/** Two samples is the least that can be drawn as a line. */
const MIN_SAMPLES = 2;

interface EngineMetricsPanelProps {
  window: EngineMetricsWindow | null;
  loading?: boolean;
  className?: string;
}

/** Epoch seconds from the backend; the chart's axis is milliseconds. */
function points(
  samples: EngineMetricSample[],
  pick: (s: EngineMetricSample) => number | null,
  scale = 1,
) {
  return samples
    .filter((s) => pick(s) !== null)
    .map((s) => ({ t: s.t * 1000, value: (pick(s) as number) * scale }));
}

/** The instants a line must not be drawn into, in chart milliseconds.
 *
 * A reset makes the rate across that interval unknowable. The sample after it
 * is a real measurement of the counter and stays on the chart; what is missing
 * is the rate, so the *line* stops there. */
function resetBreaks(samples: EngineMetricSample[]): number[] {
  return samples.filter((s) => s.counter_reset).map((s) => s.t * 1000);
}

export function engineSeries(window: EngineMetricsWindow): HealthSeries[] {
  const s = window.samples;
  const breaks = resetBreaks(s);
  return [
    {
      label: "Requests running",
      unit: "",
      color: "var(--color-primary)",
      samples: points(s, (x) => x.running),
    },
    {
      label: "Queue depth",
      unit: "",
      color: "var(--color-warning)",
      samples: points(s, (x) => x.waiting),
    },
    {
      // A fraction from 0 to 1 at the wire; shown as a percentage, which is a
      // change of unit, not of value.
      label: "KV cache used",
      unit: "%",
      color: "var(--color-success)",
      samples: points(s, (x) => x.kv_fraction, 100),
    },
    {
      label: "Output tokens/s",
      unit: "",
      color: "var(--color-primary)",
      samples: points(s, (x) => x.generation_tokens_per_second),
      breaks,
    },
    {
      label: "Prompt tokens/s",
      unit: "",
      color: "var(--color-text-muted)",
      samples: points(s, (x) => x.prompt_tokens_per_second),
      breaks,
    },
  ].filter((series) => series.samples.length > 0);
}

/** The newest value of each gauge — the "is it struggling right now" row. */
function LiveGauges({ latest }: { latest: EngineMetricSample }) {
  const cells: { label: string; value: string; title: string }[] = [
    {
      label: "Running",
      value: latest.running === null ? "—" : String(latest.running),
      title: "Requests the engine is decoding right now.",
    },
    {
      label: "Queued",
      value: latest.waiting === null ? "—" : String(latest.waiting),
      title: "Requests accepted but not yet running. A deep queue is the "
        + "clearest sign the model is saturated.",
    },
    {
      label: "KV cache",
      value:
        latest.kv_fraction === null
          ? "—"
          : `${(latest.kv_fraction * 100).toFixed(1)}%`,
      title: "How full the KV cache is. Near 100% the engine starts preempting.",
    },
    {
      label: "Preemptions",
      value:
        latest.preemptions_total === null ? "—" : String(latest.preemptions_total),
      title: "Requests evicted and restarted since the engine last started. "
        + "Cumulative, not a rate.",
    },
  ];
  return (
    <div
      className="grid gap-2 text-sm"
      style={{ gridTemplateColumns: "repeat(auto-fill, minmax(8rem, 1fr))" }}
    >
      {cells.map((cell) => (
        <div key={cell.label} className="p-2 rounded bg-bg" title={cell.title}>
          <span className="text-text-muted text-xs">{cell.label}</span>
          <p className="font-mono">{cell.value}</p>
        </div>
      ))}
    </div>
  );
}

/** Why there is nothing to draw — never an empty chart standing in for it. */
function Unavailable({ window: w }: { window: EngineMetricsWindow }) {
  return (
    <div
      data-testid="engine-metrics-unavailable"
      className="p-4 rounded-lg border border-dashed border-border text-xs text-text-muted space-y-1"
    >
      <p className="font-medium text-text">Engine metrics unavailable</p>
      <p>{w.detail ?? "The engine published nothing this build could read."}</p>
    </div>
  );
}

export default function EngineMetricsPanel({
  window: w,
  loading = false,
  className = "",
}: EngineMetricsPanelProps) {
  if (!w) {
    return (
      <div className={`text-xs text-text-muted ${className}`}>
        {loading ? "Reading the engine's metrics…" : "No engine metrics yet."}
      </div>
    );
  }

  if (!w.available) {
    return (
      <div className={className}>
        <Unavailable window={w} />
      </div>
    );
  }

  const latest = w.samples[w.samples.length - 1];
  const series = engineSeries(w);
  const resets = w.samples.filter((s) => s.counter_reset).length;

  return (
    <div className={`space-y-3 ${className}`}>
      <div className="flex items-baseline justify-between gap-3">
        <h4 className="text-sm font-semibold">Engine metrics</h4>
        <span className="text-xs text-text-muted">
          {w.samples.length} sample{w.samples.length === 1 ? "" : "s"}, every{" "}
          {w.sample_interval_seconds}s
        </span>
      </div>

      {latest && <LiveGauges latest={latest} />}

      {resets > 0 && (
        <p
          data-testid="engine-metrics-reset"
          className="text-xs text-warning"
        >
          The engine's counters restarted {resets} time
          {resets === 1 ? "" : "s"} in this window. Token rates across those
          intervals are unknown rather than zero, so the lines break there.
        </p>
      )}

      {w.samples.length < MIN_SAMPLES ? (
        <p className="text-xs text-text-muted">
          One sample so far — a second is needed before a rate or a line exists.
        </p>
      ) : (
        <HealthHistoryChart
          title="Recent load"
          caption={VOLATILE_CAPTION}
          series={series}
        />
      )}

      <p className="text-[0.65rem] text-text-muted/70">
        Latency percentiles are not shown: both engines publish histograms and
        no pre-computed percentile, so an honest p95 needs Prometheus querying
        this same endpoint.
      </p>
    </div>
  );
}
