import { useState, useEffect } from "react";
import { fetchCache, cleanCache } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { formatSize } from "@/lib/utils";
import { Database, Trash2, Loader2, AlertCircle, FolderOpen, FileStack } from "lucide-react";
import { ConfirmModal, AlertModal } from "@/components/Modal";
import { setRefresh } from "@/lib/refresh";

export default function CachePage() {
  const { data: cacheData, loading, error, refetch } = useQuery(fetchCache);
  const [cleaning, setCleaning] = useState<string | null>(null);
  const [cleanTarget, setCleanTarget] = useState<string | null>(null);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);

  useEffect(() => { setRefresh(refetch); }, [refetch]);

  const doClean = async (name: string) => {
    setCleaning(name);
    try {
      await cleanCache([name]);
      refetch();
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed" });
    } finally {
      setCleaning(null);
    }
  };

  const totalSize = cacheData?.entries.reduce((s, e) => s + e.size_bytes, 0) ?? 0;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">Cache Manager</h2>
        <p className="text-text-muted mt-1">Browse and clean cached models, wheels, and artifacts</p>
      </div>

      {cacheData && (
        <div className="p-4 rounded-xl bg-surface border border-border flex items-center justify-between">
          <div className="flex items-center gap-3"><Database size={20} className="text-primary" /><div><p className="font-medium">Total cache</p><p className="text-2xl font-bold">{formatSize(totalSize)}</p></div></div>
          <button onClick={() => setCleanTarget("all")} disabled={!!cleaning} className="px-4 py-2 rounded-lg bg-danger/10 text-danger border border-danger/30 hover:bg-danger/20 disabled:opacity-50 transition-colors flex items-center gap-2">
            {cleaning === "all" ? <Loader2 className="animate-spin" size={16} /> : <Trash2 size={16} />}Clean All
          </button>
        </div>
      )}

      {loading && <div className="flex justify-center py-20"><Loader2 className="animate-spin text-primary" size={32} /></div>}
      {error && <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3"><AlertCircle size={20} /><span>{error}</span></div>}

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {cacheData?.entries.map((e) => (
          <div key={e.name} className="p-5 rounded-xl bg-surface border border-border hover:border-border-hover transition-colors group">
            <div className="flex items-start justify-between mb-3">
              <div className="flex items-start gap-2.5 min-w-0 flex-1">
                <FolderOpen size={18} className="text-text-muted mt-0.5 shrink-0" />
                <div className="min-w-0">
                  <h3 className="font-semibold text-base group-hover:text-primary transition-colors">{e.name}</h3>
                  <p className="text-xs text-text-muted mt-0.5 font-mono truncate">{e.path}</p>
                </div>
              </div>
              <button
                onClick={() => setCleanTarget(e.name)}
                disabled={cleaning === e.name}
                className="p-1.5 rounded-lg text-text-muted hover:text-danger hover:bg-danger/10 transition-colors disabled:opacity-50 shrink-0 ml-2"
                title="Clean cache"
              >
                {cleaning === e.name ? <Loader2 className="animate-spin" size={15} /> : <Trash2 size={15} />}
              </button>
            </div>

            {e.description && <p className="text-sm text-text-muted mb-4 line-clamp-2">{e.description}</p>}

            <div className="flex flex-wrap gap-2 mt-auto">
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-text-muted font-mono font-bold">{formatSize(e.size_bytes)}</span>
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-text-muted"><FileStack size={11} />{e.file_count} file{e.file_count !== 1 ? "s" : ""}</span>
            </div>
          </div>
        ))}
      </div>

      {cacheData && cacheData.entries.length === 0 && !loading && <div className="text-center py-20 text-text-muted"><Database size={40} className="mx-auto mb-4 opacity-50" /><p>No cache entries found.</p></div>}

      {cleanTarget && (
        <ConfirmModal
          open={!!cleanTarget}
          onClose={() => setCleanTarget(null)}
          onConfirm={() => { doClean(cleanTarget!); setCleanTarget(null); }}
          title={cleanTarget === "all" ? "Clean All Caches" : "Clean Cache"}
          message={cleanTarget === "all" ? "This will clean ALL caches. This cannot be undone." : `Clean cache "${cleanTarget}"? This cannot be undone.`}
          confirmLabel="Clean"
          confirmVariant="danger"
        />
      )}

      {alertModal && (
        <AlertModal
          open={!!alertModal}
          onClose={() => setAlertModal(null)}
          title={alertModal.title}
          message={alertModal.message}
        />
      )}
    </div>
  );
}
