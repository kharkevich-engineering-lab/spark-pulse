/** What is cached, where it is, and what is still arriving.
 *
 * The Models page and the Cache page answered the same question — what is on
 * this disk — in two places, so an operator hunting for space read one page,
 * failed to find it, and went looking for the other. They are one tab now: the
 * catalogue, then a rule, then the cache directories under it.
 *
 * The column that is new is **Where**. Deleting a model from this machine and
 * calling it gone is what left 26 GB on three Sparks; the dialog already knew
 * which nodes held a copy, but only once it was open. The row says it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Boxes, Download, Rocket, X } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import {
  cancelModelDownload,
  cancelScheduledDeploy,
  deleteModel,
  fetchModelDownloads,
  fetchModelPresence,
  fetchModelSources,
  fetchNodes,
  fetchScheduledDeploys,
  startModelDownload,
  syncModelToNodes,
} from "@/lib/api";
import { useQuery, type UseQueryResult } from "@/hooks/useQuery";
import { useWideLayout } from "@/hooks/useMediaQuery";
import { useSSEConnection } from "@/hooks/useSSEConnection";
import { SSEConnectionState } from "@/lib/operations";
import { formatSize } from "@/lib/utils";
import {
  ACTIVE_STATES,
  AlertModal,
  Button,
  EmptyState,
  ErrorLine,
  Field,
  IconButton,
  Input,
  NodeScopedDialog,
  NodeState,
  ProgressRow,
  Select,
  Spinner,
  type NodeCondition,
} from "@/ui";
import type {
  CacheResponse,
  ModelDownloadJob,
  ModelEntry,
  ModelPresence,
  ModelSyncResult,
  ScheduledDeploy,
} from "@/lib/types";
import CachesSection from "./CachesSection";

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

/** One node's verdict, as the presence endpoint reports it. */
interface PresenceEntry {
  node: string;
  state?: string;
  /** Verified against what the download asked for, not the whole repository. */
  filtered?: boolean;
  error?: string | null;
}

export interface WhereVerdict {
  state: NodeCondition;
  label: string;
  title?: string;
}

/** "2 of 2 nodes", "gx10-ced2 only", "partial on gx10-ced2", "not checked".
 *
 * A node that could not be asked is not a node without a copy: it gets the
 * muted verdict and its error on hover, never a count that quietly treats
 * silence as absence.
 */
export function describeWhere(
  entries: PresenceEntry[] | null,
  t: (key: string, vars?: Record<string, string | number>) => string,
): WhereVerdict {
  if (!entries || entries.length === 0) return { state: "unknown", label: t("library.whereUnknown") };

  const failed = entries.filter((e) => e.error);
  const partial = entries.find((e) => e.state === "partial");
  if (partial) {
    return {
      state: "bad",
      label: t("library.wherePartial", { node: partial.node }),
      title: t("library.wherePartialTitle"),
    };
  }
  if (failed.length === entries.length) {
    return {
      state: "unknown",
      label: t("library.whereUnknown"),
      title: failed.map((e) => `${e.node}: ${e.error}`).join("\n"),
    };
  }

  const holders = entries.filter((e) => e.state === "verified");
  const notes = failed.map((e) => `${e.node}: ${e.error}`);
  // A filtered copy is verified, and the hover says so — a short file count
  // with nothing explaining it reads as a copy that went wrong.
  if (holders.some((e) => e.filtered)) notes.push(t("library.whereFiltered"));
  const title = notes.length > 0 ? notes.join("\n") : undefined;
  if (holders.length === entries.length) {
    return {
      state: "ok",
      label: t("library.whereNodes", { count: holders.length, total: entries.length }),
      title,
    };
  }
  if (holders.length === 1) {
    return { state: "warn", label: t("library.whereOnly", { node: holders[0].node }), title };
  }
  return {
    state: "warn",
    label: t("library.whereNodes", { count: holders.length, total: entries.length }),
    title,
  };
}

export interface ModelsTabProps {
  models: UseQueryResult<ModelEntry[]>;
  cache: UseQueryResult<CacheResponse>;
  /** `/cache` is still a bookmark: it opens this tab at the caches section. */
  scrollToCaches?: boolean;
}

export default function ModelsTab({ models: modelsQuery, cache, scrollToCaches }: ModelsTabProps) {
  const { t } = useI18n();
  const { data: models, loading, error, refetch } = modelsQuery;
  const { data: sources } = useQuery(fetchModelSources);
  const { data: nodes } = useQuery(fetchNodes);
  const wide = useWideLayout();

  const [jobs, setJobs] = useState<ModelDownloadJob[]>([]);
  const [modelId, setModelId] = useState("");
  const [sourceName, setSourceName] = useState("");
  const [revision, setRevision] = useState("");
  const [starting, setStarting] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [replicateTarget, setReplicateTarget] = useState<string | null>(null);
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);
  const [scheduled, setScheduled] = useState<ScheduledDeploy[]>([]);
  const [presence, setPresence] = useState<Record<string, ModelPresence>>({});

  // The other machines a copy could be sitting on. Deleting from this one and
  // calling the model gone is what left 26 GB on three Sparks.
  const peers = useMemo(
    () => (nodes ?? []).filter((n) => !n.is_control_plane).map((n) => n.address),
    [nodes],
  );
  const controlName = useMemo(() => {
    const control = (nodes ?? []).find((n) => n.is_control_plane);
    return control?.name || control?.address || t("library.thisNode");
  }, [nodes, t]);

  const reloadJobs = useCallback(() => {
    fetchModelDownloads().then(setJobs).catch(() => {});
  }, []);

  // What each download is *for*. A progress bar with no purpose attached is
  // the thing this feature exists to stop showing: an operator who left the
  // deploy page has no other way to know a deployment is waiting on these
  // bytes, or to call it off.
  const reloadScheduled = useCallback(() => {
    fetchScheduledDeploys().then(setScheduled).catch(() => {});
  }, []);

  useEffect(() => {
    reloadJobs();
    reloadScheduled();
  }, [reloadJobs, reloadScheduled]);

  /** Ask every node about every model, once the catalogue and the registry are
   *  both in. Only worth a request when there is more than one machine: on a
   *  solo install the catalogue *is* the answer. */
  const asked = useRef(new Set<string>());
  useEffect(() => {
    if (peers.length === 0 || !models) return;
    for (const model of models) {
      if (asked.current.has(model.id)) continue;
      asked.current.add(model.id);
      fetchModelPresence(model.id, peers)
        .then((answer) => setPresence((current) => ({ ...current, [model.id]: answer })))
        .catch(() => {
          asked.current.delete(model.id);
        });
    }
  }, [models, peers]);

  const whereFor = useCallback(
    (model: ModelEntry): WhereVerdict => {
      if (peers.length === 0) {
        return { state: "ok", label: t("library.whereNodes", { count: 1, total: 1 }) };
      }
      const answer = presence[model.id];
      if (!answer) return { state: "unknown", label: t("library.whereUnknown") };
      const here: PresenceEntry = {
        node: controlName,
        state: answer.local_state ?? (answer.local ? "verified" : "absent"),
        filtered: answer.local_filtered,
      };
      return describeWhere([here, ...answer.nodes], t);
    },
    [peers, presence, controlName, t],
  );

  const onEvent = useCallback(
    (_event: string, data: unknown) => {
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
    },
    [refetch, reloadScheduled],
  );

  const sseStatus = useSSEConnection("/sse/models", onEvent);
  const connected = sseStatus.state === SSEConnectionState.CONNECTED;

  const active = useMemo(() => jobs.filter((j) => ACTIVE_STATES.includes(j.status)), [jobs]);
  const recent = useMemo(
    () => jobs.filter((j) => !ACTIVE_STATES.includes(j.status)).slice(0, 5),
    [jobs],
  );

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
      setAlert({
        title: t("models.downloadFailed"),
        message: err instanceof Error ? err.message : t("common.unknownError"),
      });
    } finally {
      setStarting(false);
    }
  };

  const doDelete = async (id: string, onNodes: string[]) => {
    try {
      const result = await deleteModel(id, onNodes);
      const removed = (result.nodes ?? []).filter((n) => n.removed).length;
      const refused = (result.nodes ?? []).filter((n) => n.error);
      if (refused.length > 0) {
        // Partly done is its own outcome. A page that says "deleted" while a
        // node still holds 26 GB is the reason this reports per node.
        setAlert({
          title: t("models.deleteFailed"),
          message: refused.map((n) => `${n.node || t("library.thisNode")}: ${n.error}`).join("\n"),
        });
      } else if (onNodes.length > 0) {
        setAlert({
          title: t("models.deleteTitle"),
          message: t("models.removedFrom", { count: removed, size: formatSize(result.freed_bytes) }),
        });
      }
      asked.current.delete(id);
      setPresence((current) => {
        const next = { ...current };
        delete next[id];
        return next;
      });
      refetch();
    } catch (err) {
      setAlert({
        title: t("models.deleteFailed"),
        message: err instanceof Error ? err.message : t("common.unknownError"),
      });
    }
  };

  const doCancel = async (jobId: string) => {
    try {
      await cancelModelDownload(jobId);
      reloadJobs();
      reloadScheduled();
    } catch (err) {
      setAlert({
        title: t("models.cancelFailed"),
        message: err instanceof Error ? err.message : t("common.unknownError"),
      });
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
      setAlert({
        title: t("models.cancelFailed"),
        message: err instanceof Error ? err.message : t("common.unknownError"),
      });
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

  /** Replicate is offered only while there is somewhere for the bytes to go.
   *
   *  `where.state === "ok"` is the one verdict that means every node answered
   *  and every node holds a verified copy, so replicating would send 200 GB to
   *  produce a row of "skipped". Every other verdict keeps the action: `warn`
   *  is a node short of a copy, `bad` is a partial one worth re-sending, and
   *  `unknown` is a node that could not be asked — which is not evidence it
   *  has the model, so the operator must still be able to send it. */
  const actionsFor = (model: ModelEntry, where: WhereVerdict) => (
    <div className="flex items-center gap-3 text-[13px]">
      {peers.length > 0 && where.state !== "ok" && (
        <button
          type="button"
          onClick={() => setReplicateTarget(model.id)}
          aria-label={t("library.replicateModel", { model: model.id })}
          className="text-blue2 hover:underline"
        >
          {t("library.replicate")}
        </button>
      )}
      <button
        type="button"
        onClick={() => setDeleteTarget(model.id)}
        aria-label={t("library.removeModel", { model: model.id })}
        className="text-blue2 hover:underline"
      >
        {t("library.remove")}
      </button>
    </div>
  );

  return (
    <div className="space-y-6">
      {/* One row: what to fetch, where from, which revision. Under 520 it is
          one column, and the button goes full width with it. */}
      <form
        onSubmit={submit}
        className="grid grid-cols-1 gap-3 min-[520px]:grid-cols-[2fr_1fr_1fr_auto] min-[520px]:items-end"
      >
        <Field label={t("models.modelId")}>
          {(control) => (
            <Input
              {...control}
              mono
              placeholder={t("models.modelIdPlaceholder")}
              value={modelId}
              onChange={(e) => setModelId(e.target.value)}
            />
          )}
        </Field>
        <Field label={t("models.source")}>
          {(control) => (
            <Select
              {...control}
              value={sourceName}
              onChange={(e) => setSourceName(e.target.value)}
            >
              <option value="">{t("models.defaultSource")}</option>
              {(sources ?? [])
                .filter((s) => s.type === "hf_hub")
                .map((s) => (
                  <option key={s.name} value={s.name}>
                    {s.name}
                  </option>
                ))}
            </Select>
          )}
        </Field>
        <Field label={t("models.revision")}>
          {(control) => (
            <Input
              {...control}
              mono
              placeholder={t("models.revisionPlaceholder")}
              value={revision}
              onChange={(e) => setRevision(e.target.value)}
            />
          )}
        </Field>
        <Button
          type="submit"
          variant="primary"
          icon={Download}
          loading={starting}
          disabled={!modelId.trim()}
          className="max-[519px]:w-full min-[520px]:mb-[26px]"
        >
          {t("library.download")}
        </Button>
      </form>

      <p className="text-[13px] text-muted">{t("library.sourcesInSettings")}</p>

      {/* Active downloads */}
      {(active.length > 0 || recent.length > 0) && (
        <section className="space-y-2">
          <div className="flex items-center gap-2">
            <h3 className="text-[17px] font-semibold tracking-[-0.02em]">{t("models.downloads")}</h3>
            <span className="text-[13px] text-muted">
              {connected ? t("models.live") : t("models.polling")}
            </span>
          </div>
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
                  className="mt-2 flex items-center justify-between gap-3 px-3 py-2 rounded-sm border border-line bg-bg2"
                >
                  <p className="text-[13px] text-muted flex items-center gap-2 min-w-0">
                    <Rocket
                      size={13}
                      className={entry.status === "failed" ? "text-bad shrink-0" : "text-blue2 shrink-0"}
                    />
                    <span className="truncate">
                      {entry.status === "waiting" && (
                        <>
                          {t("models.scheduledTo")}{" "}
                          <span className="font-medium text-text">{entry.name}</span>{" "}
                          {t("models.whenFinishes")}
                        </>
                      )}
                      {entry.status === "deploying" && (
                        <>
                          {t("models.deployingNow")}{" "}
                          <span className="font-medium text-text">{entry.name}</span>{" "}
                          {t("models.now")}
                        </>
                      )}
                      {entry.status === "done" && (
                        <>
                          {t("models.deployed")}{" "}
                          <span className="font-medium text-text">{entry.name}</span>
                        </>
                      )}
                      {entry.status === "failed" && (
                        <>
                          <span className="font-medium text-text">{entry.name}</span>{" "}
                          {t("models.couldNotDeploy")} {entry.error}
                        </>
                      )}
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
      )}

      {/* Catalogue */}
      {loading && (
        <div className="flex justify-center py-16">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      )}
      <ErrorLine>{error}</ErrorLine>

      {models && models.length > 0 && wide && (
        <div className="rounded-md bg-surface border border-line overflow-x-auto">
          <table className="w-full text-[14px]">
            <thead>
              <tr className="text-left text-muted border-b border-line">
                <th className="p-3 font-medium">{t("models.colModel")}</th>
                <th className="p-3 font-medium">{t("models.colSize")}</th>
                <th className="p-3 font-medium">{t("models.colRevision")}</th>
                <th className="p-3 font-medium">{t("library.colQuant")}</th>
                <th className="p-3 font-medium">{t("library.colWhere")}</th>
                <th className="p-3 font-medium sr-only">{t("models.colActions")}</th>
              </tr>
            </thead>
            <tbody>
              {models.map((m) => {
                const where = whereFor(m);
                return (
                  <tr key={m.id} data-testid={`model-${m.id}`} className="border-b border-line last:border-0">
                    <td className="p-3 font-mono">{m.id}</td>
                    <td className="p-3 font-mono">{formatSize(m.size_bytes)}</td>
                    <td className="p-3 font-mono text-muted">{shortRevision(m.revision)}</td>
                    <td className="p-3">{describePrecision(m)}</td>
                    <td className="p-3">
                      <NodeState state={where.state} label={where.label} title={where.title} />
                    </td>
                    <td className="p-3 text-right whitespace-nowrap">{actionsFor(m, where)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {models && models.length > 0 && !wide && (
        <div className="space-y-3">
          {models.map((m) => {
            const where = whereFor(m);
            return (
              <div
                key={m.id}
                data-testid={`model-${m.id}`}
                className="rounded-md bg-surface border border-line p-4 space-y-2"
              >
                <p className="font-mono text-[14px] break-all">{m.id}</p>
                <p className="text-[13px] text-muted font-mono">
                  {formatSize(m.size_bytes)} · {describePrecision(m)}
                </p>
                <NodeState state={where.state} label={where.label} title={where.title} />
                {actionsFor(m, where)}
              </div>
            );
          })}
        </div>
      )}

      {models && models.length === 0 && !loading && (
        <EmptyState icon={Boxes}>{t("models.empty")}</EmptyState>
      )}

      <CachesSection cache={cache} scrollTo={scrollToCaches} />

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
        <AlertModal
          open={!!alert}
          onClose={() => setAlert(null)}
          title={alert.title}
          message={alert.message}
        />
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
      confirmLabel={(selected) =>
        selected.length > 0 ? t("models.deleteConfirm") : t("common.delete")
      }
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
      setError(err instanceof Error ? err.message : t("common.unknownError"));
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
