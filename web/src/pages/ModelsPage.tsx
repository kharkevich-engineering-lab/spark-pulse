import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertCircle, Boxes, Download, HardDrive, Loader2, Plus, Save, Trash2, X } from "lucide-react";
import {
  cancelModelDownload,
  deleteModel,
  fetchModelDownloads,
  fetchModelSources,
  fetchModels,
  saveModelSources,
  startModelDownload,
} from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { useSSEConnection } from "@/hooks/useSSEConnection";
import { SSEConnectionState } from "@/lib/operations";
import { formatSize } from "@/lib/utils";
import { setRefresh } from "@/lib/refresh";
import { AlertModal, ConfirmModal } from "@/components/Modal";
import type { ModelDownloadJob, ModelEntry, ModelSource } from "@/lib/types";

const ACTIVE_STATES = ["queued", "running"];

/** Shape of a DeploymentEvent frame as emitted by /sse/models. */
interface ModelEventFrame {
  type?: string;
  resource_type?: string;
  metadata?: unknown;
}

export function shortRevision(revision: string | null): string {
  if (!revision) return "—";
  return revision.length > 10 ? revision.slice(0, 10) : revision;
}

export function describePrecision(model: ModelEntry): string {
  const cfg = model.config;
  if (!cfg) return "—";
  if (cfg.quantization_method) return cfg.quantization_method;
  if (cfg.quantization.length) return "quantized";
  return cfg.torch_dtype || "—";
}

function progressPercent(job: ModelDownloadJob): number {
  if (job.status === "completed") return 100;
  if (!job.bytes_total) return 0;
  return Math.min(100, Math.round((job.bytes_done / job.bytes_total) * 100));
}

// ── Sources editor ───────────────────────────────────────────────────────────

function SourcesEditor({ sources, onSaved, onError }: { sources: ModelSource[]; onSaved: () => void; onError: (m: string) => void }) {
  const [draft, setDraft] = useState<ModelSource[]>(sources);
  const [saving, setSaving] = useState(false);

  useEffect(() => { setDraft(sources); }, [sources]);

  const update = (i: number, patch: Partial<ModelSource>) =>
    setDraft((d) => d.map((s, idx) => (idx === i ? { ...s, ...patch } : s)));

  const save = async () => {
    setSaving(true);
    try {
      await saveModelSources(draft);
      onSaved();
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to save sources");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="p-5 rounded-xl bg-surface border border-border space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold flex items-center gap-2"><HardDrive size={16} className="text-primary" />Model sources</h3>
        <div className="flex gap-2">
          <button
            onClick={() => setDraft((d) => [...d, { name: "", type: "hf_hub", endpoint: "https://huggingface.co", token_secret: "" }])}
            className="px-3 py-1.5 rounded-lg border border-border hover:border-border-hover text-sm flex items-center gap-1.5"
          >
            <Plus size={14} />Add source
          </button>
          <button onClick={save} disabled={saving} className="px-3 py-1.5 rounded-lg bg-primary/10 text-primary border border-primary/30 hover:bg-primary/20 disabled:opacity-50 text-sm flex items-center gap-1.5">
            {saving ? <Loader2 className="animate-spin" size={14} /> : <Save size={14} />}Save
          </button>
        </div>
      </div>

      {draft.length === 0 && <p className="text-sm text-text-muted">No sources configured.</p>}

      <div className="space-y-2">
        {draft.map((s, i) => (
          <div key={i} className="grid grid-cols-1 md:grid-cols-[1fr_140px_1fr_1fr_auto] gap-2 items-center">
            <input aria-label={`Source ${i + 1} name`} value={s.name} onChange={(e) => update(i, { name: e.target.value })} placeholder="name" className="px-2 py-1.5 rounded-lg bg-bg border border-border text-sm" />
            <select aria-label={`Source ${i + 1} type`} value={s.type} onChange={(e) => update(i, { type: e.target.value as ModelSource["type"] })} className="px-2 py-1.5 rounded-lg bg-bg border border-border text-sm">
              <option value="hf_hub">hf_hub</option>
              <option value="local_path">local_path</option>
            </select>
            {s.type === "hf_hub" ? (
              <>
                <input aria-label={`Source ${i + 1} endpoint`} value={s.endpoint ?? ""} onChange={(e) => update(i, { endpoint: e.target.value })} placeholder="https://huggingface.co" className="px-2 py-1.5 rounded-lg bg-bg border border-border text-sm font-mono" />
                <input aria-label={`Source ${i + 1} token secret`} value={s.token_secret ?? ""} onChange={(e) => update(i, { token_secret: e.target.value })} placeholder="token secret key" className="px-2 py-1.5 rounded-lg bg-bg border border-border text-sm font-mono" />
              </>
            ) : (
              <>
                <input aria-label={`Source ${i + 1} path`} value={s.path ?? ""} onChange={(e) => update(i, { path: e.target.value })} placeholder="/models" className="px-2 py-1.5 rounded-lg bg-bg border border-border text-sm font-mono md:col-span-2" />
              </>
            )}
            <button aria-label={`Remove source ${i + 1}`} onClick={() => setDraft((d) => d.filter((_, idx) => idx !== i))} className="p-1.5 rounded-lg text-text-muted hover:text-danger hover:bg-danger/10">
              <X size={15} />
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function ModelsPage() {
  const { data: models, loading, error, refetch } = useQuery(fetchModels);
  const { data: sources, refetch: refetchSources } = useQuery(fetchModelSources);
  const [jobs, setJobs] = useState<ModelDownloadJob[]>([]);
  const [modelId, setModelId] = useState("");
  const [sourceName, setSourceName] = useState("");
  const [revision, setRevision] = useState("");
  const [starting, setStarting] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);

  useEffect(() => { setRefresh(refetch); }, [refetch]);

  const reloadJobs = useCallback(() => {
    fetchModelDownloads().then(setJobs).catch(() => { });
  }, []);

  useEffect(() => { reloadJobs(); }, [reloadJobs]);

  const onEvent = useCallback((_event: string, data: unknown) => {
    const frame = data as ModelEventFrame;
    if (frame?.resource_type && frame.resource_type !== "model") return;
    const job = frame?.metadata as ModelDownloadJob | undefined;
    if (!job?.id) return;
    setJobs((current) =>
      current.some((j) => j.id === job.id)
        ? current.map((j) => (j.id === job.id ? { ...j, ...job } : j))
        : [job, ...current],
    );
    if (frame.type === "model.download.completed") refetch();
  }, [refetch]);

  const sseStatus = useSSEConnection("/sse/models", onEvent);
  const connected = sseStatus.state === SSEConnectionState.CONNECTED;

  const active = useMemo(() => jobs.filter((j) => ACTIVE_STATES.includes(j.status)), [jobs]);
  const recent = useMemo(() => jobs.filter((j) => !ACTIVE_STATES.includes(j.status)).slice(0, 5), [jobs]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!modelId.trim()) return;
    setStarting(true);
    try {
      const job = await startModelDownload({
        model: modelId.trim(),
        source: sourceName || undefined,
        revision: revision.trim() || undefined,
      });
      setJobs((current) => [job, ...current]);
      setModelId("");
      setRevision("");
    } catch (err) {
      setAlert({ title: "Download failed", message: err instanceof Error ? err.message : "Unknown error" });
    } finally {
      setStarting(false);
    }
  };

  const doDelete = async (id: string) => {
    try {
      await deleteModel(id);
      refetch();
    } catch (err) {
      setAlert({ title: "Delete failed", message: err instanceof Error ? err.message : "Unknown error" });
    }
  };

  const doCancel = async (jobId: string) => {
    try {
      await cancelModelDownload(jobId);
      reloadJobs();
    } catch (err) {
      setAlert({ title: "Cancel failed", message: err instanceof Error ? err.message : "Unknown error" });
    }
  };

  const totalSize = models?.reduce((sum, m) => sum + m.size_bytes, 0) ?? 0;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-2xl font-bold">Models</h2>
          <p className="text-text-muted mt-1">
            Cached model snapshots, downloads and distribution — independent of recipes.{" "}
            <Link to="/cache" className="text-primary hover:underline">Cache manager</Link>
          </p>
        </div>
        <div className="text-right">
          <p className="text-xs text-text-muted uppercase tracking-wide">Total on disk</p>
          <p className="text-2xl font-bold">{formatSize(totalSize)}</p>
        </div>
      </div>

      {/* Download form */}
      <form onSubmit={submit} className="p-5 rounded-xl bg-surface border border-border space-y-3">
        <h3 className="font-semibold flex items-center gap-2"><Download size={16} className="text-primary" />Download model</h3>
        <div className="grid grid-cols-1 md:grid-cols-[2fr_1fr_1fr_auto] gap-2">
          <input
            aria-label="Model id"
            placeholder="org/model-name"
            value={modelId}
            onChange={(e) => setModelId(e.target.value)}
            className="px-3 py-2 rounded-lg bg-bg border border-border font-mono text-sm"
          />
          <select aria-label="Source" value={sourceName} onChange={(e) => setSourceName(e.target.value)} className="px-3 py-2 rounded-lg bg-bg border border-border text-sm">
            <option value="">Default source</option>
            {(sources ?? []).filter((s) => s.type === "hf_hub").map((s) => (
              <option key={s.name} value={s.name}>{s.name}</option>
            ))}
          </select>
          <input
            aria-label="Revision"
            placeholder="revision (optional)"
            value={revision}
            onChange={(e) => setRevision(e.target.value)}
            className="px-3 py-2 rounded-lg bg-bg border border-border font-mono text-sm"
          />
          <button type="submit" disabled={starting || !modelId.trim()} className="px-4 py-2 rounded-lg bg-primary/10 text-primary border border-primary/30 hover:bg-primary/20 disabled:opacity-50 flex items-center gap-2">
            {starting ? <Loader2 className="animate-spin" size={16} /> : <Download size={16} />}Download
          </button>
        </div>
      </form>

      {/* Active downloads */}
      <section className="space-y-2">
        <div className="flex items-center gap-2">
          <h3 className="font-semibold">Downloads</h3>
          <span className={connected ? "text-xs text-success" : "text-xs text-text-muted"}>
            {connected ? "live" : "polling"}
          </span>
        </div>
        {active.length === 0 && recent.length === 0 && <p className="text-sm text-text-muted">No downloads yet.</p>}
        {[...active, ...recent].map((job) => (
          <div key={job.id} data-testid={`job-${job.id}`} className="p-4 rounded-xl bg-surface border border-border">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="font-mono text-sm truncate">{job.model}</p>
                <p className="text-xs text-text-muted">
                  {job.status}
                  {job.current_file ? ` · ${job.current_file}` : ""}
                  {job.error ? ` · ${job.error}` : ""}
                </p>
              </div>
              <div className="flex items-center gap-3 shrink-0">
                <span className="text-xs font-mono text-text-muted">
                  {formatSize(job.bytes_done)} / {job.bytes_total ? formatSize(job.bytes_total) : "?"}
                </span>
                {ACTIVE_STATES.includes(job.status) && (
                  <button aria-label={`Cancel download of ${job.model}`} onClick={() => doCancel(job.id)} className="p-1.5 rounded-lg text-text-muted hover:text-danger hover:bg-danger/10">
                    <X size={15} />
                  </button>
                )}
              </div>
            </div>
            <div className="mt-2 h-1.5 rounded-full bg-tag-bg overflow-hidden">
              <div
                role="progressbar"
                aria-label={`${job.model} progress`}
                aria-valuenow={progressPercent(job)}
                className="h-full bg-primary transition-all"
                style={{ width: `${progressPercent(job)}%` }}
              />
            </div>
          </div>
        ))}
      </section>

      {/* Catalogue */}
      {loading && <div className="flex justify-center py-16"><Loader2 className="animate-spin text-primary" size={32} /></div>}
      {error && <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3"><AlertCircle size={20} /><span>{error}</span></div>}

      {models && models.length > 0 && (
        <div className="rounded-xl bg-surface border border-border overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-text-muted border-b border-border">
                <th className="p-3 font-medium">Model</th>
                <th className="p-3 font-medium">Size</th>
                <th className="p-3 font-medium">Revision</th>
                <th className="p-3 font-medium">Dtype / quant</th>
                <th className="p-3 font-medium">Recipes</th>
                <th className="p-3 font-medium sr-only">Actions</th>
              </tr>
            </thead>
            <tbody>
              {models.map((m) => (
                <tr key={m.id} className="border-b border-border last:border-0 hover:bg-surface-hover">
                  <td className="p-3 font-mono">{m.id}</td>
                  <td className="p-3 font-mono">{formatSize(m.size_bytes)}</td>
                  <td className="p-3 font-mono text-text-muted">{shortRevision(m.revision)}</td>
                  <td className="p-3">{describePrecision(m)}</td>
                  <td className="p-3">{m.referenced_by.length}</td>
                  <td className="p-3 text-right">
                    <button aria-label={`Delete ${m.id}`} onClick={() => setDeleteTarget(m.id)} className="p-1.5 rounded-lg text-text-muted hover:text-danger hover:bg-danger/10">
                      <Trash2 size={15} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {models && models.length === 0 && !loading && (
        <div className="text-center py-16 text-text-muted"><Boxes size={40} className="mx-auto mb-4 opacity-50" /><p>No models cached yet.</p></div>
      )}

      <SourcesEditor
        sources={sources ?? []}
        onSaved={refetchSources}
        onError={(message) => setAlert({ title: "Save failed", message })}
      />

      {deleteTarget && (
        <ConfirmModal
          open={!!deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onConfirm={() => { doDelete(deleteTarget); setDeleteTarget(null); }}
          title="Delete model"
          message={`Delete the cached snapshot for "${deleteTarget}"? This cannot be undone.`}
          confirmLabel="Delete"
          confirmVariant="danger"
        />
      )}

      {alert && (
        <AlertModal open={!!alert} onClose={() => setAlert(null)} title={alert.title} message={alert.message} />
      )}
    </div>
  );
}
