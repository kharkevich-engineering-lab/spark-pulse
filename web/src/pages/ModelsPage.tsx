import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useI18n } from "@/lib/i18n";
import { Link } from "react-router-dom";
import { AlertCircle, Boxes, Download, HardDrive, Loader2, Plus, Rocket, Save, Trash2, X } from "lucide-react";
import {
  cancelModelDownload,
  cancelScheduledDeploy,
  deleteModel,
  fetchScheduledDeploys,
  fetchModelDownloads,
  fetchModelPresence,
  fetchModelSources,
  fetchModels,
  fetchNodes,
  saveModelSources,
  startModelDownload,
} from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { useSSEConnection } from "@/hooks/useSSEConnection";
import { SSEConnectionState } from "@/lib/operations";
import { formatSize } from "@/lib/utils";
import { AlertModal, Modal } from "@/components/Modal";
import type {
  ModelDownloadJob,
  ModelEntry,
  ModelPresence,
  ModelSource,
  ScheduledDeploy,
} from "@/lib/types";

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
  const { t } = useI18n();
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
      onError(e instanceof Error ? e.message : t("models.saveSourcesFailed"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="p-5 rounded-xl bg-surface border border-border space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold flex items-center gap-2"><HardDrive size={16} className="text-primary" />{t("models.sources")}</h3>
        <div className="flex gap-2">
          <button
            onClick={() => setDraft((d) => [...d, { name: "", type: "hf_hub", endpoint: "https://huggingface.co", token_secret: "" }])}
            className="px-3 py-1.5 rounded-lg border border-border hover:border-border-hover text-sm flex items-center gap-1.5"
          >
            <Plus size={14} />{t("models.addSource")}
          </button>
          <button onClick={save} disabled={saving} className="px-3 py-1.5 rounded-lg bg-primary/10 text-primary border border-primary/30 hover:bg-primary/20 disabled:opacity-50 text-sm flex items-center gap-1.5">
            {saving ? <Loader2 className="animate-spin" size={14} /> : <Save size={14} />}Save
          </button>
        </div>
      </div>

      {draft.length === 0 && <p className="text-sm text-text-muted">{t("models.noSources")}</p>}

      <div className="space-y-2">
        {draft.map((s, i) => (
          <div key={i} className="grid grid-cols-1 md:grid-cols-[1fr_140px_1fr_1fr_auto] gap-2 items-center">
            <input aria-label={t("models.sourceName", { n: i + 1 })} value={s.name} onChange={(e) => update(i, { name: e.target.value })} placeholder={t("models.namePlaceholder")} className="px-2 py-1.5 rounded-lg bg-bg border border-border text-sm" />
            <select aria-label={`Source ${i + 1} type`} value={s.type} onChange={(e) => update(i, { type: e.target.value as ModelSource["type"] })} className="px-2 py-1.5 rounded-lg bg-bg border border-border text-sm">
              <option value="hf_hub">hf_hub</option>
              <option value="local_path">local_path</option>
            </select>
            {s.type === "hf_hub" ? (
              <>
                <input aria-label={t("models.sourceEndpoint", { n: i + 1 })} value={s.endpoint ?? ""} onChange={(e) => update(i, { endpoint: e.target.value })} placeholder="https://huggingface.co" className="px-2 py-1.5 rounded-lg bg-bg border border-border text-sm font-mono" />
                <input aria-label={t("models.sourceToken", { n: i + 1 })} value={s.token_secret ?? ""} onChange={(e) => update(i, { token_secret: e.target.value })} placeholder={t("models.tokenPlaceholder")} className="px-2 py-1.5 rounded-lg bg-bg border border-border text-sm font-mono" />
              </>
            ) : (
              <>
                <input aria-label={t("models.sourcePath", { n: i + 1 })} value={s.path ?? ""} onChange={(e) => update(i, { path: e.target.value })} placeholder="/models" className="px-2 py-1.5 rounded-lg bg-bg border border-border text-sm font-mono md:col-span-2" />
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
  const { t } = useI18n();
  const { data: models, loading, error, refetch } = useQuery(fetchModels);
  const { data: sources, refetch: refetchSources } = useQuery(fetchModelSources);
  const [jobs, setJobs] = useState<ModelDownloadJob[]>([]);
  const [modelId, setModelId] = useState("");
  const [sourceName, setSourceName] = useState("");
  const [revision, setRevision] = useState("");
  const [starting, setStarting] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);
  const [scheduled, setScheduled] = useState<ScheduledDeploy[]>([]);
  // The other machines a copy could be sitting on. Deleting from this one and
  // calling the model gone is what left 26 GB on three Sparks.
  const { data: nodes } = useQuery(fetchNodes);
  const peers = useMemo(
    () => (nodes ?? []).filter((n) => !n.is_control_plane).map((n) => n.address),
    [nodes],
  );


  const reloadJobs = useCallback(() => {
    fetchModelDownloads().then(setJobs).catch(() => { });
  }, []);

  // What each download is *for*. A progress bar with no purpose attached is
  // the thing this feature exists to stop showing: an operator who left the
  // deploy page has no other way to know a deployment is waiting on these
  // bytes, or to call it off.
  const reloadScheduled = useCallback(() => {
    fetchScheduledDeploys().then(setScheduled).catch(() => { });
  }, []);

  useEffect(() => { reloadJobs(); reloadScheduled(); }, [reloadJobs, reloadScheduled]);

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
    // Any terminal state settles the deploys queued behind this download —
    // into "deploying", or into a failure that has to be shown.
    if (frame.type?.startsWith("model.download.") && !["queued", "running"].includes(job.status)) {
      reloadScheduled();
    }
  }, [refetch, reloadScheduled]);

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
      setAlert({ title: t("models.downloadFailed"), message: err instanceof Error ? err.message : t("models.unknownError") });
    } finally {
      setStarting(false);
    }
  };

  const doDelete = async (id: string, nodes: string[]) => {
    try {
      const result = await deleteModel(id, nodes);
      const removed = (result.nodes ?? []).filter((n) => n.removed).length;
      const refused = (result.nodes ?? []).filter((n) => n.error);
      if (refused.length > 0) {
        // Partly done is its own outcome. A page that says "deleted" while a
        // node still holds 26 GB is the reason this reports per node.
        setAlert({
          title: t("models.deleteFailed"),
          message: refused.map((n) => `${n.node || "this node"}: ${n.error}`).join("\n"),
        });
      } else if (nodes.length > 0) {
        setAlert({
          title: t("models.deleteTitle"),
          message: t("models.removedFrom", {
            count: removed,
            size: formatSize(result.freed_bytes),
          }),
        });
      }
      refetch();
    } catch (err) {
      setAlert({ title: t("models.deleteFailed"), message: err instanceof Error ? err.message : t("models.unknownError") });
    }
  };

  const doCancel = async (jobId: string) => {
    try {
      await cancelModelDownload(jobId);
      reloadJobs();
      reloadScheduled();
    } catch (err) {
      setAlert({ title: t("models.cancelFailed"), message: err instanceof Error ? err.message : t("models.unknownError") });
    }
  };

  const doCancelScheduled = async (entry: ScheduledDeploy) => {
    try {
      // The download goes too. Both wishes usually travel together, and the
      // server keeps the bytes anyway if another deploy is still waiting on
      // this same job.
      await cancelScheduledDeploy(entry.id);
      reloadScheduled();
      reloadJobs();
    } catch (err) {
      setAlert({ title: t("models.cancelFailed"), message: err instanceof Error ? err.message : t("models.unknownError") });
    }
  };

  /** The deploys attached to one download job.
   *
   *  Settled ones are shown too, and deliberately: a download that finished
   *  and a deployment that then failed to start is exactly the case an
   *  operator would otherwise wait on for ever, watching a completed progress
   *  bar. Only the ones they called off themselves are hidden — they already
   *  know. */
  const waitingOn = useCallback(
    (jobId: string) =>
      scheduled.filter((e) => e.download_job_id === jobId && e.status !== "cancelled"),
    [scheduled],
  );

  const totalSize = models?.reduce((sum, m) => sum + m.size_bytes, 0) ?? 0;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-2xl font-bold">{t("models.title")}</h2>
          <p className="text-text-muted mt-1">
            {t("models.subtitle")}{" "}
            <Link to="/cache" className="text-primary hover:underline">{t("models.cacheLink")}</Link>
          </p>
        </div>
        <div className="text-right">
          <p className="text-xs text-text-muted uppercase tracking-wide">{t("models.totalOnDisk")}</p>
          <p className="text-2xl font-bold">{formatSize(totalSize)}</p>
        </div>
      </div>

      {/* Download form */}
      <form onSubmit={submit} className="p-5 rounded-xl bg-surface border border-border space-y-3">
        <h3 className="font-semibold flex items-center gap-2"><Download size={16} className="text-primary" />{t("models.download")}</h3>
        <div className="grid grid-cols-1 md:grid-cols-[2fr_1fr_1fr_auto] gap-2">
          <input
            aria-label={t("models.modelId")}
            placeholder={t("models.modelIdPlaceholder")}
            value={modelId}
            onChange={(e) => setModelId(e.target.value)}
            className="px-3 py-2 rounded-lg bg-bg border border-border font-mono text-sm"
          />
          <select aria-label={t("models.source")} value={sourceName} onChange={(e) => setSourceName(e.target.value)} className="px-3 py-2 rounded-lg bg-bg border border-border text-sm">
            <option value="">{t("models.defaultSource")}</option>
            {(sources ?? []).filter((s) => s.type === "hf_hub").map((s) => (
              <option key={s.name} value={s.name}>{s.name}</option>
            ))}
          </select>
          <input
            aria-label={t("models.revision")}
            placeholder={t("models.revisionPlaceholder")}
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
          <h3 className="font-semibold">{t("models.downloads")}</h3>
          <span className={connected ? "text-xs text-success" : "text-xs text-text-muted"}>
            {connected ? t("models.live") : t("models.polling")}
          </span>
        </div>
        {active.length === 0 && recent.length === 0 && <p className="text-sm text-text-muted">{t("models.noDownloads")}</p>}
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
                  <button aria-label={t("models.cancelDownload", { model: job.model })} onClick={() => doCancel(job.id)} className="p-1.5 rounded-lg text-text-muted hover:text-danger hover:bg-danger/10">
                    <X size={15} />
                  </button>
                )}
              </div>
            </div>
            <div className="mt-2 h-1.5 rounded-full bg-tag-bg overflow-hidden">
              <div
                role="progressbar"
                aria-label={t("models.progress", { model: job.model })}
                aria-valuenow={progressPercent(job)}
                className="h-full bg-primary transition-all"
                style={{ width: `${progressPercent(job)}%` }}
              />
            </div>
            {waitingOn(job.id).map((entry) => (
              <div
                key={entry.id}
                data-testid={`scheduled-${entry.id}`}
                className={`mt-2 flex items-center justify-between gap-3 px-3 py-2 rounded-lg border ${entry.status === "failed" ? "bg-danger/5 border-danger/20" : "bg-primary/5 border-primary/20"}`}
              >
                <p className="text-xs text-text-secondary flex items-center gap-2 min-w-0">
                  <Rocket size={13} className={entry.status === "failed" ? "text-danger shrink-0" : "text-primary shrink-0"} />
                  <span className="truncate">
                    {entry.status === "waiting" && <>{t("models.scheduledTo")} <span className="font-medium text-text">{entry.name}</span> {t("models.whenFinishes")}</>}
                    {entry.status === "deploying" && <>{t("models.deployingNow")} <span className="font-medium text-text">{entry.name}</span> {t("models.now")}</>}
                    {entry.status === "done" && <>{t("models.deployed")} <span className="font-medium text-text">{entry.name}</span></>}
                    {entry.status === "failed" && <><span className="font-medium text-text">{entry.name}</span> {t("models.couldNotDeploy")} {entry.error}</>}
                  </span>
                </p>
                {entry.status === "waiting" && (
                  <button
                    aria-label={t("models.cancelScheduled", { name: entry.name })}
                    onClick={() => doCancelScheduled(entry)}
                    className="p-1 rounded text-text-muted hover:text-danger hover:bg-danger/10 shrink-0"
                  >
                    <X size={14} />
                  </button>
                )}
              </div>
            ))}
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
                <th className="p-3 font-medium">{t("models.colModel")}</th>
                <th className="p-3 font-medium">{t("models.colSize")}</th>
                <th className="p-3 font-medium">{t("models.colRevision")}</th>
                <th className="p-3 font-medium">{t("models.colDtype")}</th>
                <th className="p-3 font-medium">{t("models.colRecipes")}</th>
                <th className="p-3 font-medium sr-only">{t("models.colActions")}</th>
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
                    <button aria-label={t("models.deleteModel", { model: m.id })} onClick={() => setDeleteTarget(m.id)} className="p-1.5 rounded-lg text-text-muted hover:text-danger hover:bg-danger/10">
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
        <div className="text-center py-16 text-text-muted"><Boxes size={40} className="mx-auto mb-4 opacity-50" /><p>{t("models.empty")}</p></div>
      )}

      <SourcesEditor
        sources={sources ?? []}
        onSaved={refetchSources}
        onError={(message) => setAlert({ title: "Save failed", message })}
      />

      {deleteTarget && (
        <ModelDeleteDialog
          model={deleteTarget}
          peers={peers}
          onClose={() => setDeleteTarget(null)}
          onConfirm={(onNodes) => {
            const id = deleteTarget;
            setDeleteTarget(null);
            doDelete(id, onNodes);
          }}
        />
      )}

      {alert && (
        <AlertModal open={!!alert} onClose={() => setAlert(null)} title={alert.title} message={alert.message} />
      )}
    </div>
  );
}

/** Which machines lose the model.
 *
 * A model replicated to four Sparks is on four disks. The old dialog deleted
 * it from this one and said it was gone, which is how a cluster fills up with
 * copies nobody can see. Presence is asked as the dialog opens so the nodes
 * that actually hold it are the ones preselected — a node without a copy has
 * nothing to reclaim.
 */
export function ModelDeleteDialog({
  model,
  peers,
  onClose,
  onConfirm,
}: {
  model: string;
  peers: string[];
  onClose: () => void;
  onConfirm: (nodes: string[]) => void;
}) {
  const { t } = useI18n();
  const [presence, setPresence] = useState<ModelPresence | "loading" | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  // A ref, not state: whether the operator has touched the boxes must not
  // re-run the presence query, and reading it inside the effect is exactly
  // what a ref is for.
  const touched = useRef(false);

  useEffect(() => {
    if (peers.length === 0) return;
    let live = true;
    setPresence("loading");
    fetchModelPresence(model, peers)
      .then((answer) => {
        if (!live) return;
        setPresence(answer);
        // Only preselect what the operator has not already changed.
        setSelected((current) =>
          touched.current
            ? current
            : answer.nodes.filter((n) => n.present).map((n) => n.node),
        );
      })
      .catch(() => live && setPresence(null));
    return () => {
      live = false;
    };
  }, [model, peers]);

  const holders = useMemo(() => {
    if (!presence || presence === "loading") return [];
    return presence.nodes.filter((n) => n.present).map((n) => n.node);
  }, [presence]);

  return (
    <Modal open onClose={onClose} title={t("models.deleteTitle")}>
      <div className="space-y-4">
        <p className="text-sm text-text-muted">{t("models.deleteBody", { model })}</p>

        {peers.length > 0 && (
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium mb-1">{t("models.alsoRemoveFrom")}</legend>
            {presence === "loading" && (
              <p className="text-xs text-text-muted flex items-center gap-2">
                <Loader2 size={12} className="animate-spin" />
                {t("common.loading")}
              </p>
            )}
            {peers.map((node) => (
              <label key={node} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={selected.includes(node)}
                  onChange={(e) => {
                    touched.current = true;
                    setSelected((current) =>
                      e.target.checked ? [...current, node] : current.filter((n) => n !== node),
                    );
                  }}
                />
                <span className="font-mono">{node}</span>
                {presence && presence !== "loading" && !holders.includes(node) && (
                  <span className="text-xs text-text-muted">{t("models.notThere")}</span>
                )}
              </label>
            ))}
          </fieldset>
        )}

        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="px-4 py-2 rounded-lg border border-border text-sm">
            {t("common.cancel")}
          </button>
          <button
            onClick={() => onConfirm(selected)}
            className="px-4 py-2 rounded-lg bg-danger/10 text-danger border border-danger/30 hover:bg-danger/20 text-sm"
          >
            {selected.length > 0 ? t("models.deleteConfirm") : t("common.delete")}
          </button>
        </div>
      </div>
    </Modal>
  );
}
