import { HealthStatus } from "@/lib/operations";

interface HealthBadgeProps {
  status: HealthStatus;
  size?: "sm" | "md" | "lg";
  showLabel?: boolean;
  className?: string;
}

const statusConfig = {
  [HealthStatus.HEALTHY]: {
    color: "var(--color-success)",
    bg: "bg-success/10",
    border: "border-success/30",
    label: "Healthy",
    icon: "●",
  },
  [HealthStatus.DEGRADED]: {
    color: "var(--color-warning)",
    bg: "bg-warning/10",
    border: "border-warning/30",
    label: "Degraded",
    icon: "●",
  },
  [HealthStatus.UNHEALTHY]: {
    color: "var(--color-danger)",
    bg: "bg-danger/10",
    border: "border-danger/30",
    label: "Unhealthy",
    icon: "●",
  },
  [HealthStatus.UNKNOWN]: {
    color: "var(--color-text-muted)",
    bg: "bg-surface-hover",
    border: "border-border",
    label: "Unknown",
    icon: "●",
  },
};

// The badge is derived from the deployment's own status — the container is
// running, or it is not. It is deliberately not the output of a health check:
// nothing in this system runs one, and a badge that implied otherwise would be
// the most misleading pixel on the page.
export default function HealthBadge({
  status,
  size = "md",
  showLabel = true,
  className = "",
}: HealthBadgeProps) {
  const config = statusConfig[status];
  const sizeClasses = {
    sm: "w-2 h-2",
    md: "w-3 h-3",
    lg: "w-4 h-4",
  };

  return (
    <span className={`inline-flex items-center gap-1.5 ${className}`}>
      <span
        className={`rounded-full ${sizeClasses[size]}`}
        style={{ backgroundColor: config.color }}
      />
      {showLabel && (
        <span className={`text-xs font-medium ${
          status === HealthStatus.HEALTHY ? "text-success" :
          status === HealthStatus.DEGRADED ? "text-warning" :
          status === HealthStatus.UNHEALTHY ? "text-danger" :
          "text-text-muted"
        }`}>
          {config.label}
        </span>
      )}
    </span>
  );
}

// ── Health History Chart ─────────────────────────────────────────────────────
//
// Nothing in the browser stores a series. This chart draws only what arrived
// while the page was open — the metrics frame every five seconds, and whatever
// the page hands it from the backend's own bounded window. Either way the
// series is short-lived, and the caption says so rather than implying a history
// that survives.
//
// It never invents a point, and it never invents a line. Fewer than two real
// samples is not a chart, and is rendered as the sentence "not enough history
// yet". The x axis is each sample's own timestamp rather than its position in
// the array, so five seconds of stream and ten minutes of silence are not drawn
// the same width — and where the stream stopped, or where a counter reset made
// a rate unknowable, the line breaks and the missing stretch is shaded instead
// of being bridged by a straight line no measurement supports.

export interface HealthSample {
  /** Epoch milliseconds. This is the x axis, not the array index. */
  t: number;
  value: number;
}

export interface HealthSeries {
  label: string;
  /** Rendered straight after the number: "%", "°C", "" for a count. */
  unit: string;
  /** Any CSS colour; the page passes its theme variables. */
  color: string;
  samples: HealthSample[];
  /**
   * Instants the line must not be drawn *into*, on top of the gaps inferred
   * from the timestamps. A page that knows a measurement is missing for a
   * reason of its own — a counter reset, say, which makes the rate across that
   * interval unknowable rather than zero — declares it here, as the epoch
   * millisecond timestamp of the sample the line may not reach.
   */
  breaks?: number[];
}

interface HealthHistoryChartProps {
  series: HealthSeries[];
  /** Heading above the sparklines. */
  title?: string;
  /** One line saying where the numbers came from and how long they last. */
  caption?: string;
  className?: string;
}

/** Two points is the minimum that can honestly be drawn as a line. */
const MIN_SAMPLES = 2;

/**
 * How many times the usual interval a silence must exceed before it counts as
 * a gap rather than a slow tick. Three is loose enough that one dropped frame
 * is still a line, and tight enough that an `EventSource` reconnect is not.
 */
const GAP_FACTOR = 3;

const VIEW_W = 300;
const VIEW_H = 40;
const VIEW_PAD = 3;

/**
 * The usual interval between samples, taken from the samples themselves.
 *
 * The median rather than the mean, so that one ten-minute silence cannot
 * redefine "usual" and thereby hide itself.
 */
export function medianInterval(samples: HealthSample[]): number {
  const gaps: number[] = [];
  for (let i = 1; i < samples.length; i++) gaps.push(samples[i].t - samples[i - 1].t);
  if (gaps.length === 0) return 0;
  gaps.sort((a, b) => a - b);
  const mid = Math.floor(gaps.length / 2);
  return gaps.length % 2 ? gaps[mid] : (gaps[mid - 1] + gaps[mid]) / 2;
}

/** The indices the line must not be drawn into. */
export function sparklineBreaks(
  samples: HealthSample[],
  breaks: number[] = [],
): number[] {
  if (samples.length < MIN_SAMPLES) return [];
  const usual = medianInterval(samples);
  const declared = new Set(breaks);
  const at: number[] = [];
  for (let i = 1; i < samples.length; i++) {
    const silent = usual > 0 && samples[i].t - samples[i - 1].t > usual * GAP_FACTOR;
    if (silent || declared.has(samples[i].t)) at.push(i);
  }
  return at;
}

/** Map a timestamp onto the view box. */
function xFor(t: number, first: number, spanMs: number): number {
  // Every sample at the same instant: there is no axis to spread them along,
  // and spreading them anyway would invent the very thing this exists to fix.
  return spanMs === 0 ? 0 : ((t - first) / spanMs) * VIEW_W;
}

/**
 * The polyline for one series, spaced by time and broken at every gap.
 *
 * Each break starts a new subpath with `M`, so nothing is drawn across it, and
 * the x of every point is its own timestamp, so the width of the hole is the
 * length of the silence.
 */
export function sparklinePath(samples: HealthSample[], breaks: number[] = []): string {
  if (samples.length < MIN_SAMPLES) return "";
  const values = samples.map((s) => s.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  // A flat series has no range to scale against; draw it down the middle
  // rather than dividing by zero or pinning it to the floor.
  const span = max - min;
  const mid = VIEW_H / 2;
  const usable = VIEW_H - VIEW_PAD * 2;
  const first = samples[0].t;
  const spanMs = samples[samples.length - 1].t - first;
  const broken = new Set(sparklineBreaks(samples, breaks));
  return samples
    .map((s, i) => {
      const x = xFor(s.t, first, spanMs);
      const y = span === 0 ? mid : VIEW_H - VIEW_PAD - ((s.value - min) / span) * usable;
      const move = i === 0 || broken.has(i);
      return `${move ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}

/** The x extent of each gap, so it can be shaded rather than merely absent. */
export function sparklineGapBands(
  samples: HealthSample[],
  breaks: number[] = [],
): { x: number; width: number }[] {
  if (samples.length < MIN_SAMPLES) return [];
  const first = samples[0].t;
  const spanMs = samples[samples.length - 1].t - first;
  return sparklineBreaks(samples, breaks).map((i) => {
    const x = xFor(samples[i - 1].t, first, spanMs);
    // A declared break between two adjacent samples is only one interval wide;
    // give it a floor, or the reader cannot see that the line was cut.
    const width = Math.max(xFor(samples[i].t, first, spanMs) - x, 2);
    return { x, width };
  });
}

/** "45 s" / "12 min" / "2 h" — the window the samples actually cover. */
function formatSpan(ms: number): string {
  if (ms < 60_000) return `${Math.max(1, Math.round(ms / 1000))} s`;
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)} min`;
  return `${Math.round(ms / 360_000) / 10} h`;
}

function formatValue(value: number, unit: string): string {
  const rounded = Math.round(value * 10) / 10;
  return `${rounded}${unit}`;
}

function Sparkline({ series }: { series: HealthSeries }) {
  const values = series.samples.map((s) => s.value);
  const latest = values[values.length - 1];
  const low = Math.min(...values);
  const peak = Math.max(...values);
  const spanMs = series.samples[series.samples.length - 1].t - series.samples[0].t;
  const span = formatSpan(spanMs);
  const bands = sparklineGapBands(series.samples, series.breaks);
  const gapNote =
    bands.length === 0
      ? ""
      : `, ${bands.length} gap${bands.length === 1 ? "" : "s"} where nothing was measured`;

  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-3 text-xs">
        <span className="font-medium">{series.label}</span>
        <span className="text-text-muted font-mono">
          now {formatValue(latest, series.unit)} · low {formatValue(low, series.unit)} · peak{" "}
          {formatValue(peak, series.unit)}
        </span>
      </div>
      <svg
        viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
        preserveAspectRatio="none"
        className="w-full h-10"
        role="img"
        aria-label={`${series.label}, ${series.samples.length} samples over the last ${span}, now ${formatValue(latest, series.unit)}${gapNote}`}
      >
        {bands.map((band, i) => (
          <rect
            key={i}
            data-testid="sparkline-gap"
            x={band.x}
            y={0}
            width={band.width}
            height={VIEW_H}
            fill="var(--color-text-muted)"
            opacity="0.12"
          />
        ))}
        <path
          d={sparklinePath(series.samples, series.breaks)}
          fill="none"
          stroke={series.color}
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <p className="text-[0.65rem] text-text-muted/70">
        {series.samples.length} samples over the last {span}
        {bands.length > 0 &&
          ` · ${bands.length} gap${bands.length === 1 ? "" : "s"} with no measurement`}
      </p>
    </div>
  );
}

export function HealthHistoryChart({
  series,
  title = "Health history",
  caption,
  className = "",
}: HealthHistoryChartProps) {
  const drawable = series.filter((s) => s.samples.length >= MIN_SAMPLES);
  const collected = series.reduce((most, s) => Math.max(most, s.samples.length), 0);

  if (drawable.length === 0) {
    return (
      <div
        className={`p-6 rounded-lg border border-dashed border-border flex flex-col items-center justify-center text-center ${className}`}
      >
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="text-text-muted mb-2">
          <path d="M3 3v18h18" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M7 16l4-8 4 4 4-6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <span className="text-sm text-text-muted">Not enough history yet</span>
        <span className="text-xs text-text-muted/70 mt-1">
          {collected === 0
            ? "Nothing has arrived on the stream since this page was opened."
            : `${collected} sample${collected === 1 ? "" : "s"} so far — two are needed to draw a line.`}
        </span>
      </div>
    );
  }

  return (
    <div className={`rounded-lg border border-border p-4 space-y-4 ${className}`}>
      <div>
        <h4 className="text-sm font-semibold">{title}</h4>
        {caption && <p className="text-xs text-text-muted mt-0.5">{caption}</p>}
      </div>
      {drawable.map((s) => (
        <Sparkline key={s.label} series={s} />
      ))}
    </div>
  );
}
