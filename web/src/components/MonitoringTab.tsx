/** Monitoring: what every node is doing, said one node at a time.
 *
 * This was a page of its own — `/monitoring` — showing one machine, whichever
 * one the control plane was installed on, with nothing on it saying which. On
 * a cluster that is not a smaller answer, it is a wrong one: three of four
 * Sparks were invisible and the visible one was unlabelled. Every panel now
 * sits under the node it belongs to, and a node that could not be asked keeps
 * its section and says so, because a missing section and an idle machine look
 * the same.
 *
 * It is a tab of Fleet now rather than a page, because "which machines do I
 * have" and "what are they doing" are two readings of the same hardware.
 * `/monitoring` still opens it.
 */

import { useEffect, useMemo, useState } from "react";
import { translate, useI18n, type Language } from "@/lib/i18n";
import { connectMetricsStream, fetchMemory, killGpuProcess } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { Activity, OctagonX } from "lucide-react";
import { AlertModal, Button, ConfirmModal, ErrorLine, NodeState, Spinner } from "@/ui";
import { cn } from "@/lib/utils";
import type { GPUProcess, GPUStats, MemoryResponse, NodeStats } from "@/lib/types";
import { HealthHistoryChart, type HealthSeries } from "@/components/HealthHistoryChart";

/** One reading of a GPU, as the metrics stream reported it. */
interface GPUSample {
  t: number;
  utilization: number | null;
  temperature: number | null;
}

/** An hour of five-second frames. The series lives in the tab, not on disk. */
const MAX_SAMPLES = 720;

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

/** The hub's number tile: an 11px uppercase label over a 22px figure. */
function Tile({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">{label}</p>
      <p className="mt-1 text-[22px] font-bold tracking-[-0.02em] tabular-nums">{value}</p>
    </div>
  );
}

/** A usage bar in the token colours — good below 70, warn to 90, bad above.
 *  The colour is a class, so it follows the theme; only the width is inline,
 *  because a percentage is data. */
function Bar({ percent }: { percent: number }) {
  const pct = Math.max(0, Math.min(100, percent));
  const tone = pct > 90 ? "bg-bad" : pct > 70 ? "bg-warn" : "bg-good";
  return (
    <div className="h-2 rounded-full bg-bg border border-line overflow-hidden">
      <div
        className={cn("h-full rounded-full transition-[width] duration-500", tone)}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

export default function MonitoringTab() {
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
          title: t("fleet.killFailed", { pid: proc.pid }),
          message:
            result?.error ??
            t("fleet.killNoReason", { name: proc.process_name, pid: proc.pid }),
        });
      }
      await refetch();
      setSse(null);
    } catch (e) {
      setAlert({
        title: t("fleet.killError", { pid: proc.pid }),
        message: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setKilling(null);
    }
  }

  return (
    <div>
      {loading && !sse && (
        <div className="flex justify-center py-20">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      )}
      {!sse && <ErrorLine>{error}</ErrorLine>}
      {!d && !loading && (
        <div className="text-center py-20 text-muted">
          <Activity size={40} className="mx-auto mb-4 opacity-50" />
          <p>{t("monitoring.noData")}</p>
        </div>
      )}

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

/** One machine: what it is, then what it is doing.
 *
 * Separated by a rule rather than boxed in a card — the page is a list of
 * machines, and four cards on four Sparks reads as four unrelated things. */
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
    <section
      data-testid={`node-${node.id || node.address}`}
      className="border-t border-line pt-10 mt-10 first:border-t-0 first:pt-0 first:mt-0 space-y-5"
    >
      <div className="flex items-center gap-3 flex-wrap">
        <h3 className="text-[17px] font-semibold tracking-[-0.02em]">{label}</h3>
        {node.address && <span className="font-mono text-[12.5px] text-muted">{node.address}</span>}
        {node.is_control_plane && (
          <span className="text-[13px] px-2 py-0.5 rounded-full border border-line text-muted">
            {t("monitoring.controlPlane")}
          </span>
        )}
        <span data-testid={`node-condition-${node.id || node.address}`}>
          {node.reachable ? (
            <NodeState state="ok" />
          ) : (
            <NodeState
              state="unknown"
              label={t("monitoring.unreachable")}
              title={node.error ?? undefined}
            />
          )}
        </span>
      </div>

      {!node.reachable ? (
        // Unknown, not empty: the node did not answer, and pretending it
        // answered with nothing is how a page reports a busy machine as idle.
        <p className="text-[14px] text-muted">{node.error}</p>
      ) : (
        <>
          {node.unavailable.length > 0 && (
            <div className="text-warn text-[13px] space-y-1">
              {node.unavailable.map((what) => (
                <div key={what}>{t("monitoring.couldNotRead", { what })}</div>
              ))}
            </div>
          )}
          {node.gpu.map((gpu) => (
            <GPUPanel
              key={`${node.id}-${gpu.uuid || gpu.gpu}`}
              gpu={gpu}
              node={node}
              series={series[seriesKey(node.id, gpu)] ?? []}
              killing={killing}
              onKill={onKill}
            />
          ))}
          <div className="grid grid-cols-1 gap-8 min-[900px]:grid-cols-2">
            <CPUPanel cpu={node.cpu} />
            {node.disk.map((disk) => <DiskPanel key={`${node.id}-${disk.mount}`} disk={disk} />)}
          </div>
        </>
      )}
    </section>
  );
}

function GPUPanel({
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
    <div className="space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <h4 className="text-[15px] font-semibold">{gpu.name || gpu.gpu}</h4>
        {gpu.temperature && (
          <span
            className={cn(
              "text-[13px] px-2 py-0.5 rounded-full border border-line",
              (gpu.temperature ?? 0) > 80
                ? "text-bad"
                : (gpu.temperature ?? 0) > 65
                  ? "text-warn"
                  : "text-good",
            )}
          >
            {gpu.temperature}°C
          </span>
        )}
        <span className="font-mono text-[12.5px] text-muted break-all">{gpu.uuid}</span>
      </div>

      {gpu.memory_supported ? (
        <div className="space-y-1.5">
          <div className="flex justify-between text-[14px]">
            <span className="text-muted">{t("monitoring.memory")}</span>
            <span className="font-mono">{gpu.memory_used} / {gpu.memory_total} MB</span>
          </div>
          <Bar percent={pct} />
          <div className="flex justify-between text-[13px] text-muted">
            <span>{pct.toFixed(1)}%</span>
            <span>{gpu.memory_free} MB free</span>
          </div>
        </div>
      ) : (
        <p className="text-[13px] text-muted">{t("monitoring.unified")}</p>
      )}

      <div className="grid grid-cols-2 gap-4 min-[600px]:grid-cols-4">
        <Tile label={t("monitoring.utilization")} value={`${gpu.utilization ?? "—"}%`} />
        <Tile label={t("monitoring.temperature")} value={`${gpu.temperature ?? "—"}°C`} />
        <Tile label={t("monitoring.powerDraw")} value={`${gpu.power_draw ?? "—"} W`} />
        <Tile label={t("monitoring.powerLimit")} value={`${gpu.power_limit ?? "—"} W`} />
      </div>

      <HealthHistoryChart
        title={t("monitoring.liveHistory")}
        caption={t("fleet.historyCaption")}
        series={series}
      />

      {gpuProcs.length > 0 && (
        <div className="space-y-2">
          <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
            {t("monitoring.gpuProcesses")}
          </p>
          {/* Rows rather than a table: under 900 each one stacks into a card,
              and a table that scrolls sideways on a phone is a table nobody
              reads. The Kill button is 44px tall below 600 — a destructive
              control a thumb has to hit exactly. */}
          <div role="list" className="divide-y divide-line border-y border-line">
            {gpuProcs.map((p) => (
              <div
                key={p.pid}
                role="listitem"
                aria-label={`PID ${p.pid} ${p.process_name}`}
                data-testid={`gpu-process-${node.id}-${p.pid}`}
                className="flex flex-col gap-2 py-3 min-[900px]:flex-row min-[900px]:items-center min-[900px]:gap-4"
              >
                <span className="font-mono text-[12.5px] text-muted min-[900px]:w-20 shrink-0">
                  {p.pid}
                </span>
                <span className="min-w-0 flex-1 flex flex-wrap items-center gap-2">
                  <span className="text-[14px]">{p.process_name}</span>
                  {/* Whose it is, not merely whether somebody claims it: the
                      node says which container the process is in, and the
                      control plane knows what it started that container for.
                      A container of ours with no deployment label is still
                      ours — an older build's, or a rank from before the label
                      existed. */}
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
                <span className="font-mono text-[12.5px] text-muted min-[900px]:w-24 shrink-0">
                  {p.used_memory} MB
                </span>
                <Button
                  size="sm"
                  variant="danger"
                  icon={OctagonX}
                  loading={killing === p.pid}
                  title={t("monitoring.killProcess")}
                  onClick={() => onKill(p)}
                  className="self-start min-h-[44px] min-[600px]:min-h-0"
                >
                  {t("monitoring.kill")}
                </Button>
              </div>
            ))}
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

function CPUPanel({ cpu }: { cpu: { total: number; used: number; free: number; available: number; usage_percent: number } }) {
  const { t } = useI18n();
  const pct = cpu.total > 0 ? (cpu.used / cpu.total) * 100 : 0;
  const gb = (mb: number) => `${(mb / 1024).toFixed(1)} GB`;
  return (
    <div className="space-y-4">
      <h4 className="text-[15px] font-semibold">{t("monitoring.cpuMemory")}</h4>
      <div className="space-y-1.5">
        <div className="flex justify-between text-[14px]">
          <span className="text-muted">{t("monitoring.ram")}</span>
          <span className="font-mono">{(cpu.used / 1024).toFixed(1)} / {(cpu.total / 1024).toFixed(1)} GB</span>
        </div>
        <Bar percent={pct} />
      </div>
      <div className="grid grid-cols-3 gap-4">
        <Tile label={t("monitoring.used")} value={gb(cpu.used)} />
        <Tile label={t("monitoring.free")} value={gb(cpu.free ?? 0)} />
        <Tile label={t("monitoring.avail")} value={gb(cpu.available)} />
      </div>
    </div>
  );
}

function DiskPanel({ disk }: { disk: { mount: string; total: number; used: number; free: number; usage_percent: number } }) {
  const { t } = useI18n();
  const gb = (b: number) => (b / (1024 ** 3)).toFixed(1);
  return (
    <div className="space-y-4">
      <h4 className="text-[15px] font-semibold">{disk.mount}</h4>
      <div className="space-y-1.5">
        <div className="flex justify-between text-[14px]">
          <span className="text-muted">{t("monitoring.usage")}</span>
          <span className="font-mono">{disk.usage_percent}%</span>
        </div>
        <Bar percent={disk.usage_percent} />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <Tile label={t("monitoring.used")} value={`${gb(disk.used)} GB`} />
        <Tile label={t("monitoring.free")} value={`${gb(disk.free)} GB`} />
      </div>
    </div>
  );
}
