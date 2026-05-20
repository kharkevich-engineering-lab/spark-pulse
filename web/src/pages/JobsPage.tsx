import { useEffect, useState, useRef } from "react";
import { fetchDeployments, stopDeployment, connectLogStream } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import StatusBadge from "@/components/StatusBadge";
import { ConfirmModal, AlertModal } from "@/components/Modal";
import { Square, Loader2, AlertCircle, Terminal } from "lucide-react";
import { setRefresh } from "@/lib/refresh";

export default function JobsPage() {
  const { data: deployments, loading, error, refetch } = useQuery(fetchDeployments);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [logs, setLogs] = useState<Record<string, string[]>>({});
  const [streaming, setStreaming] = useState<Record<string, boolean>>({});
  const logRef = useRef<Record<string, HTMLDivElement | null>>({});
  const stopRef = useRef<Record<string, () => void>>({});
  const [stopTarget, setStopTarget] = useState<{ id: string; name: string } | null>(null);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);

  useEffect(() => { setRefresh(refetch); }, [refetch]);
  useEffect(() => { const i = setInterval(refetch, 10000); return () => clearInterval(i); }, [refetch]);
  useEffect(() => { if (expandedId && logRef.current[expandedId]) logRef.current[expandedId]?.scrollTo({ top: 99999, behavior: "smooth" }); }, [logs[expandedId || ""]]);

  const toggle = async (id: string) => {
    if (expandedId === id) { stopRef.current[id]?.(); setStreaming((s) => ({ ...s, [id]: false })); setExpandedId(null); return; }
    setExpandedId(id);
    setStreaming((s) => ({ ...s, [id]: true }));
    stopRef.current[id] = connectLogStream(id, (event, data) => {
      if (event === "log") setLogs((l) => ({ ...l, [id]: [...(l[id] || []), (data as { text: string }).text] }));
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
        <h2 className="text-2xl font-bold">Jobs</h2>
        <p className="text-text-muted mt-1">Running and recently stopped deployments</p>
      </div>

      {loading && <div className="flex justify-center py-20"><Loader2 className="animate-spin text-primary" size={32} /></div>}
      {error && <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3"><AlertCircle size={20} /><span>{error}</span></div>}

      {deployments && deployments.length > 0 && (
        <div className="space-y-2">
          {deployments.map((dep) => (
            <div key={dep.id} className="rounded-xl bg-surface border border-border overflow-hidden">
              <div className="flex items-center gap-4 p-4 cursor-pointer hover:bg-surface-hover" onClick={() => toggle(dep.id)}>
                <div className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: dep.status === "running" ? "var(--color-success)" : dep.status === "error" ? "var(--color-danger)" : dep.status === "pending" ? "var(--color-warning)" : "var(--color-text-muted)" }} />
                <div className="flex-1 min-w-0"><p className="font-medium truncate">{dep.name}</p><p className="text-xs text-text-muted">{dep.recipe_id}</p></div>
                {dep.port && <span className="text-sm font-mono text-text-muted shrink-0">:{dep.port}</span>}
                <StatusBadge status={dep.status} />
                {dep.pid && <span className="text-xs font-mono text-text-muted shrink-0">PID: {dep.pid}</span>}
                <span className="text-xs text-text-muted shrink-0">{new Date(dep.created_at).toLocaleString()}</span>
                <button onClick={(e) => { e.stopPropagation(); setStopTarget({ id: dep.id, name: dep.name }); }} disabled={dep.status !== "running"} className="p-2 rounded-lg hover:bg-danger/10 text-text-muted hover:text-danger transition-colors disabled:opacity-30 shrink-0" title="Stop"><Square size={14} /></button>
              </div>
              {expandedId === dep.id && (
                <div className="border-t border-border">
                  <div className="flex items-center gap-2 px-4 py-2 bg-bg text-xs text-text-muted">
                    {streaming[dep.id] ? <span className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />Streaming</span> : <span>Stream stopped</span>}
                    <button onClick={() => toggle(dep.id)} className="ml-auto text-primary hover:underline">Hide</button>
                  </div>
                  <div ref={(el) => { logRef.current[dep.id] = el; }} className="p-4 bg-black font-mono text-sm text-green-400 h-64 overflow-auto whitespace-pre-wrap">
                    {(logs[dep.id] || ["No logs yet..."]).map((line, i) => <div key={i} className="leading-relaxed">{line}</div>)}
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
          title="Stop Deployment"
          message={`Stop "${stopTarget.name}"? This will terminate the running process.`}
          confirmLabel="Stop"
          confirmVariant="danger"
        />
      )}

      {/* Alert modal */}
      {alertModal && (
        <AlertModal open={!!alertModal} onClose={() => setAlertModal(null)} title={alertModal.title} message={alertModal.message} />
      )}
    </div>
  );
}
