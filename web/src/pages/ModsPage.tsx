import { useState, useEffect } from "react";
import { fetchMods, fetchMod } from "@/lib/api";
import type { ModSummary, ModDetail } from "@/lib/types";
import { useQuery } from "@/hooks/useQuery";
import { Wrench, ChevronRight, Loader2, AlertCircle, X, Copy, Check, FileCode2, FileText, FileCode } from "lucide-react";
import { setRefresh } from "@/lib/refresh";

// ── File-kind badge colours ──────────────────────────────────────────────────

const KIND_STYLE: Record<string, string> = {
  patch: "bg-warning/15 text-warning border-warning/30",
  template: "bg-primary/15 text-primary border-primary/30",
  python: "bg-success/15 text-success border-success/30",
  script: "bg-tag-bg text-text-muted border-border",
  yaml: "bg-tag-bg text-text-muted border-border",
  file: "bg-tag-bg text-text-muted border-border",
};

const KIND_ICON: Record<string, React.ReactNode> = {
  patch: <FileCode2 size={12} />,
  template: <FileText size={12} />,
  python: <FileCode size={12} />,
  script: <FileCode size={12} />,
};

function FileBadge({ name, kind }: { name: string; kind: string }) {
  const cls = KIND_STYLE[kind] ?? KIND_STYLE.file;
  const icon = KIND_ICON[kind];
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-xs font-mono ${cls}`}>
      {icon}
      {name}
    </span>
  );
}

// ── Mod detail drawer ────────────────────────────────────────────────────────

function ModDrawer({ modId, onClose }: { modId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<ModDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchMod(modId)
      .then(setDetail)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [modId]);

  const copyScript = async () => {
    if (!detail?.script) return;
    await navigator.clipboard.writeText(detail.script);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end" onClick={onClose}>
      <div
        className="h-full w-full max-w-2xl bg-surface border-l border-border shadow-xl flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-border shrink-0">
          <div className="flex items-center gap-2">
            <Wrench size={20} className="text-primary" />
            <span className="font-mono font-semibold text-lg">{modId}</span>
          </div>
          <button onClick={onClose} className="p-1.5 rounded hover:bg-surface-hover transition-colors">
            <X size={18} />
          </button>
        </div>

        {loading && (
          <div className="flex-1 flex items-center justify-center">
            <Loader2 className="animate-spin text-primary" size={32} />
          </div>
        )}
        {error && (
          <div className="flex-1 flex items-center justify-center px-6">
            <div className="flex items-center gap-3 text-danger">
              <AlertCircle size={20} />
              <span>{error}</span>
            </div>
          </div>
        )}

        {detail && !loading && (
          <div className="flex-1 overflow-y-auto p-6 space-y-5">
            {/* Description */}
            {detail.description && (
              <div className="p-4 rounded-xl bg-bg border border-border">
                <p className="text-sm text-text-muted leading-relaxed">{detail.description}</p>
              </div>
            )}

            {/* Asset files */}
            {detail.files.length > 0 && (
              <div>
                <p className="text-sm font-medium mb-2 text-text-muted uppercase tracking-wide text-xs">Assets</p>
                <div className="flex flex-wrap gap-2">
                  {detail.files.map((f) => (
                    <FileBadge key={f.name} name={f.name} kind={f.kind} />
                  ))}
                </div>
              </div>
            )}

            {/* run.sh script */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <p className="text-sm font-medium">run.sh</p>
                <button
                  onClick={copyScript}
                  className="flex items-center gap-1.5 text-xs text-text-muted hover:text-text transition-colors"
                >
                  {copied ? <Check size={14} className="text-success" /> : <Copy size={14} />}
                  {copied ? "Copied" : "Copy"}
                </button>
              </div>
              <div className="rounded-xl bg-bg border border-border overflow-hidden">
                <pre className="p-4 text-xs font-mono overflow-x-auto leading-relaxed whitespace-pre">
                  {detail.script || "(empty)"}
                </pre>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Mod card ─────────────────────────────────────────────────────────────────

function ModCard({ mod, onClick }: { mod: ModSummary; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="w-full text-left p-5 rounded-xl bg-surface border border-border hover:border-primary/50 hover:bg-surface-hover transition-all group"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <Wrench size={16} className="text-primary shrink-0" />
            <span className="font-mono font-semibold text-sm">{mod.id}</span>
            {mod.has_patches && (
              <span className="px-1.5 py-0.5 rounded text-xs bg-warning/15 text-warning border border-warning/30 font-mono">
                patches
              </span>
            )}
          </div>
          {mod.description ? (
            <p className="text-sm text-text-muted leading-snug">{mod.description}</p>
          ) : (
            <p className="text-sm text-text-muted italic opacity-50">No description</p>
          )}
        </div>
        <ChevronRight size={16} className="text-text-muted group-hover:text-primary shrink-0 mt-0.5 transition-colors" />
      </div>

      {mod.files.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mt-3">
          {mod.files.map((f) => (
            <FileBadge key={f.name} name={f.name} kind={f.kind} />
          ))}
        </div>
      )}
    </button>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function ModsPage() {
  const { data: mods, loading, error, refetch } = useQuery(fetchMods);
  const [activeModId, setActiveModId] = useState<string | null>(null);

  useEffect(() => { setRefresh(refetch); }, [refetch]);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">Mods</h2>
        <p className="text-text-muted mt-1">
          spark-vllm-docker patches and modifications available on this host
        </p>
      </div>

      {loading && (
        <div className="flex justify-center py-20">
          <Loader2 className="animate-spin text-primary" size={32} />
        </div>
      )}
      {error && (
        <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3">
          <AlertCircle size={20} />
          <span>{error}</span>
        </div>
      )}

      {mods && mods.length === 0 && (
        <div className="py-20 text-center text-text-muted">
          <Wrench size={48} className="mx-auto mb-4 opacity-30" />
          <p className="text-lg font-medium">No mods found</p>
          <p className="text-sm mt-1 opacity-70">
            Make sure spark_vllm_path is configured correctly in Settings.
          </p>
        </div>
      )}

      {mods && mods.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-1 lg:grid-cols-2">
          {mods.map((mod) => (
            <ModCard key={mod.id} mod={mod} onClick={() => setActiveModId(mod.id)} />
          ))}
        </div>
      )}

      {activeModId && (
        <ModDrawer modId={activeModId} onClose={() => setActiveModId(null)} />
      )}
    </div>
  );
}
