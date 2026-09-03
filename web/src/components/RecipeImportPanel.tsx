import { useEffect, useState } from "react";
import { Download, Loader2, ChevronDown, AlertCircle, Check, MinusCircle } from "lucide-react";
import { importRecipes, fetchRecipeImportStatus } from "@/lib/api";
import type { RecipeImportResult, RecipeImportStatusKind } from "@/lib/types";

const STATUS_STYLE: Record<RecipeImportStatusKind, string> = {
  ok: "text-success",
  skipped: "text-text-muted",
  error: "text-danger",
};

function StatusIcon({ status }: { status: RecipeImportStatusKind }) {
  if (status === "ok") return <Check size={13} className="text-success shrink-0" />;
  if (status === "skipped") return <MinusCircle size={13} className="text-text-muted shrink-0" />;
  return <AlertCircle size={13} className="text-danger shrink-0" />;
}

/**
 * Pull recipes and mods out of a spark-vllm-docker checkout (local path or git
 * URL) into Spark Pulse's own config dir. Imported recipes appear in the list
 * with an `imported/` id prefix.
 */
export default function RecipeImportPanel({ onImported }: { onImported?: () => void }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"path" | "url">("path");
  const [path, setPath] = useState("");
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RecipeImportResult | null>(null);
  const [lastImportedAt, setLastImportedAt] = useState<string | null>(null);

  useEffect(() => {
    fetchRecipeImportStatus()
      .then((status) => setLastImportedAt(status.imported ? status.imported_at : null))
      .catch(() => setLastImportedAt(null));
  }, []);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const body = mode === "path" ? { path: path.trim() } : { url: url.trim(), ref: ref.trim() || undefined };
      const res = await importRecipes(body);
      setResult(res);
      setLastImportedAt(res.imported_at);
      onImported?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      setBusy(false);
    }
  };

  const canSubmit = !busy && (mode === "path" ? path.trim().length > 0 : url.trim().length > 0);

  return (
    <div className="rounded-lg border border-border bg-card">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="w-full flex items-center justify-between px-4 py-3 text-sm font-medium hover:bg-tag-bg/50 transition-colors"
      >
        <span className="flex items-center gap-2">
          <Download size={15} className="text-primary" />
          Import from upstream
          {lastImportedAt && (
            <span className="text-xs text-text-muted font-normal">last imported {lastImportedAt.slice(0, 10)}</span>
          )}
        </span>
        <ChevronDown size={16} className={`transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <div className="px-4 pb-4 space-y-3 border-t border-border pt-3">
          <p className="text-xs text-text-muted">
            Copies <code className="font-mono">recipes/</code> and <code className="font-mono">mods/</code> out of a
            spark-vllm-docker checkout. Recipes are validated on the way in and keep their original format.
          </p>

          <div className="flex items-center gap-2">
            {(["path", "url"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`px-3 py-1 rounded-md text-xs font-medium border transition-colors ${
                  mode === m ? "border-primary text-primary bg-primary/10" : "border-border text-text-muted hover:text-text"
                }`}
              >
                {m === "path" ? "Local path" : "Git URL"}
              </button>
            ))}
          </div>

          {mode === "path" ? (
            <input
              type="text"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="/home/user/spark-vllm-docker"
              aria-label="Local checkout path"
              className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm"
            />
          ) : (
            <div className="flex gap-2">
              <input
                type="text"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://github.com/eugr/spark-vllm-docker"
                aria-label="Git repository URL"
                className="flex-1 px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm"
              />
              <input
                type="text"
                value={ref}
                onChange={(e) => setRef(e.target.value)}
                placeholder="branch or tag"
                aria-label="Git ref"
                className="w-40 px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm"
              />
            </div>
          )}

          <button
            onClick={submit}
            disabled={!canSubmit}
            className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium flex items-center gap-1.5 transition-colors"
          >
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
            Import
          </button>

          {error && (
            <div className="p-3 rounded-lg bg-danger/10 border border-danger/30 text-danger text-sm flex items-start gap-2">
              <AlertCircle size={16} className="shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          {result && (
            <div className="space-y-2">
              <p className="text-sm">
                {result.counts.recipes.ok} recipe{result.counts.recipes.ok === 1 ? "" : "s"} imported,{" "}
                {result.counts.recipes.error} failed, {result.counts.mods.ok} mod
                {result.counts.mods.ok === 1 ? "" : "s"} imported.
              </p>
              <ul className="space-y-1 max-h-64 overflow-y-auto">
                {result.recipes.map((entry) => (
                  <li key={entry.file} className="flex items-start gap-2 text-xs font-mono">
                    <StatusIcon status={entry.status} />
                    <span className={STATUS_STYLE[entry.status]}>{entry.file}</span>
                    {entry.message && <span className="text-text-muted">— {entry.message}</span>}
                  </li>
                ))}
                {result.mods.map((entry) => (
                  <li key={`mod-${entry.name}`} className="flex items-start gap-2 text-xs font-mono">
                    <StatusIcon status={entry.status} />
                    <span className={STATUS_STYLE[entry.status]}>mods/{entry.name}</span>
                    {entry.message && <span className="text-text-muted">— {entry.message}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
