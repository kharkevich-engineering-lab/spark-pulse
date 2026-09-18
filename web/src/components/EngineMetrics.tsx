import { HealthHistoryChart, type HealthSeries } from "@/components/HealthHistoryChart";
import { useI18n, type Translator } from "@/lib/i18n";
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

export function engineSeries(window: EngineMetricsWindow, t: Translator["t"]): HealthSeries[] {
  const s = window.samples;
  const breaks = resetBreaks(s);
  return [
    {
      label: t("engineMetrics.requestsRunning"),
      unit: "",
      color: "var(--color-primary)",
      samples: points(s, (x) => x.running),
    },
    {
      label: t("engineMetrics.queueDepth"),
      unit: "",
      color: "var(--color-warning)",
      samples: points(s, (x) => x.waiting),
    },
    {
      // A fraction from 0 to 1 at the wire; shown as a percentage, which is a
      // change of unit, not of value.
      label: t("engineMetrics.kvUsed"),
      unit: "%",
      color: "var(--color-success)",
      samples: points(s, (x) => x.kv_fraction, 100),
    },
    {
      label: t("engineMetrics.outputTokens"),
      unit: "",
      color: "var(--color-primary)",
      samples: points(s, (x) => x.generation_tokens_per_second),
      breaks,
    },
    {
      label: t("engineMetrics.promptTokens"),
      unit: "",
      color: "var(--color-text-muted)",
      samples: points(s, (x) => x.prompt_tokens_per_second),
      breaks,
    },
  ].filter((series) => series.samples.length > 0);
}

/** The newest value of each gauge — the "is it struggling right now" row. */
function LiveGauges({ latest }: { latest: EngineMetricSample }) {
  const { t } = useI18n();
  const cells: { label: string; value: string; title: string }[] = [
    {
      label: t("engineMetrics.running"),
      value: latest.running === null ? "—" : String(latest.running),
      title: t("engineMetrics.runningHint"),
    },
    {
      label: t("engineMetrics.queued"),
      value: latest.waiting === null ? "—" : String(latest.waiting),
      title: t("engineMetrics.queuedHint"),
    },
    {
      label: t("engineMetrics.kvCache"),
      value:
        latest.kv_fraction === null
          ? "—"
          : `${(latest.kv_fraction * 100).toFixed(1)}%`,
      title: t("engineMetrics.kvCacheHint"),
    },
    {
      label: t("engineMetrics.preemptions"),
      value:
        latest.preemptions_total === null ? "—" : String(latest.preemptions_total),
      title: t("engineMetrics.preemptionsHint"),
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
  const { t } = useI18n();
  return (
    <div
      data-testid="engine-metrics-unavailable"
      className="p-4 rounded-md border border-dashed border-border text-xs text-text-muted space-y-1"
    >
      <p className="font-medium text-text">{t("engineMetrics.unavailable")}</p>
      <p>{w.detail ?? t("engineMetrics.unreadable")}</p>
    </div>
  );
}

export default function EngineMetricsPanel({
  window: w,
  loading = false,
  className = "",
}: EngineMetricsPanelProps) {
  const { t, plural } = useI18n();
  if (!w) {
    return (
      <div className={`text-xs text-text-muted ${className}`}>
        {loading ? t("engineMetrics.reading") : t("engineMetrics.noneYet")}
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
  const series = engineSeries(w, t);
  const resets = w.samples.filter((s) => s.counter_reset).length;

  return (
    <div className={`space-y-3 ${className}`}>
      <div className="flex items-baseline justify-between gap-3">
        <h4 className="text-sm font-semibold">{t("engineMetrics.heading")}</h4>
        <span className="text-xs text-text-muted">
          {plural("engineMetrics.sampleCount", w.samples.length, {
            seconds: w.sample_interval_seconds,
          })}
        </span>
      </div>

      {latest && <LiveGauges latest={latest} />}

      {resets > 0 && (
        <p
          data-testid="engine-metrics-reset"
          className="text-xs text-warning"
        >
          {plural("engineMetrics.counterResets", resets)}
        </p>
      )}

      {w.samples.length < MIN_SAMPLES ? (
        <p className="text-xs text-text-muted">{t("engineMetrics.oneSample")}</p>
      ) : (
        <HealthHistoryChart
          title={t("engineMetrics.recentLoad")}
          caption={t("engineMetrics.volatileCaption")}
          series={series}
        />
      )}

      <p className="text-[13px] text-muted">{t("engineMetrics.noPercentiles")}</p>
    </div>
  );
}
