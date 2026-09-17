/** Monitoring: what every node is doing, said one node at a time.
 *
 * This page used to show one machine — whichever one the control plane was
 * installed on — with nothing on it saying which. On a cluster that is not a
 * smaller answer, it is a wrong one: three of four Sparks were invisible and
 * the visible one was unlabelled. Every panel now sits under the node it
 * belongs to, and a node that could not be asked keeps its section and says
 * so, because a missing section and an idle machine look the same.
 */

import { useEffect, useMemo, useState } from "react";
import { translate, useI18n, type Language } from "@/lib/i18n";
import { connectMetricsStream, fetchMemory, killGpuProcess } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { Activity, Cpu, HardDrive, Zap, Workflow, OctagonX, Server } from "lucide-react";
import { Button, ErrorLine, NodeState, Spinner } from "@/ui";
import type { GPUProcess, GPUStats, MemoryResponse, NodeStats } from "@/lib/types";
import { AlertModal, ConfirmModal } from "@/components/Modal";
import { HealthHistoryChart, type HealthSeries } from "@/components/HealthHistoryChart";

/** One reading of a GPU, as the metrics stream reported it. */
interface GPUSample {
  t: number;
  utilization: number | null;
  temperature: number | null;
}

/** An hour of five-second frames. The series lives in the tab, not on disk. */
const MAX_SAMPLES = 720;

const HISTORY_CAPTION =
  "Sampled from the live metrics stream since this page was opened. Nothing stores it, so it starts over on reload.";

/** A GPU's series key.
 *
 * Node first, because a GPU UUID is unique per machine and two Sparks in a
 * cluster can — and in simulation do — report the same one. Keyed by UUID
 * alone, one node's history would be drawn under another's card.
 */
// `|` cannot appear in a machine id (hex) or a GPU uuid (`GPU-<hex>`), so it
// separates them unambiguously without a control byte in the source.
const seriesKey = (nodeId: string, gpu: GPUStats) => `${nodeId}|${gpu.uuid}`;

/** The two things the metrics frame actually carries per GPU. */
function gpuSeries(samples: GPUSample[]): HealthSeries[] {
  const of = (pick: (s: GPUSample) => number | null) =>
    samples.filter((s) => pick(s) !== null).map((s) => ({ t: s.t, value: pick(s) as number }));
  return [
    {
      label: "GPU utilization",
      unit: "%",
      color: "var(--color-primary)",
      samples: of((s) => s.utilization),
    },
    {
      label: "Temperature",
      unit: "°C",
      color: "var(--color-warning)",
      samples: of((s) => s.temperature),
    },
  ];
}

export default function MemoryPage() {
  const { t, language } = useI18n();
  const { data: memory, loading, error, refetch } = useQuery(fetchMemory);
  const [sse, setSse] = useState<MemoryResponse | null>(null);
  const [killing, setKilling] = useState<number | null>(null);
  const [pendingKill, setPendingKill] = useState<{ process: GPUProcess; node: NodeStats } | null>(null);
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);
  const [history, setHistory] = useState<Record<string, GPUSample[]>>({});
  const d = sse || memory;
  const nodes = d?.nodes ?? [];

  useEffect(() => {
    const stop = connectMetricsStream((event, data) => { if (event === "metrics") setSse(data as MemoryResponse); });
    return stop;
  }, []);

  // Accumulate the readings that go past. Every point here was reported by the
  // backend; none is interpolated, and a GPU that stops reporting simply stops
  // gaining points.
  useEffect(() => {
    if (nodes.length === 0) return;
    const t = Date.now();
    setHistory((prev) => {
      const next = { ...prev };
      for (const node of nodes) {
        for (const gpu of node.gpu) {
          const key = seriesKey(node.id, gpu);
          next[key] = [
            ...(next[key] ?? []),
            { t, utilization: gpu.utilization, temperature: gpu.temperature },
          ].slice(-MAX_SAMPLES);
        }
      }
      return next;
    });
  }, [nodes]);

  const series = useMemo(() => {
    const byGpu: Record<string, HealthSeries[]> = {};
    for (const [key, samples] of Object.entries(history)) byGpu[key] = gpuSeries(samples);
    return byGpu;
  }, [history]);

  async function handleKill(proc: GPUProcess, node: NodeStats) {
    setPendingKill(null);
    setKilling(proc.pid);
    try {
      const result = await killGpuProcess(proc.pid, node.address);
      // The API answers `{killed, error}`; a refusal and a success are the
      // same HTTP 200, so the flag is the only thing that separates them.
      if (!result?.killed) {
        setAlert({
          title: `PID ${proc.pid} was not killed`,
          message:
            result?.error ??
            `The backend refused to signal ${proc.process_name} (PID ${proc.pid}) and gave no reason.`,
        });
      }
      await refetch();
      setSse(null);
    } catch (e) {
      setAlert({
        title: `Could not kill PID ${proc.pid}`,
        message: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setKilling(null);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">{t("monitoring.title")}</h2>
        <p className="text-text-muted mt-1">{t("monitoring.subtitle")}</p>
      </div>

      {loading && !sse && (
        <div className="flex justify-center py-20">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      )}
      {!sse && <ErrorLine>{error}</ErrorLine>}
      {!d && !loading && <div className="text-center py-20 text-text-muted"><Activity size={40} className="mx-auto mb-4 opacity-50" /><p>{t("monitoring.noData")}</p></div>}

      {nodes.map((node) => (
        <NodeSection
          key={node.id || node.address}
          node={node}
          series={series}
          killing={killing}
          onKill={(process) => setPendingKill({ process, node })}
        />
      ))}

      <ConfirmModal
        open={pendingKill !== null}
        onClose={() => setPendingKill(null)}
        onConfirm={() => pendingKill && handleKill(pendingKill.process, pendingKill.node)}
        title={t("monitoring.killTitle")}
        confirmLabel={t("monitoring.killConfirm")}
        confirmVariant="danger"
        message={pendingKill ? killMessage(pendingKill.process, pendingKill.node, language) : ""}
      />

      <AlertModal
        open={alert !== null}
        onClose={() => setAlert(null)}
        title={alert?.title ?? ""}
        message={alert?.message ?? ""}
      />
    </div>
  );
}

/** One machine: what it is, then what it is doing. */
function NodeSection({
  node,
  series,
  killing,
  onKill,
}: {
  node: NodeStats;
  series: Record<string, HealthSeries[]>;
  killing: number | null;
  onKill: (process: GPUProcess) => void;
}) {
  const { t } = useI18n();
  const label = node.name || node.address || t("monitoring.controlPlane");

  return (
    <section data-testid={`node-${node.id || node.address}`} className="space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        <Server size={16} className="text-text-muted" />
        <h3 className="font-semibold">{label}</h3>
        {node.address && <span className="text-xs font-mono text-text-muted">{node.address}</span>}
        {node.is_control_plane && (
          <span className="text-[13px] px-2 py-0.5 rounded-full border border-line text-muted">
            {t("monitoring.controlPlane")}
          </span>
        )}
        {!node.reachable && (
          <span data-testid={`unreachable-${node.id || node.address}`}>
            <NodeState state="unknown" label={t("monitoring.unreachable")} title={node.error ?? undefined} />
          </span>
        )}
      </div>

      {!node.reachable ? (
        // Unknown, not empty: the node did not answer, and pretending it
        // answered with nothing is how a page reports a busy machine as idle.
        <div className="rounded-md bg-surface border border-line p-5 text-[14px] text-muted">
          {node.error}
        </div>
      ) : (
        <>
          {node.unavailable.length > 0 && (
            <div className="rounded-sm border border-warn/40 text-warn text-[13px] px-3 py-2 space-y-1">
              {node.unavailable.map((what) => (
                <div key={what}>{t("monitoring.couldNotRead", { what })}</div>
              ))}
            </div>
          )}
          {node.gpu.map((gpu) => (
            <GPUCard
              key={`${node.id}-${gpu.uuid || gpu.gpu}`}
              gpu={gpu}
              node={node}
              series={series[seriesKey(node.id, gpu)] ?? []}
              killing={killing}
              onKill={onKill}
            />
          ))}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <CPUCard cpu={node.cpu} />
            {node.disk.map((disk) => <DiskCard key={`${node.id}-${disk.mount}`} disk={disk} />)}
          </div>
        </>
      )}
    </section>
  );
}

function GPUCard({
  gpu,
  node,
  series,
  killing,
  onKill,
}: {
  gpu: GPUStats;
  node: NodeStats;
  series: HealthSeries[];
  killing: number | null;
  onKill: (process: GPUProcess) => void;
}) {
  const { t } = useI18n();
  const pct = gpu.memory_total > 0 ? (gpu.memory_used / gpu.memory_total) * 100 : 0;
  const gpuProcs = node.processes.filter((p) => p.gpu_uuid === gpu.uuid);

  return (
    <div className="rounded-md bg-surface border border-line p-5">
      <div className="flex items-center gap-2 mb-4">
        <Zap size={18} className="text-blue2" /><h3 className="font-semibold">{gpu.name || gpu.gpu}</h3>
        {gpu.temperature && <span className={`text-xs px-2 py-0.5 rounded-full ${(gpu.temperature ?? 0) > 80 ? "bg-danger/20 text-danger" : (gpu.temperature ?? 0) > 65 ? "bg-warning/20 text-warning" : "bg-success/20 text-success"}`}>{gpu.temperature}°C</span>}
      </div>
      <div className="text-xs text-text-muted mb-3 font-mono break-all">{gpu.uuid}</div>
      <div className="mb-4">
        {gpu.memory_supported ? (
          <>
            <div className="flex justify-between text-sm mb-1"><span className="text-text-muted">{t("monitoring.memory")}</span><span className="font-mono">{gpu.memory_used} / {gpu.memory_total} MB</span></div>
            <div className="h-3 rounded-full bg-bg overflow-hidden"><div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, backgroundColor: pct > 90 ? "var(--color-danger)" : pct > 70 ? "var(--color-warning)" : "var(--color-primary)" }} /></div>
            <div className="flex justify-between text-xs text-text-muted mt-1"><span>{pct.toFixed(1)}%</span><span>{gpu.memory_free} MB free</span></div>
          </>
        ) : (
          <div className="text-xs text-text-muted px-2 py-1.5 rounded bg-bg">{t("monitoring.unified")}</div>
        )}
      </div>
      <div className="grid gap-2 text-sm" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(9rem, 1fr))" }}>
        <div className="p-2 rounded bg-bg"><span className="text-text-muted text-xs">{t("monitoring.utilization")}</span><p className="font-mono">{gpu.utilization ?? "—"}%</p></div>
        <div className="p-2 rounded bg-bg"><span className="text-text-muted text-xs">{t("monitoring.temperature")}</span><p className="font-mono">{gpu.temperature ?? "—"}°C</p></div>
        <div className="p-2 rounded bg-bg"><span className="text-text-muted text-xs">{t("monitoring.powerDraw")}</span><p className="font-mono">{gpu.power_draw ?? "—"} W</p></div>
        <div className="p-2 rounded bg-bg"><span className="text-text-muted text-xs">{t("monitoring.powerLimit")}</span><p className="font-mono">{gpu.power_limit ?? "—"} W</p></div>
      </div>
      <HealthHistoryChart
        className="mt-4"
        title={t("monitoring.liveHistory")}
        caption={HISTORY_CAPTION}
        series={series}
      />
      {gpuProcs.length > 0 && (
        <div className="mt-4 pt-4 border-t border-border">
          <div className="flex items-center gap-2 mb-3"><Workflow size={14} className="text-blue2" /><span className="text-sm font-semibold">{t("monitoring.gpuProcesses")}</span></div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-text-muted text-xs uppercase">
                <tr>
                  <th className="text-left py-1.5 pr-4">{t("monitoring.pid")}</th>
                  <th className="text-left py-1.5 pr-4">{t("monitoring.process")}</th>
                  <th className="text-left py-1.5 pr-4">{t("monitoring.memory")}</th>
                  <th className="text-left py-1.5"></th>
                </tr>
              </thead>
              <tbody>
                {gpuProcs.map((p) => (
                  <tr key={p.pid} className="border-t border-border/60">
                    <td className="py-1.5 pr-4 font-mono text-xs">{p.pid}</td>
                    <td className="py-1.5 pr-4">
                      <span>{p.process_name}</span>
                      {/* Whose it is, not merely whether somebody claims it:
                          the node says which container the process is in, and
                          the control plane knows what it started that
                          container for. A container of ours with no
                          deployment label is still ours — an older build's,
                          or a rank from before the label existed. */}
                      <span className="ml-2 inline-flex">
                        {p.is_tracked ? (
                          <NodeState
                            state="ok"
                            label={
                              p.deployment
                                ? t("monitoring.heldBy", { deployment: p.deployment })
                                : t("monitoring.tracked")
                            }
                          />
                        ) : (
                          <NodeState state="warn" label={t("monitoring.untracked")} />
                        )}
                      </span>
                    </td>
                    <td className="py-1.5 pr-4 font-mono text-xs">{p.used_memory} MB</td>
                    <td className="py-1.5">
                      <Button
                        size="sm"
                        variant="danger"
                        icon={OctagonX}
                        loading={killing === p.pid}
                        title={t("monitoring.killProcess")}
                        onClick={() => onKill(p)}
                      >
                        {t("monitoring.kill")}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

/** Name what dies, in the terms the row already showed the operator. */
function killMessage(p: GPUProcess, node: NodeStats, language: Language): string {
  const held = translate(language, "monitoring.killHeld", { mb: p.used_memory });
  const provenance = [
    translate(
      language,
      p.is_tracked ? "monitoring.killTracked" : "monitoring.killUntracked",
    ),
    translate(language, "monitoring.killOnNode", {
      node: node.name || node.address || translate(language, "monitoring.controlPlane"),
    }),
  ].join(" ");
  return translate(language, "monitoring.killBody", {
    pid: p.pid,
    name: p.process_name,
    held,
    provenance,
  });
}

function CPUCard({ cpu }: { cpu: { total: number; used: number; free: number; available: number; usage_percent: number } }) {
  const { t } = useI18n();
  const pct = cpu.total > 0 ? (cpu.used / cpu.total) * 100 : 0;
  return (
    <div className="rounded-md bg-surface border border-line p-5">
      <div className="flex items-center gap-2 mb-4"><Cpu size={18} className="text-blue2" /><h3 className="font-semibold">{t("monitoring.cpuMemory")}</h3></div>
      <div className="mb-3">
        <div className="flex justify-between text-sm mb-1"><span className="text-text-muted">{t("monitoring.ram")}</span><span className="font-mono">{(cpu.used / 1024).toFixed(1)} / {(cpu.total / 1024).toFixed(1)} GB</span></div>
        <div className="h-3 rounded-full bg-bg overflow-hidden"><div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, backgroundColor: pct > 90 ? "var(--color-danger)" : pct > 70 ? "var(--color-warning)" : "var(--color-primary)" }} /></div>
      </div>
      <div className="grid grid-cols-3 gap-2 text-sm text-center">
        <div className="p-2 rounded bg-bg"><p className="text-text-muted text-xs">{t("monitoring.used")}</p><p className="font-mono">{(cpu.used / 1024).toFixed(1)} GB</p></div>
        <div className="p-2 rounded bg-bg"><p className="text-text-muted text-xs">{t("monitoring.free")}</p><p className="font-mono">{((cpu.free ?? 0) / 1024).toFixed(1)} GB</p></div>
        <div className="p-2 rounded bg-bg"><p className="text-text-muted text-xs">{t("monitoring.avail")}</p><p className="font-mono">{(cpu.available / 1024).toFixed(1)} GB</p></div>
      </div>
    </div>
  );
}

function DiskCard({ disk }: { disk: { mount: string; total: number; used: number; free: number; usage_percent: number } }) {
  const { t } = useI18n();
  const gb = (b: number) => (b / (1024 ** 3)).toFixed(1);
  return (
    <div className="rounded-md bg-surface border border-line p-5">
      <div className="flex items-center gap-2 mb-4"><HardDrive size={18} className="text-blue2" /><h3 className="font-semibold">{disk.mount}</h3></div>
      <div className="mb-3">
        <div className="flex justify-between text-sm mb-1"><span className="text-text-muted">{t("monitoring.usage")}</span><span className="font-mono">{disk.usage_percent}%</span></div>
        <div className="h-3 rounded-full bg-bg overflow-hidden"><div className="h-full rounded-full transition-all duration-500" style={{ width: `${disk.usage_percent}%`, backgroundColor: disk.usage_percent > 90 ? "var(--color-danger)" : disk.usage_percent > 70 ? "var(--color-warning)" : "var(--color-primary)" }} /></div>
      </div>
      <div className="text-sm text-text-muted">{t("monitoring.diskSummary", { used: gb(disk.used), total: gb(disk.total) })}</div>
    </div>
  );
}
