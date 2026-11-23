/** What one node is doing now, in the pieces its page lays out: a strip of
 *  readings, the GPU history since the page opened, the processes holding the
 *  GPU, and its disks.
 *
 * This was the Monitoring page, then a panel beside the node list, then five
 * tabs — each of which left most of a wide screen empty. It is one page now:
 * the readings are a strip across the top, and everything else sits in rows
 * that share the width.
 */

import { useState } from "react";
import { OctagonX } from "lucide-react";
import { translate, useI18n, type Language } from "@/lib/i18n";
import { killGpuProcess } from "@/lib/api";
import { cn, formatSize } from "@/lib/utils";
import { formatMb, type MemoryLayout } from "@/lib/memoryLayout";
import { AlertModal, ConfirmModal, IconButton, NodeState, Section, Spinner, Table, THead, Th, Tr, Td } from "@/ui";
import { HealthHistoryChart, type HealthSeries } from "@/components/HealthHistoryChart";
import { MemoryBar } from "@/components/overview/NodeCard";
import { seriesKey } from "@/hooks/useGpuHistory";
import type { ClusterNode, GPUProcess, NodeStats } from "@/lib/types";

function tempTone(c: number | null | undefined): string | undefined {
  if (c == null) return undefined;
  return c > 80 ? "text-bad" : c > 65 ? "text-warn" : undefined;
}

/** A thin usage bar in the token colours: good below 70, warn to 90, bad above. */
export function UsageBar({ percent, className }: { percent: number; className?: string }) {
  const pct = Math.max(0, Math.min(100, percent));
  const tone = pct > 90 ? "bg-bad" : pct > 70 ? "bg-warn" : "bg-good";
  return (
    <div className={cn("h-1.5 overflow-hidden rounded-full bg-bg2", className)}>
      <div className={cn("h-full rounded-full transition-[width] duration-500", tone)} style={{ width: `${pct}%` }} />
    </div>
  );
}

function Labelled({ label, value, percent }: { label: React.ReactNode; value: string; percent: number }) {
  return (
    <div className="space-y-1">
      <div className="flex justify-between gap-3 text-[12.5px]">
        <span className="min-w-0 truncate text-muted">{label}</span>
        <span className="font-mono">{value}</span>
      </div>
      <UsageBar percent={percent} />
    </div>
  );
}

/** Unknown, not idle: a node that did not answer says why, in place of the
 *  readings it could not give. */
export function NodeUnreachable({ stats }: { stats: NodeStats }) {
  const { t } = useI18n();
  return (
    <p className="text-[13px] text-muted" data-testid={`node-live-unreachable-${stats.id}`}>
      {stats.error || t("monitoring.unreachable")}
    </p>
  );
}

/** One cell of the strip: a small label over a reading. */
function Cell({ label, children, wide, testId }: { label: string; children: React.ReactNode; wide?: boolean; testId?: string }) {
  return (
    <div
      data-testid={testId}
      className={cn(
        "min-w-0 border-l border-t border-line px-4 py-3",
        wide ? "flex-[2_1_280px]" : "flex-[1_1_120px]",
      )}
    >
      <p className="mb-1 text-[10.5px] font-semibold uppercase tracking-[0.08em] text-muted">{label}</p>
      {children}
    </div>
  );
}

const READING = "font-mono text-[15px] tabular-nums";

/** Several GPUs read as one value each, in order: "12% · 30%". */
function perGpu(stats: NodeStats, pick: (g: NodeStats["gpu"][number]) => string | null): string {
  const values = stats.gpu.map(pick).filter((v): v is string => v !== null);
  return values.length > 0 ? values.join(" · ") : "—";
}

/** Everything the node reports right now, as one strip across the page.
 *
 * Cells share the width and wrap on a narrow screen; the hairlines between
 * them are each cell's own left and top borders, pulled one pixel out of the
 * frame, so a wrapped row never leaves a hole. A node that did not answer
 * keeps the strip — what the registry knows still holds — and says why in
 * place of its readings. */
export function NodeStatStrip({
  node,
  interfaces,
  stats,
  layout,
  loading,
}: {
  node: ClusterNode;
  /** The node's interfaces, as the registry summarises them. */
  interfaces: string;
  stats?: NodeStats;
  layout: MemoryLayout | null;
  loading: boolean;
}) {
  const { t } = useI18n();
  const runtime = node.agent?.runtime;
  const gpus = stats?.reachable ? stats.gpu : [];
  const gpuMemory = gpus.filter((g) => g.memory_supported);
  // A card with memory of its own does not draw from the host's pool, so the
  // shape of that pool says nothing about where its weights land.
  const shape = stats?.reachable && gpuMemory.length === 0 ? stats.memory_shape : null;
  const hottest = gpus.reduce<number | null>((max, g) => (g.temperature == null ? max : Math.max(max ?? g.temperature, g.temperature)), null);

  return (
    <div className="overflow-hidden rounded-md border border-line bg-surface" data-testid={`node-strip-${node.id}`}>
      <div className="-ml-px -mt-px flex flex-wrap">
        <Cell label={t("nodesPage.memory")} wide>
          {!stats ? (
            loading ? <Spinner size="sm" label={t("common.loading")} /> : <p className="text-[13px] text-muted">{t("monitoring.unreachable")}</p>
          ) : !stats.reachable ? (
            <NodeUnreachable stats={stats} />
          ) : layout ? (
            <div className="space-y-1.5">
              <p className="text-[12.5px] text-muted">
                <span className={cn(READING, "text-text")}>{formatMb(layout.usedMb)}</span>{" "}
                {t("nodesPage.usedOf", { total: formatMb(layout.totalMb) })}
              </p>
              <MemoryBar layout={layout} />
              <ul className="flex flex-wrap gap-x-3 gap-y-0.5 text-[12px] text-muted">
                {layout.segments
                  .filter((seg) => seg.kind === "run")
                  .map((seg) => (
                    <li key={seg.id} className="inline-flex items-center gap-1.5">
                      <span className="h-2 w-2 rounded-sm" style={{ background: seg.color }} />
                      {seg.label} <span className="font-mono">{formatMb(seg.mb)}</span>
                    </li>
                  ))}
              </ul>
              {stats.unavailable.length > 0 && (
                <ul className="space-y-0.5 text-[12px] text-warn">
                  {stats.unavailable.map((what) => (
                    <li key={what}>{t("monitoring.couldNotRead", { what })}</li>
                  ))}
                </ul>
              )}
            </div>
          ) : (
            <p className="text-[13px] text-muted">{t("overview.memoryUnknown")}</p>
          )}
        </Cell>
        {gpus.length > 0 && stats && (
          <>
            <Cell label={t("monitoring.gpuUtilization")} testId={`gpu-${stats.id}-${gpus[0].gpu}`}>
              <p className={READING}>{perGpu(stats, (g) => (g.utilization != null ? `${g.utilization}%` : null))}</p>
              <p className="truncate text-[12px] text-muted">{gpus.map((g) => g.name || g.gpu).join(" · ")}</p>
            </Cell>
            <Cell label={t("monitoring.temperature")}>
              <p className={cn(READING, tempTone(hottest))}>{perGpu(stats, (g) => (g.temperature != null ? `${g.temperature}°C` : null))}</p>
            </Cell>
            <Cell label={t("nodesPage.power")}>
              <p className={READING}>
                {perGpu(stats, (g) =>
                  g.power_draw != null
                    ? `${Math.round(g.power_draw)}${g.power_limit != null ? ` / ${Math.round(g.power_limit)}` : ""} W`
                    : null,
                )}
              </p>
            </Cell>
            {gpuMemory.length > 0 && (
              <Cell label={t("monitoring.memory")}>
                {gpuMemory.map((g) => (
                  <Labelled
                    key={g.uuid || g.gpu}
                    label=""
                    value={`${formatMb(g.memory_used)} / ${formatMb(g.memory_total)}`}
                    percent={g.memory_total > 0 ? (g.memory_used / g.memory_total) * 100 : 0}
                  />
                ))}
              </Cell>
            )}
          </>
        )}
        {shape && (
          <Cell label={t("nodesPage.memoryShape")} testId={`memory-shape-${node.id}`}>
            <p className={cn(READING, shape.fragmented && "text-warn")}>{`${Math.round(shape.contiguous_percent)}%`}</p>
            <p className="text-[12px] text-muted">
              {t("nodesPage.memoryShapeDetail", {
                contiguous: formatSize(shape.contiguous_bytes),
                free: formatSize(shape.free_bytes),
                largest: formatSize(shape.largest_block_bytes),
              })}
            </p>
            {shape.fragmented && <p className="text-[12px] text-warn">{t("nodesPage.memoryShapeFragmented")}</p>}
          </Cell>
        )}
        {stats?.reachable && gpus.length === 0 && (
          <Cell label={t("monitoring.gpuUtilization")}>
            <p className="text-[13px] text-muted">{t("nodesPage.noGpu")}</p>
          </Cell>
        )}
        <Cell label={t("nodesPage.agent")}>
          <p className={cn("text-[13px]", node.agent?.current === false && "text-warn")}>
            {node.agent?.version
              ? `${t("nodes.agentVersion", { version: node.agent.version })} · ${
                  node.agent.current === false ? t("nodes.agentStale") : t("fleet.agentCurrent")
                }`
              : t("nodes.noAgent")}
          </p>
          {runtime?.name && (
            <p className="truncate text-[12px] text-muted" data-testid={`node-runtime-${node.id}`}>
              {t("nodes.runtime", { name: runtime.name, version: runtime.version })}
              {runtime.rootless && ` · ${t("nodes.rootless")}`}
            </p>
          )}
        </Cell>
        <Cell label={t("nodesPage.interfaces")}>
          <p className="break-words font-mono text-[13px]">{interfaces}</p>
        </Cell>
      </div>
    </div>
  );
}

/** One section per GPU and reading: the history since the page opened, side
 *  by side so a chart is never a half-empty row. */
export function NodeCharts({ stats, history }: { stats: NodeStats; history: Record<string, HealthSeries[]> }) {
  if (!stats.reachable || stats.gpu.length === 0) return null;
  return (
    <>
      {stats.gpu.map((gpu) => {
        const series = history[seriesKey(stats.id, gpu)] ?? [];
        return (
          <div key={gpu.uuid || gpu.gpu} className="grid gap-3 min-[900px]:grid-cols-2">
            {series.map((one) => (
              <Section key={one.label} title={one.label} meta={stats.gpu.length > 1 ? gpu.name || gpu.gpu : undefined}>
                <HealthHistoryChart bare tall series={[one]} />
              </Section>
            ))}
          </div>
        );
      })}
    </>
  );
}

/** Name what dies, in the terms the row already showed. */
function killMessage(p: GPUProcess, node: NodeStats, language: Language): string {
  const held = translate(language, "monitoring.killHeld", { mb: p.used_memory });
  const provenance = [
    translate(language, p.is_tracked ? "monitoring.killTracked" : "monitoring.killUntracked"),
    translate(language, "monitoring.killOnNode", {
      node: node.name || node.address || translate(language, "monitoring.controlPlane"),
    }),
  ].join(" ");
  return translate(language, "monitoring.killBody", { pid: p.pid, name: p.process_name, held, provenance });
}

/** The processes holding the GPU, each with what started it and a kill. */
export function NodeProcesses({ stats, onChanged }: { stats: NodeStats; onChanged: () => void }) {
  const { t, language } = useI18n();
  const [pendingKill, setPendingKill] = useState<GPUProcess | null>(null);
  const [killing, setKilling] = useState<number | null>(null);
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);

  async function kill(proc: GPUProcess) {
    setPendingKill(null);
    setKilling(proc.pid);
    try {
      const result = await killGpuProcess(proc.pid, stats.address);
      // A refusal and a success are the same HTTP 200; the flag tells them apart.
      if (!result?.killed) {
        setAlert({
          title: t("fleet.killFailed", { pid: proc.pid }),
          message: result?.error ?? t("fleet.killNoReason", { name: proc.process_name, pid: proc.pid }),
        });
      }
      onChanged();
    } catch (e) {
      setAlert({ title: t("fleet.killError", { pid: proc.pid }), message: e instanceof Error ? e.message : String(e) });
    } finally {
      setKilling(null);
    }
  }

  if (!stats.reachable) return null;
  return (
    <Section title={t("monitoring.gpuProcesses")} meta={String(stats.processes.length)} flush>
      {stats.processes.length === 0 ? (
        <p className="px-4 py-3 text-[12.5px] text-muted">{t("nodesPage.noProcesses")}</p>
      ) : (
        <Table>
          <THead>
            <Th>{t("nodesPage.colPid")}</Th>
            <Th>{t("nodesPage.colProcess")}</Th>
            <Th numeric>{t("nodesPage.colMemory")}</Th>
            <Th srOnly>{t("runs.actions")}</Th>
          </THead>
          <tbody>
            {stats.processes.map((p) => (
              <Tr key={p.pid} data-testid={`gpu-process-${stats.id}-${p.pid}`}>
                <Td mono muted>
                  {p.pid}
                </Td>
                <Td>
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                    <span>{p.process_name}</span>
                    {p.is_tracked ? (
                      <NodeState
                        state="ok"
                        label={p.deployment ? t("monitoring.heldBy", { deployment: p.deployment }) : t("monitoring.tracked")}
                      />
                    ) : (
                      <NodeState state="warn" label={t("monitoring.untracked")} />
                    )}
                  </div>
                </Td>
                <Td numeric muted>
                  {formatMb(p.used_memory)}
                </Td>
                <Td actions>
                  <IconButton
                    size="sm"
                    variant="danger"
                    icon={OctagonX}
                    loading={killing === p.pid}
                    label={t("nodesPage.killFor", { pid: p.pid, name: p.process_name })}
                    onClick={() => setPendingKill(p)}
                  />
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
      <ConfirmModal
        open={pendingKill !== null}
        onClose={() => setPendingKill(null)}
        onConfirm={() => pendingKill && kill(pendingKill)}
        title={t("monitoring.killTitle")}
        confirmLabel={t("monitoring.killConfirm")}
        confirmVariant="danger"
        message={pendingKill ? killMessage(pendingKill, stats, language) : ""}
      />
      <AlertModal open={alert !== null} onClose={() => setAlert(null)} title={alert?.title ?? ""} message={alert?.message ?? ""} />
    </Section>
  );
}

/** Host memory and every disk the node reports. */
export function NodeStorage({ stats }: { stats: NodeStats }) {
  const { t } = useI18n();
  if (!stats.reachable) return null;
  return (
    <Section title={t("nodesPage.hostAndDisk")}>
      {stats.cpu.total <= 0 && stats.disk.length === 0 ? (
        <p className="text-[12.5px] text-muted">{t("nodesPage.noStorage")}</p>
      ) : (
        <div className="space-y-3">
          {stats.cpu.total > 0 && (
            <Labelled
              label={t("monitoring.ram")}
              value={`${formatMb(stats.cpu.used)} / ${formatMb(stats.cpu.total)}`}
              percent={(stats.cpu.used / stats.cpu.total) * 100}
            />
          )}
          {stats.disk.map((d) => (
            <div key={d.mount} data-testid={`disk-${stats.id}-${d.mount}`}>
              <Labelled
                label={<span className="font-mono">{d.mount}</span>}
                value={t("nodesPage.diskFree", { free: `${(d.free / 1024 ** 3).toFixed(1)} GB`, percent: d.usage_percent })}
                percent={d.usage_percent}
              />
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}
