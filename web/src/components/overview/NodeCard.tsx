/** One node on the overview: whether it answers, and who holds its memory.
 *
 * The memory bar is the point. Deciding whether a recipe fits beside what is
 * already running used to mean opening Monitoring, reading two blocks of big
 * numbers and doing the sum; here the runs are coloured slices of one bar,
 * the same colour on every node a run spans, and the free figure is printed.
 */

import { Link } from "react-router-dom";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { NodeState, type NodeCondition } from "@/ui";
import { formatMb, type MemoryLayout } from "@/lib/memoryLayout";
import type { ClusterNode, NodeStats } from "@/lib/types";

const CONDITION: Record<string, NodeCondition> = { healthy: "ok", unknown: "unknown", dead: "bad" };

/** The condition the card leads with: unreachable beats the registry's word,
 *  and an outdated agent is a warning on an otherwise healthy node. */
export function nodeCondition(stats: NodeStats, node?: ClusterNode): NodeCondition {
  if (!stats.reachable) return "bad";
  if (node?.agent?.connected && node.agent.current === false) return "warn";
  return node ? CONDITION[node.state] ?? "unknown" : "ok";
}

export function MemoryBar({ layout, className }: { layout: MemoryLayout; className?: string }) {
  const t = useT();
  return (
    <div
      role="img"
      aria-label={t("overview.memoryAria", { free: formatMb(layout.freeMb), total: formatMb(layout.totalMb) })}
      className={cn("flex h-2.5 w-full overflow-hidden rounded-full bg-bg2", className)}
    >
      {layout.segments.map((s, i) => (
        <span
          key={`${s.kind}-${s.id}-${i}`}
          title={`${segmentLabel(s, t)} · ${formatMb(s.mb)}`}
          className="h-full border-r border-bg last:border-r-0"
          style={{ width: `${(s.mb / layout.totalMb) * 100}%`, background: s.color }}
        />
      ))}
    </div>
  );
}

function segmentLabel(s: MemoryLayout["segments"][number], t: ReturnType<typeof useT>): string {
  if (s.kind === "system") return t("overview.memSystem");
  if (s.kind === "other") return t("overview.memOther");
  return s.label;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <p className="text-[10.5px] font-semibold uppercase tracking-[0.1em] text-muted">{label}</p>
      <p className="font-mono text-[15px] tabular-nums">{value}</p>
    </div>
  );
}

export interface NodeCardProps {
  stats: NodeStats;
  node?: ClusterNode;
  layout: MemoryLayout | null;
}

export default function NodeCard({ stats, node, layout }: NodeCardProps) {
  const t = useT();
  const gpu = stats.gpu?.[0];
  const condition = nodeCondition(stats, node);
  const outdated = condition === "warn";
  const dash = "—";

  return (
    <article
      data-testid={`node-card-${stats.id}`}
      className="flex min-w-0 flex-col gap-3 rounded-md border border-line bg-surface p-4"
    >
      <header className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <Link to="/monitoring" className="truncate font-semibold text-text hover:text-blue2">
            {stats.name}
          </Link>
          {stats.is_control_plane && (
            <span className="rounded-full border border-line px-2 text-[11px] text-muted">{t("nodes.controlPlane")}</span>
          )}
        </div>
        <NodeState
          state={condition}
          label={outdated ? t("overview.agentOutdated") : undefined}
          title={stats.error || undefined}
        />
      </header>

      {layout ? (
        <div className="space-y-1.5">
          <MemoryBar layout={layout} />
          <p className="text-[12px] text-muted">
            <span className="font-mono text-text tabular-nums">{formatMb(layout.freeMb)}</span>{" "}
            {t("overview.freeOf", { total: formatMb(layout.totalMb) })}
            {layout.segments
              .filter((s) => s.kind === "run")
              .map((s) => (
                <span key={s.id} className="ml-2 inline-flex items-center gap-1 whitespace-nowrap">
                  <span aria-hidden="true" className="h-2 w-2 rounded-sm" style={{ background: s.color }} />
                  {s.label} <span className="font-mono tabular-nums">{formatMb(s.mb)}</span>
                </span>
              ))}
          </p>
        </div>
      ) : (
        <p className="text-[12px] text-muted">{stats.error || t("overview.memoryUnknown")}</p>
      )}

      <div className="grid grid-cols-3 gap-2">
        <Stat label={t("overview.gpu")} value={gpu?.utilization != null ? `${gpu.utilization}%` : dash} />
        <Stat label={t("overview.temp")} value={gpu?.temperature != null ? `${gpu.temperature}°` : dash} />
        <Stat label={t("overview.power")} value={gpu?.power_draw != null ? `${Math.round(gpu.power_draw)} W` : dash} />
      </div>
    </article>
  );
}
