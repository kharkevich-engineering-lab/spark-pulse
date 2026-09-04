import { useEffect, useState, useRef, useCallback } from "react";
import { fetchDeployments, stopDeployment, connectLogStream, runBenchmark } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { useSSEConnection } from "@/hooks/useSSEConnection";
import StatusBadge from "@/components/StatusBadge";
import HealthBadge from "@/components/HealthBadge";
import EventStreamViewer from "@/components/EventStreamViewer";
import { ConfirmModal, AlertModal } from "@/components/Modal";
import { Square, X, Trash2, Loader2, AlertCircle, Terminal, Flame } from "lucide-react";
import { setRefresh } from "@/lib/refresh";
import type { DeploymentEvent } from "@/lib/operations";

export default function InferencePage() {
  const { data: deployments, loading, error, refetch } = useQuery(fetchDeployments);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [logs, setLogs] = useState<Record<string, string[]>>({});
  const [streaming, setStreaming] = useState<Record<string, boolean>>({});
  const logRef = useRef<Record<string, HTMLDivElement | null>>({});
  const stopRef = useRef<Record<string, () => void>>({});
  const atBottomRef = useRef<Record<string, boolean>>({});
  const [stopTarget, setStopTarget] = useState<{ id: string; name: string } | null>(null);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);
  const [benchmarkModal, setBenchmarkModal] = useState<{ id: string; name: string; recipeId: string; recipeName: string } | null>(null);
  const [isBenchmarking, setIsBenchmarking] = useState(false);

  // SSE connection for deployment events
  const [deploymentEvents, setDeploymentEvents] = useState<DeploymentEvent[]>([]);
  const handleDeploymentEvent = useCallback((_event: string, data: unknown) => {
    if (data && typeof data === "object" && "type" in data) {
      const evt = data as Record<string, unknown>;
      setDeploymentEvents((prev) => [
        {
          event_id: (evt.event_id as string) || crypto.randomUUID(),
          timestamp: (evt.timestamp as string) || new Date().toISOString(),
          event_type: (evt.type as any) || "unknown",
          message: (evt.message as string) || "",
          resource: (evt.resource as string) || "",
          resource_type: (evt.resource_type as any) || "deployment",
          node: evt.node as string | undefined,
        },
        ...prev,
      ].slice(0, 100));
    }
  }, []);
  useSSEConnection("/sse/deployments", handleDeploymentEvent);

  const handleBenchmark = async () => {
    if (!benchmarkModal) return;
    setIsBenchmarking(true);
    try {
      await runBenchmark({
        deployment_id: benchmarkModal.id,
        recipe_id: benchmarkModal.recipeId,
        recipe_name: benchmarkModal.recipeName,
        params: { benchmarks: ["throughput", "latency"] },
      });
      setBenchmarkModal(null);
      refetch();
    } catch (e) {
      setAlertModal({
        title: "Error",
        message: e instanceof Error ? e.message : "Failed to run benchmark",
      });
    } finally {
      setIsBenchmarking(false);
    }
  };

  useEffect(() => { setRefresh(refetch); }, [refetch]);
  useEffect(() => { const i = setInterval(refetch, 10000); return () => clearInterval(i); }, [refetch]);

  // Auto-scroll only if already pinned to the bottom
  useEffect(() => {
    if (!expandedId) return;
    const el = logRef.current[expandedId];
    if (el && atBottomRef.current[expandedId] !== false) {
      el.scrollTop = el.scrollHeight;
    }
  }, [logs[expandedId || ""]]);

  const handleLogScroll = (id: string) => {
    const el = logRef.current[id];
    if (el) atBottomRef.current[id] = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const toggle = async (id: string) => {
    if (expandedId === id) { stopRef.current[id]?.(); setStreaming((s) => ({ ...s, [id]: false })); setExpandedId(null); return; }
    setExpandedId(id);
    atBottomRef.current[id] = true; // start pinned to bottom
    setStreaming((s) => ({ ...s, [id]: true }));
    stopRef.current[id] = connectLogStream(id, (event, data) => {
      if (event === "log") {
        setLogs((l) => {
          const prev = l[id] || [];
          return { ...l, [id]: [...prev.slice(-499), (data as { text: string }).text] };
        });
      }
      else if (event === "status") refetch();
    });
  };

  const doStop = async (id: string, _name: string) => {
    try {
      await stopDeployment(id);
      stopRef.current[id]?.();
      setStreaming((s) => ({ ...s, [id]: false }));
      refetch();
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to stop deployment" });
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">Inference</h2>
        <p className="text-text-muted mt-1">Live model inference workloads and their logs</p>
      </div>

      {loading && <div className="flex justify-center py-20"><Loader2 className="animate-spin text-primary" size={32} /></div>}
      {error && <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3"><AlertCircle size={20} /><span>{error}</span></div>}

      {deployments && deployments.length > 0 && (
        <div className="space-y-2">
          {deployments.map((dep) => (
            <div key={dep.id} data-testid={`deployment-${dep.id}`} className="rounded-xl bg-surface border border-border overflow-hidden">
              <div className="flex items-center gap-4 p-4 cursor-pointer hover:bg-surface-hover" onClick={() => toggle(dep.id)}>
                <div className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: dep.status === "running" ? "var(--color-success)" : dep.status === "error" ? "var(--color-danger)" : dep.status === "pending" ? "var(--color-warning)" : "var(--color-text-muted)" }} />
                <div className="flex-1 min-w-0">
                  <p className="font-medium truncate">{dep.name}</p>
                  <p className="text-xs text-text-muted truncate">
                    {dep.recipe_id}
                    {dep.model ? ` · ${dep.model}` : ""}
                  </p>
                </div>
                {dep.runtime === "native" && (
                  <span
                    className="hidden md:inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-mono bg-primary/15 text-primary border border-primary/30 shrink-0"
                    title={dep.image_ref || undefined}
                  >
                    {dep.engine || "native"}
                  </span>
                )}
                {dep.port && <span className="text-sm font-mono text-text-muted shrink-0">:{dep.port}</span>}
                <HealthBadge status={dep.status === "running" ? "healthy" as any : dep.status === "error" ? "unhealthy" as any : "unknown" as any} size="sm" />
                <StatusBadge status={dep.status} />
                {dep.pid && <span className="text-xs font-mono text-text-muted shrink-0">PID: {dep.pid}</span>}
                <span className="text-xs text-text-muted shrink-0">{new Date(dep.created_at).toLocaleString()}</span>
                {["stopped", "error"].includes(dep.status)
                  ? <button onClick={(e) => { e.stopPropagation(); setStopTarget({ id: dep.id, name: dep.name }); }} className="p-2 rounded-lg hover:bg-danger/10 text-text-muted hover:text-danger transition-colors shrink-0" title="Remove from history"><Trash2 size={14} /></button>
                  : <button onClick={(e) => { e.stopPropagation(); setStopTarget({ id: dep.id, name: dep.name }); }} disabled={dep.status !== "running" && dep.status !== "pending"} className="p-2 rounded-lg hover:bg-danger/10 text-text-muted hover:text-danger transition-colors disabled:opacity-30 shrink-0" title={dep.status === "pending" ? "Cancel" : "Stop"}>{dep.status === "pending" ? <X size={14} /> : <Square size={14} />}</button>}
                {dep.status === "running" && (
                  <button onClick={(e) => { e.stopPropagation(); setBenchmarkModal({ id: dep.id, name: dep.name, recipeId: dep.recipe_id, recipeName: dep.recipe_id }); }} className="p-2 rounded-lg hover:bg-primary/10 text-text-muted hover:text-primary transition-colors shrink-0" title="Run Benchmark"><Flame size={14} /></button>
                )}
              </div>
              {expandedId === dep.id && (
                <div className="border-t border-border">
                  {dep.runtime === "native" && (
                    <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-1 px-4 py-3 bg-bg text-xs border-b border-border">
                      <dt className="text-text-muted">Engine</dt>
                      <dd className="font-mono truncate">{dep.engine}{dep.variant ? `/${dep.variant}` : ""}</dd>
                      <dt className="text-text-muted">Image</dt>
                      <dd className="font-mono truncate">{dep.image_ref}</dd>
                      <dt className="text-text-muted">Model</dt>
                      <dd className="font-mono truncate">{dep.model || "(from the command)"}</dd>
                      <dt className="text-text-muted">Container</dt>
                      <dd className="font-mono truncate">{dep.container_name}</dd>
                    </dl>
                  )}
                  <div className="flex items-center gap-2 px-4 py-2 bg-bg text-xs text-text-muted">
                    {streaming[dep.id] ? <span className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />Streaming</span> : <span>Stream stopped</span>}
                    <button onClick={() => toggle(dep.id)} className="ml-auto text-primary hover:underline">Hide</button>
                  </div>
                  <div ref={(el) => { logRef.current[dep.id] = el; }} onScroll={() => handleLogScroll(dep.id)} className="p-4 bg-bg font-mono text-sm text-text h-[calc(100vh-20rem)] overflow-auto whitespace-pre-wrap">
                    {(logs[dep.id] || ["No logs yet..."]).map((line, i) => <div key={i} className="leading-relaxed text-text-muted last:text-text">{line}</div>)}
                  </div>
                  {/* Event Stream Viewer */}
                  <div className="border-t border-border p-4">
                    <EventStreamViewer
                      events={deploymentEvents.filter(e => e.resource === dep.id)}
                      resource={dep.id}
                      onClear={() => setDeploymentEvents(prev => prev.filter(e => e.resource !== dep.id))}
                    />
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {deployments && deployments.length === 0 && !loading && !error && (
        <div className="text-center py-20 text-text-muted"><Terminal size={40} className="mx-auto mb-4 opacity-50" /><p>No deployments yet.</p><p className="text-sm mt-1">Launch a recipe from the Recipes page.</p></div>
      )}

      {/* Stop confirmation */}
      {stopTarget && (
        <ConfirmModal
          open={!!stopTarget}
          onClose={() => setStopTarget(null)}
          onConfirm={() => { doStop(stopTarget.id, stopTarget.name); setStopTarget(null); }}
          title={["stopped", "error"].includes(deployments?.find(d => d.id === stopTarget.id)?.status ?? "") ? "Remove" : deployments?.find(d => d.id === stopTarget.id)?.status === "pending" ? "Cancel" : "Stop Deployment"}
          message={["stopped", "error"].includes(deployments?.find(d => d.id === stopTarget.id)?.status ?? "") ? `Remove "${stopTarget.name}" from history?` : deployments?.find(d => d.id === stopTarget.id)?.status === "pending" ? `Cancel "${stopTarget.name}" before it starts?` : `Stop "${stopTarget.name}"? This will terminate the running process.`}
          confirmLabel={["stopped", "error"].includes(deployments?.find(d => d.id === stopTarget.id)?.status ?? "") ? "Remove" : deployments?.find(d => d.id === stopTarget.id)?.status === "pending" ? "Cancel" : "Stop"}
          confirmVariant="danger"
        />
      )}

      {/* Alert modal */}
      {alertModal && (
        <AlertModal open={!!alertModal} onClose={() => setAlertModal(null)} title={alertModal.title} message={alertModal.message} />
      )}

      {/* Benchmark confirmation */}
      {benchmarkModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="rounded-xl bg-surface border border-border w-full max-w-sm p-6 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-lg font-bold flex items-center gap-2">
                <Flame size={20} className="text-primary" />
                Run Benchmark
              </h3>
              <button onClick={() => setBenchmarkModal(null)} className="p-1 rounded hover:bg-surface-hover">
                <X size={18} />
              </button>
            </div>
            <p className="text-sm text-text-muted">
              Run a benchmark on <strong>{benchmarkModal.name}</strong>? This will measure throughput, latency, and memory usage.
            </p>
            <div className="flex justify-end gap-2 pt-2">
              <button
                onClick={() => setBenchmarkModal(null)}
                className="px-4 py-2 rounded-lg border border-border text-sm hover:bg-surface-hover transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleBenchmark}
                disabled={isBenchmarking}
                className="flex items-center gap-2 px-4 py-2 rounded-lg bg-primary hover:bg-primary-hover text-white text-sm font-medium transition-colors disabled:opacity-50"
              >
                {isBenchmarking && <Loader2 size={16} className="animate-spin" />}
                {isBenchmarking ? "Running..." : "Run"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
