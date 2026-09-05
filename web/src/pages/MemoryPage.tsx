import { useEffect, useMemo, useState } from "react";
import { connectMetricsStream, fetchMemory, killGpuProcess } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { Activity, Cpu, HardDrive, Loader2, AlertCircle, Zap, Workflow, OctagonX } from "lucide-react";
import type { GPUProcess, MemoryResponse } from "@/lib/types";
import { setRefresh } from "@/lib/refresh";
import { AlertModal, ConfirmModal } from "@/components/Modal";
import { HealthHistoryChart, type HealthSeries } from "@/components/HealthBadge";

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
  const { data: memory, loading, error, refetch } = useQuery(fetchMemory);
  const [sse, setSse] = useState<MemoryResponse | null>(null);
  const [killing, setKilling] = useState<number | null>(null);
  const [pendingKill, setPendingKill] = useState<GPUProcess | null>(null);
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);
  const [history, setHistory] = useState<Record<string, GPUSample[]>>({});
  const d = sse || memory;

  useEffect(() => { setRefresh(refetch); }, [refetch]);
  useEffect(() => {
    const stop = connectMetricsStream((event, data) => { if (event === "metrics") setSse(data as MemoryResponse); });
    return stop;
  }, []);

  // Accumulate the readings that go past. Every point here was reported by the
  // backend; none is interpolated, and a GPU that stops reporting simply stops
  // gaining points.
  useEffect(() => {
    if (!d) return;
    const t = Date.now();
    setHistory((prev) => {
      const next = { ...prev };
      for (const gpu of d.gpu) {
        next[gpu.uuid] = [
          ...(next[gpu.uuid] ?? []),
          { t, utilization: gpu.utilization, temperature: gpu.temperature },
        ].slice(-MAX_SAMPLES);
      }
      return next;
    });
  }, [d]);

  const series = useMemo(() => {
    const byGpu: Record<string, HealthSeries[]> = {};
    for (const [uuid, samples] of Object.entries(history)) byGpu[uuid] = gpuSeries(samples);
    return byGpu;
  }, [history]);

  async function handleKill(proc: GPUProcess) {
    setPendingKill(null);
    setKilling(proc.pid);
    try {
      const result = await killGpuProcess(proc.pid);
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
        <h2 className="text-2xl font-bold">Monitoring</h2>
        <p className="text-text-muted mt-1">Real-time GPU, CPU, and disk usage</p>
      </div>

      {loading && !sse && <div className="flex justify-center py-20"><Loader2 className="animate-spin text-primary" size={32} /></div>}
      {error && !sse && <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3"><AlertCircle size={20} /><span>{error}</span></div>}
      {!d && !loading && <div className="text-center py-20 text-text-muted"><Activity size={40} className="mx-auto mb-4 opacity-50" /><p>No data available.</p></div>}

      {d && (
        <div className="space-y-6">
          {d.gpu.length > 0 && <div className="space-y-4">
            {d.gpu.map((gpu) => {
              const pct = gpu.memory_total > 0 ? (gpu.memory_used / gpu.memory_total) * 100 : 0;
              const gpuProcs = d.processes.filter(p => p.gpu_uuid === gpu.uuid);
              return (
                <div key={gpu.gpu} className="rounded-xl bg-surface border border-border p-5">
                  <div className="flex items-center gap-2 mb-4">
                    <Zap size={18} className="text-primary" /><h3 className="font-semibold">{gpu.name || gpu.gpu}</h3>
                    {gpu.temperature && <span className={`text-xs px-2 py-0.5 rounded-full ${(gpu.temperature ?? 0) > 80 ? "bg-danger/20 text-danger" : (gpu.temperature ?? 0) > 65 ? "bg-warning/20 text-warning" : "bg-success/20 text-success"}`}>{gpu.temperature}°C</span>}
                  </div>
                  <div className="text-xs text-text-muted mb-3 font-mono break-all">{gpu.uuid}</div>
                  <div className="mb-4">
                    {gpu.memory_supported ? (
                      <>
                        <div className="flex justify-between text-sm mb-1"><span className="text-text-muted">Memory</span><span className="font-mono">{gpu.memory_used} / {gpu.memory_total} MB</span></div>
                        <div className="h-3 rounded-full bg-bg overflow-hidden"><div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, backgroundColor: pct > 90 ? "var(--color-danger)" : pct > 70 ? "var(--color-warning)" : "var(--color-primary)" }} /></div>
                        <div className="flex justify-between text-xs text-text-muted mt-1"><span>{pct.toFixed(1)}%</span><span>{gpu.memory_free} MB free</span></div>
                      </>
                    ) : (
                      <div className="text-xs text-text-muted px-2 py-1.5 rounded bg-bg">Unified memory — usage not reported by nvidia-smi</div>
                    )}
                  </div>
                  <div className="grid gap-2 text-sm" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(9rem, 1fr))" }}>
                    <div className="p-2 rounded bg-bg"><span className="text-text-muted text-xs">Utilization</span><p className="font-mono">{gpu.utilization ?? "—"}%</p></div>
                    <div className="p-2 rounded bg-bg"><span className="text-text-muted text-xs">Temperature</span><p className="font-mono">{gpu.temperature ?? "—"}°C</p></div>
                    <div className="p-2 rounded bg-bg"><span className="text-text-muted text-xs">Power Draw</span><p className="font-mono">{gpu.power_draw ?? "—"} W</p></div>
                    <div className="p-2 rounded bg-bg"><span className="text-text-muted text-xs">Power Limit</span><p className="font-mono">{gpu.power_limit ?? "—"} W</p></div>
                  </div>
                  <HealthHistoryChart
                    className="mt-4"
                    title="Live history"
                    caption={HISTORY_CAPTION}
                    series={series[gpu.uuid] ?? []}
                  />
                  {gpuProcs.length > 0 && (
                    <div className="mt-4 pt-4 border-t border-border">
                      <div className="flex items-center gap-2 mb-3"><Workflow size={14} className="text-primary" /><span className="text-sm font-semibold">GPU Processes</span></div>
                      <div className="overflow-x-auto">
                        <table className="w-full text-sm">
                          <thead className="text-text-muted text-xs uppercase">
                            <tr>
                              <th className="text-left py-1.5 pr-4">PID</th>
                              <th className="text-left py-1.5 pr-4">Process</th>
                              <th className="text-left py-1.5 pr-4">Memory</th>
                              <th className="text-left py-1.5"></th>
                            </tr>
                          </thead>
                          <tbody>
                            {gpuProcs.map((p) => (
                              <tr key={p.pid} className="border-t border-border/60">
                                <td className="py-1.5 pr-4 font-mono text-xs">{p.pid}</td>
                                <td className="py-1.5 pr-4">
                                  <span>{p.process_name}</span>
                                  {p.is_tracked === false && (
                                    <span className="ml-2 text-xs px-1.5 py-0.5 rounded bg-warning/15 text-warning border border-warning/30">untracked</span>
                                  )}
                                </td>
                                <td className="py-1.5 pr-4 font-mono text-xs">{p.used_memory} MB</td>
                                <td className="py-1.5">
                                  <button
                                    onClick={() => setPendingKill(p)}
                                    disabled={killing === p.pid}
                                    className="flex items-center gap-1 px-2 py-1 text-xs rounded bg-danger/10 text-danger border border-danger/30 hover:bg-danger/20 transition-colors disabled:opacity-50"
                                    title="Kill process (SIGTERM)"
                                  >
                                    {killing === p.pid ? <Loader2 size={12} className="animate-spin" /> : <OctagonX size={12} />}
                                    Kill
                                  </button>
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
            })}
          </div>}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <CPUCard cpu={d.cpu} />
            {d.disk.map((disk) => <DiskCard key={disk.mount} disk={disk} />)}
          </div>
        </div>
      )}

      <ConfirmModal
        open={pendingKill !== null}
        onClose={() => setPendingKill(null)}
        onConfirm={() => pendingKill && handleKill(pendingKill)}
        title="Kill this GPU process?"
        confirmLabel="Kill process"
        confirmVariant="danger"
        message={pendingKill ? killMessage(pendingKill) : ""}
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

/** Name what dies, in the terms the row already showed the operator. */
function killMessage(p: GPUProcess): string {
  const held = `${p.used_memory} MB of GPU memory`;
  const provenance =
    p.is_tracked === false
      ? "Nothing on this page started it, so it belongs to something else on this machine."
      : "It belongs to a deployment this page is tracking, which will stop.";
  return `SIGTERM will be sent to PID ${p.pid} (${p.process_name}), holding ${held}. ${provenance} Any inference run it is serving stops immediately, and this cannot be undone.`;
}

function CPUCard({ cpu }: { cpu: { total: number; used: number; free: number; available: number; usage_percent: number } }) {
  const pct = cpu.total > 0 ? (cpu.used / cpu.total) * 100 : 0;
  return (
    <div className="rounded-xl bg-surface border border-border p-5">
      <div className="flex items-center gap-2 mb-4"><Cpu size={18} className="text-primary" /><h3 className="font-semibold">CPU Memory</h3></div>
      <div className="mb-3">
        <div className="flex justify-between text-sm mb-1"><span className="text-text-muted">RAM</span><span className="font-mono">{(cpu.used / 1024).toFixed(1)} / {(cpu.total / 1024).toFixed(1)} GB</span></div>
        <div className="h-3 rounded-full bg-bg overflow-hidden"><div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, backgroundColor: pct > 90 ? "var(--color-danger)" : pct > 70 ? "var(--color-warning)" : "var(--color-primary)" }} /></div>
      </div>
      <div className="grid grid-cols-3 gap-2 text-sm text-center">
        <div className="p-2 rounded bg-bg"><p className="text-text-muted text-xs">Used</p><p className="font-mono">{(cpu.used / 1024).toFixed(1)} GB</p></div>
        <div className="p-2 rounded bg-bg"><p className="text-text-muted text-xs">Free</p><p className="font-mono">{((cpu.free ?? 0) / 1024).toFixed(1)} GB</p></div>
        <div className="p-2 rounded bg-bg"><p className="text-text-muted text-xs">Avail</p><p className="font-mono">{(cpu.available / 1024).toFixed(1)} GB</p></div>
      </div>
    </div>
  );
}

function DiskCard({ disk }: { disk: { mount: string; total: number; used: number; free: number; usage_percent: number } }) {
  const gb = (b: number) => (b / (1024 ** 3)).toFixed(1);
  return (
    <div className="rounded-xl bg-surface border border-border p-5">
      <div className="flex items-center gap-2 mb-4"><HardDrive size={18} className="text-primary" /><h3 className="font-semibold">{disk.mount}</h3></div>
      <div className="mb-3">
        <div className="flex justify-between text-sm mb-1"><span className="text-text-muted">Usage</span><span className="font-mono">{disk.usage_percent}%</span></div>
        <div className="h-3 rounded-full bg-bg overflow-hidden"><div className="h-full rounded-full transition-all duration-500" style={{ width: `${disk.usage_percent}%`, backgroundColor: disk.usage_percent > 90 ? "var(--color-danger)" : disk.usage_percent > 70 ? "var(--color-warning)" : "var(--color-primary)" }} /></div>
      </div>
      <div className="text-sm text-text-muted">{gb(disk.used)} used / {gb(disk.total)} total</div>
    </div>
  );
}
