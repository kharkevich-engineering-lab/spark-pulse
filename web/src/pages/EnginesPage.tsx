/** Engines: what this cluster can run, and what it costs in disk.
 *
 * There used to be two answers to that one question. "Engines" was a Settings
 * tab listing what the registry knows about; "Images" was a page listing what
 * is on this host. They describe the same object — an engine *is* its image —
 * and an operator deciding whether to keep a 26 GB image had to read the
 * capability list on one page and the size on another.
 *
 * So: one row per engine, carrying both. The registry settings that govern
 * where engines come from sit at the bottom of the page they govern, rather
 * than in a form beside shm sizes.
 *
 * The other half of the merge is per-node. `sync` pushes an image to every
 * machine in the cluster and, until now, delete could only clean this one —
 * so a four-node Spark could be filled from here and tidied nowhere. Expanding
 * a row asks each node what it holds, and the delete dialog is where you say
 * which of them to clear.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  ArrowDownToLine,
  ChevronDown,
  ChevronRight,
  Download,
  Layers,
  Loader2,
  RefreshCw,
  Server,
  Trash2,
  X,
} from "lucide-react";
import { Link } from "react-router-dom";
import {
  cancelImagePull,
  deleteImage,
  fetchEngines,
  fetchImagePresence,
  fetchImagePulls,
  fetchImages,
  fetchNodes,
  refreshEngines,
  startImagePull,
  syncImageToNodes,
  updateSettings,
  fetchSettings,
} from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { useSSEConnection } from "@/hooks/useSSEConnection";
import { SSEConnectionState } from "@/lib/operations";
import { useI18n } from "@/lib/i18n";
import { formatSize } from "@/lib/utils";
import { AlertModal, Modal } from "@/components/Modal";
import EngineBadge from "@/components/EngineBadge";
import type {
  EngineSummary,
  ImageEntry,
  ImagePresence,
  ImagePullJob,
  Settings,
} from "@/lib/types";

const ACTIVE_STATES = ["queued", "running"];

interface ImageEventFrame {
  type?: string;
  resource_type?: string;
  metadata?: unknown;
}

/** A digest is 71 characters of noise; show enough to compare two by eye. */
export function shortDigest(digest: string | null | undefined): string {
  if (!digest) return "—";
  const body = digest.startsWith("sha256:") ? digest.slice(7) : digest;
  return body.slice(0, 12);
}

/** Why this image wants attention, or "" when it does not. */
export function updateReason(image: ImageEntry): string {
  if (image.digest_drift) return "newer digest published";
  if (!image.present) return "not pulled";
  return "";
}

export function progressPercent(job: ImagePullJob): number {
  if (job.status === "completed") return 100;
  return Math.max(0, Math.min(100, Math.round(job.percent || 0)));
}

/** One engine, with everything known about it in one place.
 *
 * The join is on the image reference, which both sides already agree on: the
 * catalogue builds its entries from the same engine specs the registry serves.
 * An image with no engine — something pulled by hand — keeps its row rather
 * than disappearing, because it is still occupying disk.
 */
export interface EngineRow {
  key: string;
  image: ImageEntry;
  engine: EngineSummary | null;
}

export function joinEngines(
  images: ImageEntry[] | null,
  engines: EngineSummary[] | null,
): EngineRow[] {
  const byRef = new Map((engines ?? []).map((e) => [e.image_ref, e]));
  const byKey = new Map((engines ?? []).map((e) => [e.key, e]));
  return (images ?? []).map((image) => ({
    key: image.ref,
    image,
    engine: byRef.get(image.ref) ?? byKey.get(image.engine_key) ?? null,
  }));
}

export default function EnginesPage() {
  const { t } = useI18n();
  const { data: images, loading, error, refetch } = useQuery(fetchImages);
  const { data: engineData, refetch: refetchEngines } = useQuery(fetchEngines);
  const { data: nodes } = useQuery(fetchNodes);
  const { data: settings, refetch: refetchSettings } = useQuery(fetchSettings);

  const [jobs, setJobs] = useState<ImagePullJob[]>([]);
  const [pulling, setPulling] = useState<string | null>(null);
  const [ref, setRef] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [presence, setPresence] = useState<Record<string, ImagePresence | "loading">>({});
  const [deleteTarget, setDeleteTarget] = useState<EngineRow | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);

  const reloadJobs = useCallback(() => {
    fetchImagePulls().then(setJobs).catch(() => {});
  }, []);

  useEffect(() => {
    reloadJobs();
  }, [reloadJobs]);

  const onEvent = useCallback(
    (_event: string, data: unknown) => {
      const frame = data as ImageEventFrame;
      if (frame?.resource_type && frame.resource_type !== "image") return;
      const job = frame?.metadata as ImagePullJob | undefined;
      if (!job?.id) return;
      setJobs((current) =>
        current.some((j) => j.id === job.id)
          ? current.map((j) => (j.id === job.id ? { ...j, ...job } : j))
          : [job, ...current],
      );
      if (frame.type === "image.pull.completed" || frame.type === "image.deleted") refetch();
    },
    [refetch],
  );

  const sseStatus = useSSEConnection("/sse/images", onEvent);
  const connected = sseStatus.state === SSEConnectionState.CONNECTED;

  const rows = useMemo(
    () => joinEngines(images, engineData?.engines ?? null),
    [images, engineData],
  );
  const peers = useMemo(
    () => (nodes ?? []).filter((n) => !n.is_control_plane),
    [nodes],
  );
  const active = useMemo(() => jobs.filter((j) => ACTIVE_STATES.includes(j.status)), [jobs]);
  const recent = useMemo(
    () => jobs.filter((j) => !ACTIVE_STATES.includes(j.status)).slice(0, 5),
    [jobs],
  );
  const onDisk = useMemo(
    () => (images ?? []).reduce((sum, i) => sum + (i.present ? i.size_bytes : 0), 0),
    [images],
  );
  const needsAttention = useMemo(
    () => (images ?? []).filter((i) => i.update_available).length,
    [images],
  );

  /** Ask the nodes about one image, once, when its row is opened. */
  const toggleRow = async (row: EngineRow) => {
    if (expanded === row.key) {
      setExpanded(null);
      return;
    }
    setExpanded(row.key);
    if (peers.length === 0 || presence[row.key]) return;
    setPresence((current) => ({ ...current, [row.key]: "loading" }));
    try {
      const answer = await fetchImagePresence(row.image.ref, peers.map((n) => n.address));
      setPresence((current) => ({ ...current, [row.key]: answer }));
    } catch {
      setPresence((current) => {
        const next = { ...current };
        delete next[row.key];
        return next;
      });
    }
  };

  const pull = async (target: string) => {
    if (!target.trim()) return;
    setPulling(target);
    try {
      const job = await startImagePull(target.trim());
      setJobs((current) => (current.some((j) => j.id === job.id) ? current : [job, ...current]));
      setRef("");
    } catch (err) {
      setAlert({ title: t("engines.pullFailed"), message: err instanceof Error ? err.message : "Unknown error" });
    } finally {
      setPulling(null);
    }
  };

  const doDelete = async (target: string, onNodes: string[]) => {
    try {
      const result = await deleteImage(target, onNodes);
      const failed = (result.nodes ?? []).filter((n) => n.error);
      if (failed.length > 0) {
        setAlert({
          title: t("engines.partialDelete"),
          message: failed.map((n) => `${n.node}: ${n.error}`).join("\n"),
        });
      }
      setPresence((current) => {
        const next = { ...current };
        delete next[target];
        return next;
      });
      refetch();
    } catch (err) {
      setAlert({ title: t("engines.deleteFailed"), message: err instanceof Error ? err.message : "Unknown error" });
    }
  };

  const doSync = async (target: string) => {
    if (peers.length === 0) return;
    try {
      await syncImageToNodes(target, peers.map((n) => n.address));
      setPresence((current) => {
        const next = { ...current };
        delete next[target];
        return next;
      });
    } catch (err) {
      setAlert({ title: t("engines.syncFailed"), message: err instanceof Error ? err.message : "Unknown error" });
    }
  };

  const doCancel = async (jobId: string) => {
    try {
      await cancelImagePull(jobId);
      reloadJobs();
    } catch (err) {
      setAlert({ title: t("engines.cancelFailed"), message: err instanceof Error ? err.message : "Unknown error" });
    }
  };

  const doRefreshIndex = async () => {
    setRefreshing(true);
    try {
      await refreshEngines();
      await refetchEngines();
      refetch();
    } catch (err) {
      setAlert({ title: t("engines.refreshFailed"), message: err instanceof Error ? err.message : "Unknown error" });
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-2xl font-bold">{t("engines.title")}</h2>
          <p className="text-text-muted mt-1">
            {t("engines.subtitle")}{" "}
            <Link to="/models" className="text-primary hover:underline">
              {t("engines.modelsLink")}
            </Link>
          </p>
        </div>
        <div className="text-right">
          <p className="text-xs text-text-muted uppercase tracking-wide">{t("engines.onDisk")}</p>
          <p className="text-2xl font-bold">{formatSize(onDisk)}</p>
          {needsAttention > 0 && (
            <p className="text-xs text-warning mt-1">{t("engines.needAttention", { count: needsAttention })}</p>
          )}
        </div>
      </div>

      {/* Pull by reference. Kept from the Images page: an engine index is the
          usual way an image arrives, but not the only one. */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          pull(ref);
        }}
        className="p-5 rounded-xl bg-surface border border-border space-y-3"
      >
        <h3 className="font-semibold flex items-center gap-2">
          <Download size={16} className="text-primary" />
          {t("engines.pullTitle")}
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-2">
          <input
            aria-label={t("engines.refLabel")}
            placeholder={t("engines.refPlaceholder")}
            value={ref}
            onChange={(e) => setRef(e.target.value)}
            className="px-3 py-2 rounded-lg bg-bg border border-border font-mono text-sm"
          />
          <button
            type="submit"
            disabled={!!pulling || !ref.trim()}
            className="px-4 py-2 rounded-lg bg-primary/10 text-primary border border-primary/30 hover:bg-primary/20 disabled:opacity-50 flex items-center gap-2"
          >
            {pulling ? <Loader2 className="animate-spin" size={16} /> : <ArrowDownToLine size={16} />}
            {t("engines.pull")}
          </button>
        </div>
      </form>

      {(active.length > 0 || recent.length > 0) && (
        <section className="space-y-2">
          <div className="flex items-center gap-2">
            <h3 className="font-semibold">{t("engines.pulls")}</h3>
            <span className={connected ? "text-xs text-success" : "text-xs text-text-muted"}>
              {connected ? t("engines.live") : t("engines.polling")}
            </span>
          </div>
          {[...active, ...recent].map((job) => (
            <div key={job.id} data-testid={`pull-${job.id}`} className="p-4 rounded-xl bg-surface border border-border">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="font-mono text-sm truncate">{job.ref}</p>
                  <p className="text-xs text-text-muted">
                    {job.status}
                    {job.layers ? ` · ${job.layers} layers` : ""}
                    {job.error ? ` · ${job.error}` : ""}
                  </p>
                </div>
                <div className="flex items-center gap-3 shrink-0">
                  <span className="text-xs font-mono text-text-muted">
                    {formatSize(job.bytes_done)} / {job.bytes_total ? formatSize(job.bytes_total) : "?"}
                  </span>
                  {ACTIVE_STATES.includes(job.status) && (
                    <button
                      aria-label={`Cancel pull of ${job.ref}`}
                      onClick={() => doCancel(job.id)}
                      className="p-1.5 rounded-lg text-text-muted hover:text-danger hover:bg-danger/10"
                    >
                      <X size={15} />
                    </button>
                  )}
                </div>
              </div>
              <div className="mt-2 h-1.5 rounded-full bg-tag-bg overflow-hidden">
                <div
                  role="progressbar"
                  aria-label={`${job.ref} progress`}
                  aria-valuenow={progressPercent(job)}
                  className="h-full bg-primary transition-all"
                  style={{ width: `${progressPercent(job)}%` }}
                />
              </div>
            </div>
          ))}
        </section>
      )}

      {loading && (
        <div className="flex justify-center py-16">
          <Loader2 className="animate-spin text-primary" size={32} />
        </div>
      )}
      {error && (
        <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3">
          <AlertCircle size={20} />
          <span>{error}</span>
        </div>
      )}

      {rows.length > 0 && (
        <div className="rounded-xl bg-surface border border-border overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-text-muted border-b border-border">
                <th className="p-3 font-medium">{t("engines.colEngine")}</th>
                <th className="p-3 font-medium">{t("engines.colImage")}</th>
                <th className="p-3 font-medium">{t("engines.colStatus")}</th>
                <th className="p-3 font-medium">{t("engines.colSize")}</th>
                <th className="p-3 font-medium">{t("engines.colDigest")}</th>
                <th className="p-3 font-medium sr-only">{t("engines.colActions")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const { image, engine } = row;
                const reason = updateReason(image);
                const open = expanded === row.key;
                const answer = presence[row.key];
                return [
                  <tr key={row.key} data-testid={`engine-${image.ref}`} className="border-b border-border last:border-0 hover:bg-surface-hover">
                    <td className="p-3">
                      <button
                        type="button"
                        onClick={() => toggleRow(row)}
                        aria-expanded={open}
                        // Two rows can carry the same engine name at different
                        // versions, so the reference is what distinguishes them
                        // — for a screen reader as much as for a test.
                        aria-label={`Details for ${image.ref}`}
                        className="flex items-center gap-2 text-left hover:text-primary transition-colors"
                      >
                        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                        <span className="flex items-center gap-2">
                          {/* The catalogue knows the engine name even for a
                              version the registry no longer advertises — a
                              second tag of an engine we have is still that
                              engine, not an unmanaged image. Only something
                              with no engine at all is unmanaged. */}
                          {image.engine
                            ? <EngineBadge
                                engine={image.engine}
                                variant={image.variant}
                                enabled={engine ? engine.enabled : true}
                              />
                            : <span className="text-text-muted">{t("engines.unmanaged")}</span>}
                          {(engine?.version || image.version) && (
                            <span className="text-xs text-text-muted font-mono">
                              v{engine?.version || image.version}
                            </span>
                          )}
                        </span>
                      </button>
                    </td>
                    <td className="p-3 font-mono">
                      <span className="truncate">{image.repository}</span>
                      <span className="text-text-muted">:{image.tag}</span>
                    </td>
                    <td className="p-3">
                      {image.present ? (
                        <span className="text-success">{t("engines.present")}</span>
                      ) : (
                        <span className="text-text-muted">{t("engines.notPulled")}</span>
                      )}
                      {engine?.available === false && (
                        <span className="ml-2 px-1.5 py-0.5 rounded text-xs bg-text-muted/10 text-text-muted border border-border">
                          {t("engines.unpublished")}
                        </span>
                      )}
                      {reason && (
                        <span className="ml-2 px-1.5 py-0.5 rounded text-xs bg-warning/10 text-warning border border-warning/30">
                          {reason}
                        </span>
                      )}
                    </td>
                    <td className="p-3 font-mono">{image.present ? formatSize(image.size_bytes) : "—"}</td>
                    <td className="p-3 font-mono text-text-muted">
                      {shortDigest(image.local_digest)}
                      {image.digest_drift && (
                        <>
                          {" → "}
                          <span className="text-warning">{shortDigest(image.index_digest)}</span>
                        </>
                      )}
                    </td>
                    <td className="p-3 text-right whitespace-nowrap">
                      {reason && (
                        <button
                          aria-label={`Pull ${image.ref}`}
                          onClick={() => pull(image.ref)}
                          className="p-1.5 rounded-lg text-text-muted hover:text-primary hover:bg-primary/10"
                        >
                          <RefreshCw size={15} />
                        </button>
                      )}
                      {image.present && peers.length > 0 && (
                        <button
                          aria-label={`Copy ${image.ref} to every node`}
                          title={t("engines.syncTitle")}
                          onClick={() => doSync(image.ref)}
                          className="p-1.5 rounded-lg text-text-muted hover:text-primary hover:bg-primary/10"
                        >
                          <Server size={15} />
                        </button>
                      )}
                      {image.present && (
                        <button
                          aria-label={`Delete ${image.ref}`}
                          onClick={() => setDeleteTarget(row)}
                          className="p-1.5 rounded-lg text-text-muted hover:text-danger hover:bg-danger/10"
                        >
                          <Trash2 size={15} />
                        </button>
                      )}
                    </td>
                  </tr>,
                  open && (
                    <tr key={`${row.key}-detail`} className="border-b border-border last:border-0 bg-bg/40">
                      <td colSpan={6} className="p-4">
                        <EngineDetail
                          row={row}
                          presence={answer}
                          peerCount={peers.length}
                        />
                      </td>
                    </tr>
                  ),
                ];
              })}
            </tbody>
          </table>
        </div>
      )}

      {rows.length === 0 && !loading && (
        <div className="text-center py-16 text-text-muted">
          <Layers size={40} className="mx-auto mb-4 opacity-50" />
          <p>{t("engines.empty")}</p>
        </div>
      )}

      {/* Where engines come from. It used to be a Settings tab; it governs this
          page, so it lives at the bottom of it. */}
      <RegistrySettings
        settings={settings}
        onSaved={() => {
          refetchSettings();
          refetchEngines();
          refetch();
        }}
        onRefreshIndex={doRefreshIndex}
        refreshing={refreshing}
        onError={setAlert}
      />

      {deleteTarget && (
        <DeleteDialog
          row={deleteTarget}
          peers={peers.map((n) => n.address)}
          presence={presence[deleteTarget.key]}
          onClose={() => setDeleteTarget(null)}
          onConfirm={(onNodes) => {
            const ref = deleteTarget.image.ref;
            setDeleteTarget(null);
            doDelete(ref, onNodes);
          }}
        />
      )}

      {alert && (
        <AlertModal open onClose={() => setAlert(null)} title={alert.title} message={alert.message} />
      )}
    </div>
  );
}

/** What an engine is, and where its image is, under the row. */
function EngineDetail({
  row,
  presence,
  peerCount,
}: {
  row: EngineRow;
  presence: ImagePresence | "loading" | undefined;
  peerCount: number;
}) {
  const { t } = useI18n();
  const { engine, image } = row;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <div className="space-y-2 text-xs">
        <p className="font-medium text-sm">{engine?.description || image.description || t("engines.noDescription")}</p>
        <p className="font-mono text-text-muted break-all">{image.ref}</p>
        {engine && (
          <>
            <p className="text-text-muted">
              {Object.entries(engine.capabilities)
                .filter(([, on]) => on)
                .map(([name]) => name)
                .join(", ") || t("engines.noCapabilities")}
              {" · "}
              <span className="font-mono">:{engine.ports.api}</span>
              {engine.ports.rendezvous ? <span className="font-mono"> / :{engine.ports.rendezvous}</span> : null}
            </p>
            <p className="text-text-muted">
              {engine.verified.length > 0
                ? t("engines.verifiedCount", { count: engine.verified.length })
                : t("engines.neverVerified")}
              {" · "}
              {t("engines.source", { source: engine.source })}
            </p>
          </>
        )}
      </div>

      <div className="space-y-2 text-xs" data-testid={`presence-${image.ref}`}>
        <p className="font-medium text-sm">{t("engines.onNodes")}</p>
        {peerCount === 0 && <p className="text-text-muted">{t("engines.soloNode")}</p>}
        {peerCount > 0 && presence === "loading" && (
          <p className="text-text-muted flex items-center gap-1.5">
            <Loader2 className="animate-spin" size={12} />
            {t("engines.asking")}
          </p>
        )}
        {peerCount > 0 && presence && presence !== "loading" && (
          <ul className="space-y-1">
            {presence.nodes.map((node) => (
              <li key={node.node} className="flex items-center justify-between gap-3">
                <span className="font-mono">{node.node}</span>
                {node.error ? (
                  <span className="text-warning">{t("engines.couldNotAsk")}</span>
                ) : node.present ? (
                  <span className={node.matches ? "text-success" : "text-warning"}>
                    {node.matches ? t("engines.sameImage") : t("engines.differentImage")}
                  </span>
                ) : (
                  <span className="text-text-muted">{t("engines.absent")}</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/** Choosing where to delete from, rather than assuming "here". */
export function DeleteDialog({
  row,
  peers,
  presence,
  onClose,
  onConfirm,
}: {
  row: EngineRow;
  peers: string[];
  presence: ImagePresence | "loading" | undefined;
  onClose: () => void;
  onConfirm: (nodes: string[]) => void;
}) {
  const { t } = useI18n();
  // Preselected to the nodes that actually hold it, when we know: the point of
  // the dialog is reclaiming disk, and a node without the image has none to
  // reclaim.
  const holders = useMemo(() => {
    if (!presence || presence === "loading") return peers;
    return presence.nodes.filter((n) => n.present).map((n) => n.node);
  }, [presence, peers]);
  const [selected, setSelected] = useState<string[]>(holders);

  return (
    <Modal open onClose={onClose} title={t("engines.deleteTitle")}>
      <div className="space-y-4">
        <p className="text-sm text-text-muted">
          {t("engines.deleteBody", { ref: row.image.ref, size: formatSize(row.image.size_bytes) })}
        </p>

        {peers.length > 0 && (
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium mb-1">{t("engines.alsoRemoveFrom")}</legend>
            {peers.map((node) => (
              <label key={node} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={selected.includes(node)}
                  onChange={(e) =>
                    setSelected((current) =>
                      e.target.checked ? [...current, node] : current.filter((n) => n !== node),
                    )
                  }
                />
                <span className="font-mono">{node}</span>
                {!holders.includes(node) && (
                  <span className="text-xs text-text-muted">{t("engines.notThere")}</span>
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
            {t("engines.deleteConfirm")}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** Where engines come from: the indexes, the cache TTL, the default. */
export function RegistrySettings({
  settings,
  onSaved,
  onRefreshIndex,
  refreshing,
  onError,
}: {
  settings: Settings | null;
  onSaved: () => void;
  onRefreshIndex: () => void;
  refreshing: boolean;
  onError: (alert: { title: string; message: string }) => void;
}) {
  const { t } = useI18n();
  const [form, setForm] = useState<{ default_engine?: string; engine_indexes?: string[]; engine_index_cache_ttl_seconds?: number }>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (settings) {
      setForm({
        default_engine: settings.default_engine,
        engine_indexes: settings.engine_indexes,
        engine_index_cache_ttl_seconds: settings.engine_index_cache_ttl_seconds,
      });
    }
  }, [settings]);

  const save = async () => {
    setSaving(true);
    try {
      await updateSettings(form as Parameters<typeof updateSettings>[0]);
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
      onSaved();
    } catch (err) {
      onError({ title: t("engines.saveFailed"), message: err instanceof Error ? err.message : "Unknown error" });
    } finally {
      setSaving(false);
    }
  };

  const inputCls = "w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm";

  return (
    <section className="p-5 rounded-xl bg-surface border border-border space-y-4">
      <div className="flex items-center justify-between pb-3 border-b border-border">
        <h3 className="font-semibold">{t("engines.registry")}</h3>
        <button
          onClick={onRefreshIndex}
          disabled={refreshing}
          className="px-2.5 py-1 rounded-lg border border-border hover:border-primary/50 text-text-muted hover:text-text text-xs transition-colors flex items-center gap-1.5 disabled:opacity-50"
        >
          <RefreshCw size={13} className={refreshing ? "animate-spin" : ""} />
          {t("engines.refreshIndex")}
        </button>
      </div>

      <div>
        <label className="block text-sm font-medium mb-1" htmlFor="default-engine">{t("engines.defaultEngine")}</label>
        <input
          id="default-engine"
          type="text"
          value={form.default_engine ?? ""}
          onChange={(e) => setForm({ ...form, default_engine: e.target.value })}
          className={inputCls}
          placeholder="vllm"
        />
      </div>

      <div>
        <label className="block text-sm font-medium mb-1" htmlFor="engine-indexes">{t("engines.indexes")}</label>
        <textarea
          id="engine-indexes"
          rows={3}
          value={(form.engine_indexes ?? []).join("\n")}
          onChange={(e) => setForm({ ...form, engine_indexes: e.target.value.split("\n").map((l) => l.trim()).filter(Boolean) })}
          className={`${inputCls} resize-y`}
        />
        <p className="text-xs text-text-muted mt-1">{t("engines.indexesHelp")}</p>
      </div>

      <div>
        <label className="block text-sm font-medium mb-1" htmlFor="index-ttl">{t("engines.indexTtl")}</label>
        <div className="flex items-center gap-2">
          <input
            id="index-ttl"
            type="number"
            min="0"
            value={form.engine_index_cache_ttl_seconds ?? 3600}
            onChange={(e) => setForm({ ...form, engine_index_cache_ttl_seconds: parseInt(e.target.value) || 0 })}
            className="w-28 px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm"
          />
          <span className="text-sm text-text-muted">{t("engines.seconds")}</span>
        </div>
      </div>

      <button
        onClick={save}
        disabled={saving}
        className="px-4 py-2 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50 text-white font-medium text-sm"
      >
        {saving ? t("common.saving") : saved ? t("common.saved") : t("engines.saveRegistry")}
      </button>
    </section>
  );
}
