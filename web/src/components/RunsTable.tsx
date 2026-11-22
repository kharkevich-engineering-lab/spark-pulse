/** Runs as a console lists them: one table, full width.
 *
 * On a wide screen a run is a row — name, phase, engine, model, nodes, port,
 * throughput, uptime — so twenty runs fit where three cards did. A run opens
 * on a page of its own (`/jobs/:id`): a panel beside the list was a second
 * scrolling column inside a scrolling page. Under 1200px Runs keeps
 * `RunRow`'s cards, which link to the same page.
 */

import { Link, useNavigate } from "react-router-dom";
import { useT } from "@/lib/i18n";
import { cn, formatDuration } from "@/lib/utils";
import { IconButton, StatusBadge, isSettling } from "@/ui";
import { isLiveRun } from "@/hooks/useDeployments";
import { Square, Trash2, X } from "lucide-react";
import ServesChip from "./ServesChip";
import { metric, placement, secondsBetween } from "./RunRow";
import type { BenchmarkResult, Deployment } from "@/lib/types";
import type { PullProgress } from "@/lib/operations";

type T = ReturnType<typeof useT>;

/** Live: how long it has served. Finished: when it ended and how long it ran. */
export function runTiming(run: Deployment, t: T, now = Date.now()): string {
  const started = run.started_at || run.created_at;
  if (isLiveRun(run)) return formatDuration(secondsBetween(started, now));
  if (!run.stopped_at) return "—";
  const at = new Date(run.stopped_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return t("runs.endedAt", { at, duration: formatDuration(secondsBetween(started, run.stopped_at)) });
}

/** Which of the three words a teardown uses. A live deployment is stopped, one
 *  that never started is cancelled, and a finished one is a record being
 *  cleared — three different consequences, three dialogs. */
export const teardownKind = (run: Deployment): "stop" | "cancel" | "remove" =>
  !isLiveRun(run) ? "remove" : run.status === "pending" ? "cancel" : "stop";

export function teardownLabel(run: Deployment, t: T): string {
  if (!isLiveRun(run)) return t("runs.remove");
  return run.status === "pending" ? t("runs.cancel") : t("runs.stop");
}

export function teardownIcon(run: Deployment) {
  if (!isLiveRun(run)) return Trash2;
  return run.status === "pending" ? X : Square;
}

export function throughputOf(benchmark: BenchmarkResult | undefined, t: T): string {
  if (benchmark?.status === "running") return t("runs.benchmarkMeasuring");
  const value = metric(benchmark, "throughput");
  return value != null ? `${value.toFixed(1)} tok/s` : "—";
}

/** Where a run's own page is. */
export const runPath = (id: string) => `/jobs/${encodeURIComponent(id)}`;

export interface RunsTableProps {
  runs: Deployment[];
  onTeardown: (run: Deployment) => void;
  benchmarks: Map<string, BenchmarkResult>;
  pulls: Map<string, PullProgress>;
}

export function RunsTable({ runs, onTeardown, benchmarks, pulls }: RunsTableProps) {
  const t = useT();
  const navigate = useNavigate();
  const head = "px-3 py-2 text-left text-[10.5px] font-semibold uppercase tracking-[0.08em] text-muted";
  return (
    <div className="overflow-x-auto rounded-md border border-line bg-surface">
      <table className="w-full border-collapse text-[13px]" data-testid="runs-table">
        <thead>
          <tr className="border-b border-line">
            <th className={head}>{t("overview.colRun")}</th>
            <th className={head}>{t("overview.colPhase")}</th>
            <th className={head}>{t("overview.colEngine")}</th>
            <th className={head}>{t("runs.model")}</th>
            <th className={head}>{t("overview.colNodes")}</th>
            <th className={head}>{t("overview.colPort")}</th>
            <th className={cn(head, "text-right")}>{t("runs.throughput")}</th>
            <th className={cn(head, "text-right")}>{t("overview.colUp")}</th>
            <th className={head}>
              <span className="sr-only">{t("runs.actions")}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => {
            const pull = run.status === "pulling" ? pulls.get(run.id) : undefined;
            const label = teardownLabel(run, t);
            return (
              <tr
                key={run.id}
                data-testid={`deployment-${run.id}`}
                onClick={() => navigate(runPath(run.id))}
                className="cursor-pointer border-b border-line last:border-b-0 transition-colors hover:bg-[color-mix(in_srgb,var(--text)_4%,transparent)]"
              >
                <td className="max-w-[260px] px-3 py-2">
                  <Link
                    to={runPath(run.id)}
                    onClick={(e) => e.stopPropagation()}
                    className="block max-w-full truncate font-semibold text-text hover:text-blue2"
                  >
                    {run.name}
                  </Link>
                </td>
                <td className="whitespace-nowrap px-3 py-2">
                  <StatusBadge
                    status={run.status}
                    sync={run.sync}
                    syncReason={run.sync_reason}
                    syncIntent={run.sync_intent}
                    phase={run.phase}
                    phaseVerified={run.phase_verified}
                    phaseReason={run.phase_reason}
                    progress={pull?.percent}
                  />
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-muted">
                  {run.engine ? `${run.engine}${run.variant ? `/${run.variant}` : ""}` : "—"}
                </td>
                <td className="max-w-[240px] truncate px-3 py-2 text-muted" title={run.model || undefined}>
                  {run.model || "—"} <ServesChip serves={run.serves} />
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-muted">{placement(run, t("runs.thisNode"))}</td>
                <td className="whitespace-nowrap px-3 py-2 font-mono tabular-nums">{run.port ? `:${run.port}` : "—"}</td>
                <td className="whitespace-nowrap px-3 py-2 text-right font-mono tabular-nums">
                  {throughputOf(benchmarks.get(run.id), t)}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right font-mono tabular-nums text-muted">
                  {runTiming(run, t)}
                </td>
                <td className="whitespace-nowrap px-2 py-1 text-right" onClick={(e) => e.stopPropagation()}>
                  <IconButton
                    size="sm"
                    variant="ghost"
                    icon={teardownIcon(run)}
                    label={t("runs.teardownFor", { action: label, name: run.name })}
                    disabled={isSettling(run.sync, run.phase)}
                    onClick={() => onTeardown(run)}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
