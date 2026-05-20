import { useEffect, useState } from "react";
import { connectMetricsStream, fetchMemory } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { Activity, Cpu, HardDrive, Loader2, AlertCircle, Zap } from "lucide-react";
import type { MemoryResponse } from "@/lib/types";
import { setRefresh } from "@/lib/refresh";

export default function MemoryPage() {
  const { data: memory, loading, error, refetch } = useQuery(fetchMemory);
  const [sse, setSse] = useState<MemoryResponse | null>(null);
  const d = sse || memory;

  useEffect(() => { setRefresh(refetch); }, [refetch]);
  useEffect(() => {
    const stop = connectMetricsStream((event, data) => { if (event === "metrics") setSse(data as MemoryResponse); });
    return stop;
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">Memory Monitor</h2>
        <p className="text-text-muted mt-1">Real-time GPU, CPU, and disk usage</p>
      </div>

      {loading && !sse && <div className="flex justify-center py-20"><Loader2 className="animate-spin text-primary" size={32} /></div>}
      {error && !sse && <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3"><AlertCircle size={20} /><span>{error}</span></div>}
      {!d && !loading && <div className="text-center py-20 text-text-muted"><Activity size={40} className="mx-auto mb-4 opacity-50" /><p>No data available.</p></div>}

      {d && (
        <div className="space-y-6">
          {d.gpu.length > 0 && <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {d.gpu.map((gpu) => {
              const pct = gpu.memory_total > 0 ? (gpu.memory_used / gpu.memory_total) * 100 : 0;
              return (
                <div key={gpu.gpu} className="rounded-xl bg-surface border border-border p-5">
                  <div className="flex items-center gap-2 mb-4">
                    <Zap size={18} className="text-primary" /><h3 className="font-semibold">{gpu.gpu}</h3>
                    {gpu.temperature && <span className={`text-xs px-2 py-0.5 rounded-full ${(gpu.temperature ?? 0) > 80 ? "bg-danger/20 text-danger" : (gpu.temperature ?? 0) > 65 ? "bg-warning/20 text-warning" : "bg-success/20 text-success"}`}>{gpu.temperature}°C</span>}
                  </div>
                  <div className="mb-3">
                    <div className="flex justify-between text-sm mb-1"><span className="text-text-muted">Memory</span><span className="font-mono">{gpu.memory_used} / {gpu.memory_total} MB</span></div>
                    <div className="h-3 rounded-full bg-bg overflow-hidden"><div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, backgroundColor: pct > 90 ? "var(--color-danger)" : pct > 70 ? "var(--color-warning)" : "var(--color-primary)" }} /></div>
                    <div className="flex justify-between text-xs text-text-muted mt-1"><span>{pct.toFixed(1)}%</span><span>{gpu.memory_free} MB free</span></div>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-sm">
                    <div className="p-2 rounded bg-bg"><span className="text-text-muted">Utilization</span><p className="font-mono">{gpu.utilization ?? "—"}%</p></div>
                    <div className="p-2 rounded bg-bg"><span className="text-text-muted">Temperature</span><p className="font-mono">{gpu.temperature ?? "—"}°C</p></div>
                  </div>
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
    </div>
  );
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
        <div className="p-2 rounded bg-bg"><p className="text-text-muted text-xs">Free</p><p className="font-mono">{(cpu.free ?? 0 / 1024).toFixed(1)} GB</p></div>
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
