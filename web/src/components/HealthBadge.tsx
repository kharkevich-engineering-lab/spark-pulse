import { HealthStatus, type DeploymentHealth } from "@/lib/operations";

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

// ── Health Alert Component ───────────────────────────────────────────────────

interface HealthAlertProps {
  health: DeploymentHealth;
  className?: string;
}

export function HealthAlert({ health, className = "" }: HealthAlertProps) {
  return (
    <div className={`space-y-2 ${className}`}>
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">{health.deployment_id}</span>
        <HealthBadge status={health.status} size="sm" />
      </div>

      {health.errors.length > 0 && (
        <div className="space-y-1">
          {health.errors.map((err, i) => (
            <p key={i} className="text-xs text-danger">• {err}</p>
          ))}
        </div>
      )}

      {health.warnings.length > 0 && (
        <div className="space-y-1">
          {health.warnings.map((warn, i) => (
            <p key={i} className="text-xs text-warning">• {warn}</p>
          ))}
        </div>
      )}

      <div className="flex gap-4 text-xs text-text-muted">
        {health.restart_count > 0 && (
          <span>Restarts: {health.restart_count}</span>
        )}
        {health.gpu_errors > 0 && (
          <span>GPU Errors: {health.gpu_errors}</span>
        )}
        <span>Last check: {new Date(health.last_check).toLocaleTimeString()}</span>
      </div>
    </div>
  );
}

// ── Health Monitor Controls ──────────────────────────────────────────────────

interface HealthMonitorControlsProps {
  isMonitoring: boolean;
  onToggle: () => void;
  className?: string;
}

export function HealthMonitorControls({
  isMonitoring,
  onToggle,
  className = "",
}: HealthMonitorControlsProps) {
  return (
    <div className={`flex items-center gap-3 ${className}`}>
      <span className="text-sm font-medium">Health Monitoring:</span>
      <button
        onClick={onToggle}
        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
          isMonitoring ? "bg-primary" : "bg-surface-hover"
        }`}
      >
        <span
          className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
            isMonitoring ? "translate-x-6" : "translate-x-1"
          }`}
        />
      </button>
      <span className="text-sm text-text-muted">
        {isMonitoring ? "Active" : "Inactive"}
      </span>
    </div>
  );
}

// ── Health History Chart ─────────────────────────────────────────────────────
//
// There is no stored health history anywhere in this system: `DeploymentHealth`
// is a point-in-time snapshot and the health monitor broadcasts rather than
// records. So this chart draws only what the open page has watched go past on
// the SSE streams it is already subscribed to — the metrics frame every five
// seconds, and the health events. That series lives in the tab and starts over
// on reload, which the caption says out loud rather than implying a history
// that survives.
//
// It never invents a point. Fewer than two real samples is not a chart, and is
// rendered as the sentence "not enough history yet".

export interface HealthSample {
  /** Epoch milliseconds, for ordering and for the elapsed-time caption. */
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

const VIEW_W = 300;
const VIEW_H = 40;
const VIEW_PAD = 3;

/** The polyline for one series, scaled to its own range. */
export function sparklinePath(samples: HealthSample[]): string {
  if (samples.length < MIN_SAMPLES) return "";
  const values = samples.map((s) => s.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  // A flat series has no range to scale against; draw it down the middle
  // rather than dividing by zero or pinning it to the floor.
  const span = max - min;
  const mid = VIEW_H / 2;
  const usable = VIEW_H - VIEW_PAD * 2;
  return samples
    .map((s, i) => {
      const x = (i / (samples.length - 1)) * VIEW_W;
      const y = span === 0 ? mid : VIEW_H - VIEW_PAD - ((s.value - min) / span) * usable;
      return `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
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
        aria-label={`${series.label}, ${series.samples.length} samples over the last ${span}, now ${formatValue(latest, series.unit)}`}
      >
        <path
          d={sparklinePath(series.samples)}
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
