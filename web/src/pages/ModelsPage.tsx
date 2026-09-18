import { useCallback, useEffect, useMemo, useState } from "react";
import { useI18n } from "@/lib/i18n";
import { Link } from "react-router-dom";
import { Boxes, Download, HardDrive, Plus, Rocket, Save, Server, Trash2, X } from "lucide-react";
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
  syncModelToNodes,
} from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { useSSEConnection } from "@/hooks/useSSEConnection";
import { SSEConnectionState } from "@/lib/operations";
import { formatSize } from "@/lib/utils";
import {
  ACTIVE_STATES,
  AlertModal,
  Button,
  EmptyState,
  ErrorLine,
  IconButton,
  Input,
  NodeScopedDialog,
  ProgressRow,
  Select,
  Spinner,
} from "@/ui";
import type {
  ModelDownloadJob,
  ModelEntry,
  ModelSource,
  ModelSyncResult,
  ScheduledDeploy,
} from "@/lib/types";

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
    <section className="p-5 rounded-md bg-surface border border-line space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold flex items-center gap-2">
          <HardDrive size={16} className="text-blue2" />
          {t("models.sources")}
        </h3>
        <div className="flex gap-2">
          <Button
            size="sm"
            icon={Plus}
            onClick={() =>
              setDraft((d) => [
                ...d,
                { name: "", type: "hf_hub", endpoint: "https://huggingface.co", token_secret: "" },
              ])
            }
          >
            {t("models.addSource")}
          </Button>
          <Button size="sm" variant="primary" icon={Save} loading={saving} onClick={save}>
            {t("common.save")}
          </Button>
        </div>
      </div>

      {draft.length === 0 && <p className="text-[14px] text-muted">{t("models.noSources")}</p>}

      <div className="space-y-2">
        {draft.map((s, i) => (
          <div key={i} className="grid grid-cols-1 md:grid-cols-[1fr_140px_1fr_1fr_auto] gap-2 items-center">
            <Input aria-label={t("models.sourceName", { n: i + 1 })} value={s.name} onChange={(e) => update(i, { name: e.target.value })} placeholder={t("models.namePlaceholder")} />
            <Select aria-label={t("models.sourceType", { n: i + 1 })} value={s.type} onChange={(e) => update(i, { type: e.target.value as ModelSource["type"] })}>
              <option value="hf_hub">hf_hub</option>
              <option value="local_path">local_path</option>
            </Select>
            {s.type === "hf_hub" ? (
              <>
                <Input mono aria-label={t("models.sourceEndpoint", { n: i + 1 })} value={s.endpoint ?? ""} onChange={(e) => update(i, { endpoint: e.target.value })} placeholder="https://huggingface.co" />
                <Input mono aria-label={t("models.sourceToken", { n: i + 1 })} value={s.token_secret ?? ""} onChange={(e) => update(i, { token_secret: e.target.value })} placeholder={t("models.tokenPlaceholder")} />
              </>
            ) : (
              <Input mono aria-label={t("models.sourcePath", { n: i + 1 })} value={s.path ?? ""} onChange={(e) => update(i, { path: e.target.value })} placeholder="/models" className="md:col-span-2" />
            )}
            <IconButton
              size="sm"
              icon={X}
              label={`Remove source ${i + 1}`}
              onClick={() => setDraft((d) => d.filter((_, idx) => idx !== i))}
              className="border-transparent text-muted hover:text-bad hover:border-line"
            />
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
  const [replicateTarget, setReplicateTarget] = useState<string | null>(null);
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
            <Link to="/cache" className="text-blue2 hover:underline">{t("models.cacheLink")}</Link>
          </p>
        </div>
        <div className="text-right">
          <p className="text-xs text-text-muted uppercase tracking-wide">{t("models.totalOnDisk")}</p>
          <p className="text-2xl font-bold">{formatSize(totalSize)}</p>
        </div>
      </div>

      {/* Download form */}
      <form onSubmit={submit} className="p-5 rounded-md bg-surface border border-line space-y-3">
        <h3 className="font-semibold flex items-center gap-2"><Download size={16} className="text-blue2" />{t("models.download")}</h3>
        <div className="grid grid-cols-1 md:grid-cols-[2fr_1fr_1fr_auto] gap-2">
          <Input
            mono
            aria-label={t("models.modelId")}
            placeholder={t("models.modelIdPlaceholder")}
            value={modelId}
            onChange={(e) => setModelId(e.target.value)}
          />
          <Select aria-label={t("models.source")} value={sourceName} onChange={(e) => setSourceName(e.target.value)}>
            <option value="">{t("models.defaultSource")}</option>
            {(sources ?? []).filter((s) => s.type === "hf_hub").map((s) => (
              <option key={s.name} value={s.name}>{s.name}</option>
            ))}
          </Select>
          <Input
            mono
            aria-label={t("models.revision")}
            placeholder={t("models.revisionPlaceholder")}
            value={revision}
            onChange={(e) => setRevision(e.target.value)}
          />
          <Button
            type="submit"
            variant="primary"
            icon={Download}
            loading={starting}
            disabled={!modelId.trim()}
          >
            Download
          </Button>
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
          <ProgressRow
            key={job.id}
            data-testid={`job-${job.id}`}
            title={job.model}
            detail={`${job.status}${job.current_file ? ` · ${job.current_file}` : ""}${job.error ? ` · ${job.error}` : ""}`}
            job={job}
            progressLabel={t("models.progress", { model: job.model })}
            cancelLabel={t("models.cancelDownload", { model: job.model })}
            onCancel={ACTIVE_STATES.includes(job.status) ? () => doCancel(job.id) : undefined}
          >
            {waitingOn(job.id).map((entry) => (
              <div
                key={entry.id}
                data-testid={`scheduled-${entry.id}`}
                className={`mt-2 flex items-center justify-between gap-3 px-3 py-2 rounded-sm border ${entry.status === "failed" ? "bg-danger/5 border-danger/20" : "bg-primary/5 border-primary/20"}`}
              >
                <p className="text-xs text-text-secondary flex items-center gap-2 min-w-0">
                  <Rocket size={13} className={entry.status === "failed" ? "text-danger shrink-0" : "text-blue2 shrink-0"} />
                  <span className="truncate">
                    {entry.status === "waiting" && <>{t("models.scheduledTo")} <span className="font-medium text-text">{entry.name}</span> {t("models.whenFinishes")}</>}
                    {entry.status === "deploying" && <>{t("models.deployingNow")} <span className="font-medium text-text">{entry.name}</span> {t("models.now")}</>}
                    {entry.status === "done" && <>{t("models.deployed")} <span className="font-medium text-text">{entry.name}</span></>}
                    {entry.status === "failed" && <><span className="font-medium text-text">{entry.name}</span> {t("models.couldNotDeploy")} {entry.error}</>}
                  </span>
                </p>
                {entry.status === "waiting" && (
                  <IconButton
                    size="sm"
                    icon={X}
                    label={t("models.cancelScheduled", { name: entry.name })}
                    onClick={() => doCancelScheduled(entry)}
                    className="border-transparent text-muted hover:text-bad hover:border-line"
                  />
                )}
              </div>
            ))}
          </ProgressRow>
        ))}
      </section>

      {/* Catalogue */}
      {loading && (
        <div className="flex justify-center py-16">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      )}
      <ErrorLine>{error}</ErrorLine>

      {models && models.length > 0 && (
        <div className="rounded-md bg-surface border border-line overflow-x-auto">
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
                  <td className="p-3 text-right whitespace-nowrap">
                    {peers.length > 0 && (
                      <IconButton
                        size="sm"
                        icon={Server}
                        label={t("models.replicateModel", { model: m.id })}
                        onClick={() => setReplicateTarget(m.id)}
                        className="border-transparent text-muted hover:text-blue2 hover:border-line"
                      />
                    )}
                    <IconButton
                      size="sm"
                      icon={Trash2}
                      label={t("models.deleteModel", { model: m.id })}
                      onClick={() => setDeleteTarget(m.id)}
                      className="border-transparent text-muted hover:text-bad hover:border-line"
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {models && models.length === 0 && !loading && (
        <EmptyState icon={Boxes}>{t("models.empty")}</EmptyState>
      )}

      <SourcesEditor
        sources={sources ?? []}
        onSaved={refetchSources}
        onError={(message) => setAlert({ title: t("models.saveFailed"), message })}
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

      {replicateTarget && (
        <ModelReplicateDialog
          model={replicateTarget}
          peers={peers}
          onClose={() => setReplicateTarget(null)}
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
 * A model replicated to four Sparks is on four disks. Deleting it from this
 * one and saying it was gone is how a cluster fills up with copies nobody can
 * see. Presence is asked as the dialog opens, so the nodes that actually hold
 * it are the ones preselected: a node without a copy has nothing to reclaim.
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

  return (
    <NodeScopedDialog
      verb="delete"
      title={t("models.deleteTitle")}
      body={t("models.deleteBody", { model })}
      legend={t("models.alsoRemoveFrom")}
      nodes={peers}
      fetchPresence={(nodes) => fetchModelPresence(model, nodes)}
      noteFor={(_node, holds) =>
        holds === false ? <span className="text-[13px] text-muted">{t("models.notThere")}</span> : null
      }
      confirmLabel={(selected) => (selected.length > 0 ? t("models.deleteConfirm") : t("common.delete"))}
      onConfirm={(nodes) => onConfirm(nodes)}
      onClose={onClose}
    />
  );
}

/** Which machines get a copy.
 *
 * The inverse of the delete dialog: a node presence already reports as holding
 * the model has nothing to gain from a transfer, so the nodes actually missing
 * it are what is preselected. The control node is never offered — it is always
 * the source, and `peers` already excludes it. `force` is there for the case
 * presence gets it wrong (a node that verifies against a different manifest,
 * say): re-transfer rather than trust the skip.
 */
export function ModelReplicateDialog({
  model,
  peers,
  onClose,
}: {
  model: string;
  peers: string[];
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [result, setResult] = useState<ModelSyncResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const resultFor = useCallback(
    (node: string) => result?.results.find((r) => r.node === node) ?? null,
    [result],
  );

  const doSync = async (nodes: string[], { force }: { force: boolean }) => {
    setError(null);
    setResult(null);
    try {
      setResult(await syncModelToNodes(model, nodes, undefined, { force }));
    } catch (err) {
      setError(err instanceof Error ? err.message : t("models.unknownError"));
    }
  };

  return (
    <NodeScopedDialog
      verb="replicate"
      title={t("models.replicateTitle")}
      body={t("models.replicateBody", { model })}
      legend={t("models.replicateTo")}
      nodes={peers}
      fetchPresence={(nodes) => fetchModelPresence(model, nodes)}
      forceLabel={t("models.forceOption")}
      confirmLabel={t("models.replicateConfirm")}
      requireSelection
      error={error}
      onConfirm={doSync}
      onClose={onClose}
      noteFor={(node, holds) => {
        const outcome = resultFor(node);
        return (
          <>
            {holds === true && (
              <span className="text-[13px] text-muted">{t("models.alreadyThere")}</span>
            )}
            {outcome && (
              <span className={outcome.ok ? "text-[13px] text-good" : "text-[13px] text-bad"}>
                {outcome.ok
                  ? outcome.skipped
                    ? t("models.replicateSkipped")
                    : t("models.replicateVerified")
                  : outcome.error || t("models.replicateNodeFailed")}
              </span>
            )}
          </>
        );
      }}
    />
  );
}
