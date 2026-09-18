/** One run, as a row.
 *
 * The row it replaces fitted ten controls onto one line: a name, a recipe id,
 * an engine pill, a node count, a port, a status badge, a PID, a creation
 * timestamp and three unlabelled icon buttons. At 1280px it just fitted; below
 * that the timestamp squeezed the name to three characters and the buttons
 * slid off the right edge, and at no width did it answer the question an
 * operator actually arrives with — is this thing fast, and how long has it
 * been up.
 *
 * So: two lines and a right-hand column. Line one is the name and the *one*
 * status badge. Line two is what the run is — engine, model, where its ranks
 * landed, the port. The right-hand column is how it is doing: the numbers from
 * its latest benchmark, how long it has been serving, and the actions, which
 * are labelled buttons rather than icons because "square" and "flame" are not
 * words. Under 900px the column drops below the two lines and the actions
 * become full-width thirds, which is the only shape that gives a thumb a
 * 44px target at 390.
 */

import { useT } from "@/lib/i18n";
import { StatusBadge, Button, isSettling } from "@/ui";
import { ExperimentalBadge } from "@/components/Experimental";
import { MULTI_NODE_BADGE_TITLE } from "@/lib/experimental";
import { formatDuration } from "@/lib/utils";
import { cn } from "@/lib/utils";
import { isLiveRun } from "@/hooks/useDeployments";
import { ChevronDown, ChevronUp, Flame, Square, Trash2, X } from "lucide-react";
import type { BenchmarkResult, Deployment } from "@/lib/types";

/** Seconds between two timestamps, floored at zero. */
function secondsBetween(from: string, to: number | string): number {
  const start = new Date(from).getTime();
  const end = typeof to === "number" ? to : new Date(to).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end)) return 0;
  return Math.max(0, Math.floor((end - start) / 1000));
}

/** A number out of a benchmark's result bag, when it is one. */
function metric(result: BenchmarkResult | undefined, ...keys: string[]): number | null {
  const results = result?.results as Record<string, unknown> | null | undefined;
  if (!results) return null;
  for (const key of keys) {
    const value = results[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

/** Where this run's ranks are, named rather than counted.
 *
 * `tp N · node-a + node-b` when the shape is worth stating, the node's own
 * name when it is one machine. A record that names no node is on the machine
 * the control plane runs on, which is still a node — it just has not told us
 * which one. */
export function placement(run: Deployment, thisNode: string): string {
  const nodes = run.nodes?.length ? run.nodes.join(" + ") : thisNode;
  const tp = Number(run.params?.tensor_parallel);
  return Number.isFinite(tp) && tp > 1 ? `tp ${tp} · ${nodes}` : nodes;
}

export interface RunRowProps {
  run: Deployment;
  /** The most recent benchmark for this run, if one was ever taken. */
  benchmark?: BenchmarkResult;
  expanded: boolean;
  onToggle: () => void;
  /** Stop, cancel or remove — the row decides which word, the page confirms. */
  onTeardown: () => void;
  onBenchmark: () => void;
  /** The expanded panel: logs, ranks, the engine's metrics. */
  children?: React.ReactNode;
}

/** Grid columns for the phone layout, one per action. Static classes, because
 *  Tailwind reads them out of the source rather than out of a template. */
const ACTION_COLUMNS: Record<number, string> = {
  2: "grid-cols-2",
  3: "grid-cols-3",
};

function Chip({ children, title }: { children: React.ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="inline-flex max-w-full items-center gap-1 truncate rounded-full border border-line px-2 py-0.5 text-[13px] text-muted"
    >
      {children}
    </span>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-[900px]:text-right">
      <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">{label}</p>
      <p className="font-mono text-[14px] tabular-nums text-text">{value}</p>
    </div>
  );
}

export default function RunRow({
  run,
  benchmark,
  expanded,
  onToggle,
  onTeardown,
  onBenchmark,
  children,
}: RunRowProps) {
  const t = useT();
  const live = isLiveRun(run);
  const settling = isSettling(run.sync);
  const multiNode = (run.node_count ?? 1) > 1;

  const throughput = metric(benchmark, "throughput");
  // No engine publishes a time-to-first-token of its own, so the first-token
  // figure is the benchmark's own latency — named for what it measures rather
  // than for what would look better in the column.
  const latency = metric(benchmark, "ttft_ms", "latency_ms");

  // Live: how long it has been serving. Finished: when it stopped, and how
  // long it lasted — a run that ran for four seconds and one that ran for four
  // hours are different events, and a bare timestamp says neither.
  const started = run.started_at || run.created_at;
  const timing = live
    ? t("runs.uptime", { duration: formatDuration(secondsBetween(started, Date.now())) })
    : run.stopped_at
      ? `${new Date(run.stopped_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} · ${formatDuration(secondsBetween(started, run.stopped_at))}`
      : "—";

  const teardownLabel = !live
    ? t("runs.remove")
    : run.status === "pending"
      ? t("runs.cancel")
      : t("runs.stop");

  const actionCount = live ? 3 : 2;

  return (
    <div
      data-testid={`deployment-${run.id}`}
      className="overflow-hidden rounded-md border border-line bg-surface"
    >
      <div className="flex flex-col gap-4 p-4 min-[900px]:flex-row min-[900px]:items-center min-[900px]:gap-6">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <button
              type="button"
              onClick={onToggle}
              aria-expanded={expanded}
              className="max-w-full truncate text-left text-[17px] font-semibold tracking-[-0.02em] hover:text-blue2"
            >
              {run.name}
            </button>
            <StatusBadge status={run.status} sync={run.sync} syncReason={run.sync_reason} />
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {run.engine && (
              <Chip title={run.image_ref || undefined}>
                {run.engine}
                {run.variant ? `/${run.variant}` : ""}
              </Chip>
            )}
            {run.model && <Chip title={run.model}>{run.model}</Chip>}
            <span className="inline-flex max-w-full items-center gap-1.5">
              <Chip>{placement(run, t("runs.thisNode"))}</Chip>
              {multiNode && <ExperimentalBadge title={MULTI_NODE_BADGE_TITLE} />}
            </span>
            {run.port && (
              <span className="font-mono text-[13px] text-muted">:{run.port}</span>
            )}
          </div>
        </div>

        <div className="flex shrink-0 flex-col gap-3 min-[900px]:items-end">
          {/* Three figures across 318px of phone is tight and the ended stamp
              is the widest of them, so the row wraps rather than overflowing. */}
          <div className="flex flex-wrap items-start gap-x-5 gap-y-2 min-[900px]:flex-nowrap min-[900px]:gap-x-6">
            <Figure
              label={t("runs.throughput")}
              value={throughput != null ? `${throughput.toFixed(1)} tok/s` : "—"}
            />
            <Figure
              label={t("runs.firstToken")}
              value={latency != null ? `${latency.toFixed(1)} ms` : "—"}
            />
            <Figure label={live ? t("runs.serving") : t("runs.ended")} value={timing} />
          </div>
          <div className={cn("grid gap-2 min-[900px]:flex", ACTION_COLUMNS[actionCount])}>
            <Button
              size="sm"
              icon={expanded ? ChevronUp : ChevronDown}
              aria-expanded={expanded}
              onClick={onToggle}
            >
              {t("runs.logs")}
            </Button>
            {live && (
              <Button size="sm" icon={Flame} onClick={onBenchmark} disabled={run.status !== "running"}>
                {t("runs.benchmark")}
              </Button>
            )}
            <Button
              size="sm"
              variant="danger"
              icon={!live ? Trash2 : run.status === "pending" ? X : Square}
              disabled={settling}
              title={settling ? t("runs.settling") : teardownLabel}
              onClick={onTeardown}
            >
              {teardownLabel}
            </Button>
          </div>
        </div>
      </div>
      {expanded && <div className="border-t border-line">{children}</div>}
    </div>
  );
}
