/** What is cached, where it is, and what is still arriving.
 *
 * A console list: a search box and the catalogue as one table. A row opens in
 * place to say which node holds which copy — a few lines, so it stays a strip
 * under the row rather than a panel beside the list. Downloading is the page's one primary action and opens a dialog — it
 * is done now and then, while the list is read every visit. The cache
 * directories are a second table under the catalogue.
 *
 * The column that is new is **Where**. Deleting a model from this machine and
 * calling it gone is what left 26 GB on three Sparks; the dialog already knew
 * which nodes held a copy, but only once it was open. The row says it.
 */

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Boxes, ChevronRight, Copy, Download, Rocket, Trash2, X } from "lucide-react";
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
import { cn, formatSize } from "@/lib/utils";
import {
  ACTIVE_STATES,
  AlertModal,
  Button,
  DetailRow,
  EmptyState,
  ErrorLine,
  Field,
  IconButton,
  Input,
  KeyValues,
  Modal,
  NodeScopedDialog,
  NodeState,
  ProgressRow,
  SearchInput,
  Section,
  Select,
  Spinner,
  Table,
  Td,
  Th,
  THead,
  Toolbar,
  Tr,
  matchesQuery,
  type NodeCondition,
} from "@/ui";
import type { AlertTone } from "@/ui";
import type {
  CacheResponse,
  ModelDownloadJob,
  ModelEntry,
  ModelPresence,
  ModelSyncResult,
  ScheduledDeploy,
} from "@/lib/types";
import CachesSection from "./CachesSection";
import { LibraryStateChip, type LibraryState } from "./LibraryState";

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
  // A GGUF repository: the quantisation is in each file's name, and a repo
  // often ships several. One is named; several are counted, with the list on
  // hover (ggufDetail).
  const gguf = model.gguf ?? [];
  if (gguf.length === 1) return `GGUF · ${gguf[0].quant || "?"}`;
  if (gguf.length > 1) return `GGUF ×${gguf.length}`;
  const cfg = model.config;
  if (!cfg) return "—";
  if (cfg.quantization_method) return cfg.quantization_method;
  if (cfg.quantization.length) return "quantized";
  return cfg.torch_dtype || "—";
}

/** Every GGUF in the repo with its size, for the precision cell's hover. */
export function ggufDetail(model: ModelEntry): string | undefined {
  const gguf = model.gguf ?? [];
  if (gguf.length < 2) return gguf[0]?.file;
  return gguf.map((g) => `${g.quant || g.file} · ${formatSize(g.size_bytes)}`).join("\n");
}

function StandingChip({ standing }: { standing: ModelStanding }) {
  return <LibraryStateChip state={standing.state} detail={standing.detail} title={standing.title} />;
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

/** Where a model stands, in the one vocabulary every Library page speaks.
 *
 *  Built from the same per-node verdict as the column it replaces: every node
 *  holding a verified copy is *on nodes*, some short of one is *partial*, a
 *  node that could not be asked is *unknown* — never counted as absent — and a
 *  download in flight is *getting*. There is no catalogue to offer a model
 *  from, so *available* and *update* do not occur. */
export interface ModelStanding {
  state: LibraryState;
  detail?: string;
  title?: string;
}

export function modelStanding(
  where: WhereVerdict,
  entries: PresenceEntry[] | null,
  downloading: { percent: number | null } | null,
): ModelStanding {
  if (downloading) {
    return { state: "getting", detail: downloading.percent != null ? `${downloading.percent}%` : undefined };
  }
  if (!entries || entries.length === 0) return { state: "unknown", title: where.title };
  const failed = entries.some((e) => e.error);
  if (where.state === "ok") return { state: "on_nodes", detail: where.label, title: where.title };
  // A partial copy is a definite answer from one node, whatever the others said.
  if (where.state !== "bad" && (failed || where.state === "unknown")) return { state: "unknown", title: where.title };
  return { state: "partial", detail: where.label, title: where.title };
}

export interface ModelsTabProps {
  models: UseQueryResult<ModelEntry[]>;
  cache: UseQueryResult<CacheResponse>;
  /** `/cache` is still a bookmark: it opens this tab at the caches section. */
  scrollToCaches?: boolean;
  /** The page header's Download button opens the dialog this tab owns. */
  downloadOpen?: boolean;
  onDownloadOpenChange?: (open: boolean) => void;
}

/** A scheduled deploy whose run was created and then failed — the image did
 *  not pull, the engine died. "Deployed" over that is the wrong answer. */
function runFailed(entry: ScheduledDeploy): boolean {
  return entry.run?.status === "error";
}

export default function ModelsTab({
  models: modelsQuery,
  cache,
  scrollToCaches,
  downloadOpen = false,
  onDownloadOpenChange,
}: ModelsTabProps) {
  const { t, plural } = useI18n();
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
  const [alert, setAlert] = useState<{ title: string; message: string; tone?: AlertTone } | null>(null);
  const [scheduled, setScheduled] = useState<ScheduledDeploy[]>([]);
  const [presence, setPresence] = useState<Record<string, ModelPresence>>({});
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

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
   *  both in — this machine included, and on a solo install too. The
   *  catalogue lists a model from the first file a download writes, so it is
   *  not the answer: on the lab a 1 GB download read "1 of 1 nodes" while it
   *  was still arriving. */
  const asked = useRef(new Set<string>());
  useEffect(() => {
    if (!models) return;
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

  /** Every node's own verdict on one model, this machine first. */
  const entriesFor = useCallback(
    (id: string): PresenceEntry[] | null => {
      const answer = presence[id];
      if (!answer) return null;
      const here: PresenceEntry = {
        node: controlName,
        state: answer.local_state ?? (answer.local ? "verified" : "absent"),
        filtered: answer.local_filtered,
      };
      return [here, ...answer.nodes];
    },
    [presence, controlName],
  );

  const whereFor = useCallback(
    (model: ModelEntry): WhereVerdict => {
      // A download still running is not a copy anywhere yet, whatever the
      // cache directory already holds.
      const downloading = jobs.find((j) => j.model === model.id && ACTIVE_STATES.includes(j.status));
      if (downloading) {
        const percent = downloading.bytes_total
          ? Math.floor(((downloading.bytes_done ?? 0) / downloading.bytes_total) * 100)
          : null;
        return {
          state: "warn",
          label:
            percent != null
              ? t("library.whereDownloading", { percent })
              : t("library.whereDownloadingUnknown"),
        };
      }
      const entries = entriesFor(model.id);
      if (!entries) return { state: "unknown", label: t("library.whereUnknown") };
      return describeWhere(entries, t);
    },
    [jobs, entriesFor, t],
  );

  const standingFor = useCallback(
    (model: ModelEntry): ModelStanding => {
      const job = jobs.find((j) => j.model === model.id && ACTIVE_STATES.includes(j.status));
      const percent =
        job && job.bytes_total ? Math.floor(((job.bytes_done ?? 0) / job.bytes_total) * 100) : null;
      return modelStanding(whereFor(model), entriesFor(model.id), job ? { percent } : null);
    },
    [jobs, whereFor, entriesFor],
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
      if (frame.type === "model.download.completed") {
        // Ask again: the answer from before the bytes landed is stale.
        asked.current.delete(job.model);
        setPresence((current) => {
          const next = { ...current };
          delete next[job.model];
          return next;
        });
        refetch();
      }
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
  /** Finished downloads are not shown: the section is for what is in flight,
   *  and a finished one is in the catalogue below. The one exception is a
   *  download whose scheduled deploy then failed — the deploy never created
   *  its run, or its run died — because that is the case an operator would
   *  otherwise wait on for ever, and the only place it is said is here. */
  const needsAttention = useCallback(
    (jobId: string) =>
      scheduled.some(
        (e) => e.download_job_id === jobId && (e.status === "failed" || runFailed(e)),
      ),
    [scheduled],
  );
  const recent = useMemo(
    () =>
      jobs
        .filter((j) => !ACTIVE_STATES.includes(j.status) && needsAttention(j.id))
        .slice(0, 5),
    [jobs, needsAttention],
  );

  const shown = useMemo(
    () => (models ?? []).filter((m) => matchesQuery(query, m.id, describePrecision(m))),
    [models, query],
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
      onDownloadOpenChange?.(false);
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
          tone: "info",
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
  const canReplicate = (where: WhereVerdict) => peers.length > 0 && where.state !== "ok";

  const actionsFor = (model: ModelEntry, where: WhereVerdict) => (
    <>
      {canReplicate(where) && (
        <IconButton
          size="sm"
          icon={Copy}
          label={t("library.replicateModel", { model: model.id })}
          onClick={() => setReplicateTarget(model.id)}
        />
      )}
      <IconButton
        size="sm"
        icon={Trash2}
        label={t("library.removeModel", { model: model.id })}
        onClick={() => setDeleteTarget(model.id)}
      />
    </>
  );

  /** The few lines a row has to add: the full revision, and each node's copy. */
  const detailFor = (model: ModelEntry) => {
    const where = whereFor(model);
    const entries = entriesFor(model.id);
    return (
      <div className="flex flex-wrap gap-x-8 gap-y-3 text-[12.5px]" data-testid={`model-detail-${model.id}`}>
        <KeyValues
          items={[
            [t("models.colRevision"), <span className="font-mono break-all">{model.revision || "—"}</span>],
            [t("library.colQuant"), <span title={ggufDetail(model)}>{describePrecision(model)}</span>],
          ]}
        />
        <div className="min-w-0">
          <p className="mb-1 text-[10.5px] font-semibold uppercase tracking-[0.08em] text-muted">{t("library.colWhere")}</p>
          {entries ? (
            <ul className="flex flex-wrap gap-x-5 gap-y-1">
              {entries.map((entry) => (
                <li key={entry.node} className="inline-flex items-center gap-2">
                  <span className="font-mono">{entry.node}</span>
                  <NodeState
                    state={entry.error ? "unknown" : entry.state === "verified" ? "ok" : entry.state === "partial" ? "bad" : "unknown"}
                    label={
                      entry.error
                        ? t("engines.couldNotAsk")
                        : entry.state === "verified"
                          ? t("library.presenceVerified")
                          : entry.state === "partial"
                            ? t("library.presencePartial")
                            : t("library.presenceAbsent")
                    }
                    title={entry.error ?? undefined}
                  />
                </li>
              ))}
            </ul>
          ) : (
            <NodeState state={where.state} label={where.label} title={where.title} />
          )}
        </div>
      </div>
    );
  };

  const downloads = (active.length > 0 || recent.length > 0) && (
    <Section
      title={t("models.downloads")}
      meta={connected ? t("models.live") : t("models.polling")}
      bodyClassName="space-y-2"
    >
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
                  className={
                    entry.status === "failed" || runFailed(entry) ? "text-bad shrink-0" : "text-blue2 shrink-0"
                  }
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
                  {entry.status === "done" && runFailed(entry) && (
                    <>
                      <span className="font-medium text-text">{entry.name}</span>{" "}
                      {t("models.couldNotDeploy")} {entry.run?.error || t("common.unknownError")}
                    </>
                  )}
                  {entry.status === "done" && !runFailed(entry) && (
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
    </Section>
  );

  return (
    <div className="space-y-4">
      {downloads}

      <div>
        <Toolbar>
          <SearchInput
            value={query}
            onChange={setQuery}
            label={t("library.filterModels")}
            clearLabel={t("common.clear")}
          />
          {models && (
            <span className="ml-auto text-[12.5px] text-muted tabular-nums">
              {plural("library.modelCount", shown.length)}
            </span>
          )}
        </Toolbar>

        {loading && (
          <div className="flex justify-center py-16">
            <Spinner size="lg" label={t("common.loading")} />
          </div>
        )}
        <ErrorLine>{error}</ErrorLine>

        {models && models.length > 0 && (
          <>
            {wide ? (
              <Section flush>
                <Table data-testid="models-table">
                  <THead>
                    <Th>{t("models.colModel")}</Th>
                    <Th numeric>{t("models.colSize")}</Th>
                    <Th>{t("models.colRevision")}</Th>
                    <Th>{t("library.colQuant")}</Th>
                    <Th>{t("library.colWhere")}</Th>
                    <Th srOnly>{t("models.colActions")}</Th>
                  </THead>
                  <tbody>
                    {shown.map((m) => {
                      const where = whereFor(m);
                      const open = m.id === selected;
                      return (
                        <Fragment key={m.id}>
                          <Tr
                            data-testid={`model-${m.id}`}
                            interactive
                            selected={open}
                            aria-expanded={open}
                            onClick={() => setSelected(open ? null : m.id)}
                          >
                            <Td mono truncate title={m.id} className="max-w-[420px]">
                              <ChevronRight
                                size={13}
                                aria-hidden="true"
                                className={cn("mr-1.5 inline text-muted transition-transform", open && "rotate-90")}
                              />
                              {m.id}
                            </Td>
                            <Td numeric>{formatSize(m.size_bytes)}</Td>
                            <Td mono muted>{shortRevision(m.revision)}</Td>
                            <Td title={ggufDetail(m)} className="whitespace-nowrap">{describePrecision(m)}</Td>
                            <Td className="whitespace-nowrap">
                              <StandingChip standing={standingFor(m)} />
                            </Td>
                            <Td actions>{actionsFor(m, where)}</Td>
                          </Tr>
                          {open && <DetailRow colSpan={6}>{detailFor(m)}</DetailRow>}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </Table>
                {shown.length === 0 && (
                  <p className="px-3 py-8 text-center text-[13px] text-muted">{t("library.noMatch")}</p>
                )}
              </Section>
            ) : (
              <Section flush>
                <ul className="divide-y divide-line">
                  {shown.map((m) => {
                    const where = whereFor(m);
                    return (
                      <li key={m.id} data-testid={`model-${m.id}`} className="p-3">
                        <div className="flex items-start gap-2">
                        <button
                          type="button"
                          aria-expanded={m.id === selected}
                          onClick={() => setSelected(m.id === selected ? null : m.id)}
                          className="min-w-0 flex-1 space-y-1 text-left"
                        >
                          <p className="font-mono text-[13px] break-all">{m.id}</p>
                          <p className="text-[12.5px] text-muted font-mono">
                            {formatSize(m.size_bytes)} · <span title={ggufDetail(m)}>{describePrecision(m)}</span>
                          </p>
                          <StandingChip standing={standingFor(m)} />
                        </button>
                        <div className="flex shrink-0 items-center gap-0.5">{actionsFor(m, where)}</div>
                        </div>
                        {m.id === selected && <div className="mt-3 border-t border-line pt-3">{detailFor(m)}</div>}
                      </li>
                    );
                  })}
                </ul>
                {shown.length === 0 && (
                  <p className="px-3 py-8 text-center text-[13px] text-muted">{t("library.noMatch")}</p>
                )}
              </Section>
            )}
          </>
        )}

        {models && models.length === 0 && !loading && (
          <EmptyState icon={Boxes}>{t("models.empty")}</EmptyState>
        )}
      </div>

      <CachesSection cache={cache} scrollTo={scrollToCaches} />

      {downloadOpen && (
        <Modal
          open
          onClose={() => onDownloadOpenChange?.(false)}
          title={t("models.downloadModel")}
          size="md"
        >
          <form onSubmit={submit} className="space-y-4">
            <Field label={t("models.modelId")}>
              {(control) => (
                <Input
                  {...control}
                  mono
                  autoFocus
                  placeholder={t("models.modelIdPlaceholder")}
                  value={modelId}
                  onChange={(e) => setModelId(e.target.value)}
                />
              )}
            </Field>
            <div className="grid grid-cols-1 gap-4 min-[520px]:grid-cols-2">
              <Field label={t("models.source")} hint={t("library.sourcesInSettings")}>
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
            </div>
            <div className="flex justify-end gap-2">
              <Button type="button" onClick={() => onDownloadOpenChange?.(false)}>
                {t("common.cancel")}
              </Button>
              <Button
                type="submit"
                variant="primary"
                icon={Download}
                loading={starting}
                disabled={!modelId.trim()}
              >
                {t("library.download")}
              </Button>
            </div>
          </form>
        </Modal>
      )}

      {deleteTarget && (
        <ModelDeleteDialog
          model={deleteTarget}
          peers={peers}
          onClose={() => setDeleteTarget(null)}
          onConfirm={(onNodes) => {
            const id = deleteTarget;
            setDeleteTarget(null);
            if (selected === id) setSelected(null);
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
          tone={alert.tone}
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
