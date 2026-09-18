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
  ArrowDownToLine,
  ChevronDown,
  ChevronRight,
  Download,
  Layers,
  RefreshCw,
  Server,
  Trash2,
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
import {
  ACTIVE_STATES,
  AlertModal,
  Button,
  ErrorLine,
  Field,
  Input,
  NodeScopedDialog,
  NodeState,
  ProgressRow,
  Spinner,
  Textarea,
  Toggle,
} from "@/ui";
import EngineBadge from "@/components/EngineBadge";
import type {
  EngineSummary,
  ImageEntry,
  ImagePresence,
  ImagePullJob,
  Settings,
} from "@/lib/types";

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

/** A tag *is* a digest when the image was pulled `@sha256:...` rather than
 * by a floating tag like `latest` or `26.5.0` — the two look nothing alike
 * and the row needs to tell them apart before it can decide how to render
 * one. */
export function isDigestTag(tag: string | null | undefined): boolean {
  return /^sha256:[0-9a-f]{64}$/i.test(tag ?? "");
}

/** Same trade as `shortDigest` — enough of each end to compare by eye — but
 * kept in the tag's own `sha256:<hex>` shape rather than `shortDigest`'s bare
 * hex, since this is what stands in for the tag itself in the image ref, not
 * a value next to a label that already says "digest". A non-digest tag is
 * returned unchanged. */
export function shortImageTag(tag: string): string {
  if (!isDigestTag(tag)) return tag;
  const hex = tag.slice("sha256:".length);
  return `sha256:${hex.slice(0, 8)}…${hex.slice(-4)}`;
}

/** Why this image wants attention, or "" when it does not. */
export function updateReason(image: ImageEntry): string {
  if (image.digest_drift) return "newer digest published";
  if (!image.present) return "not pulled";
  return "";
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

/** An `engines` map entry as settings.json actually carries it: a dict once
 * something has edited it, but still a bare bool for an engine nobody has
 * touched since before the dict shape existed. Read either. */
function normalizeEngineEntry(entry: unknown): { enabled?: boolean } {
  if (typeof entry === "boolean") return { enabled: entry };
  if (entry && typeof entry === "object") return entry as { enabled?: boolean };
  return {};
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
  // Optimistic per-engine enabled state: set on click, cleared on success (the
  // refetch below is then the source of truth) or on failure (which restores
  // whatever the server last said by simply removing the override).
  const [engineOverrides, setEngineOverrides] = useState<Record<string, boolean>>({});
  const [togglingEngine, setTogglingEngine] = useState<string | null>(null);

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

  // Enabled state is per engine name, not per image row: two variants of the
  // same engine share one switch in config.engines. This is every engine the
  // registry knows about, not just the ones with a row here, because the
  // guard below has to see an engine nobody has pulled yet too.
  const enabledByEngine = useMemo(() => {
    const map: Record<string, boolean> = {};
    for (const e of engineData?.engines ?? []) {
      if (!(e.engine in map)) map[e.engine] = e.enabled;
    }
    return map;
  }, [engineData]);

  const effectiveEnabled = useCallback(
    (engineName: string) => engineOverrides[engineName] ?? enabledByEngine[engineName] ?? true,
    [engineOverrides, enabledByEngine],
  );

  const enabledEngineCount = useMemo(
    () => Object.keys(enabledByEngine).filter((name) => effectiveEnabled(name)).length,
    [enabledByEngine, effectiveEnabled],
  );

  /** Flip one engine on or off. Disabling the last enabled engine would leave
   * no recipe deployable, so it is refused here — the backend does not guard
   * this, it just returns a config an operator would have to notice broke
   * everything. */
  const toggleEngineEnabled = async (engineName: string, next: boolean) => {
    if (!next && enabledEngineCount <= 1 && effectiveEnabled(engineName)) {
      setAlert({ title: t("engines.lastEngineTitle"), message: t("engines.lastEngineMessage") });
      return;
    }
    setTogglingEngine(engineName);
    setEngineOverrides((current) => ({ ...current, [engineName]: next }));
    try {
      const currentEngines = settings?.engines ?? {};
      const existing = normalizeEngineEntry(currentEngines[engineName]);
      const merged = {
        ...currentEngines,
        [engineName]: { ...existing, enabled: next },
      };
      await updateSettings({ engines: merged });
      await Promise.all([refetchSettings(), refetchEngines()]);
      setEngineOverrides((current) => {
        const rest = { ...current };
        delete rest[engineName];
        return rest;
      });
    } catch (err) {
      setEngineOverrides((current) => {
        const rest = { ...current };
        delete rest[engineName];
        return rest;
      });
      setAlert({
        title: t("engines.toggleFailed"),
        message: err instanceof Error ? err.message : "Unknown error",
      });
    } finally {
      setTogglingEngine(null);
    }
  };

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
            <Link to="/models" className="text-blue2 hover:underline">
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
        className="p-5 rounded-md bg-surface border border-border space-y-3"
      >
        <h3 className="font-semibold flex items-center gap-2">
          <Download size={16} className="text-blue2" />
          {t("engines.pullTitle")}
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-2">
          <Input
            mono
            aria-label={t("engines.refLabel")}
            placeholder={t("engines.refPlaceholder")}
            value={ref}
            onChange={(e) => setRef(e.target.value)}
          />
          <Button
            type="submit"
            variant="primary"
            icon={ArrowDownToLine}
            loading={!!pulling}
            disabled={!ref.trim()}
          >
            {t("engines.pull")}
          </Button>
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
            <ProgressRow
              key={job.id}
              data-testid={`pull-${job.id}`}
              title={job.ref}
              detail={`${job.status}${job.layers ? ` · ${job.layers} layers` : ""}${job.error ? ` · ${job.error}` : ""}`}
              job={job}
              progressLabel={`${job.ref} progress`}
              cancelLabel={`Cancel pull of ${job.ref}`}
              onCancel={ACTIVE_STATES.includes(job.status) ? () => doCancel(job.id) : undefined}
            />
          ))}
        </section>
      )}

      {loading && (
        <div className="flex justify-center py-16">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      )}
      <ErrorLine>{error}</ErrorLine>

      {rows.length > 0 && (
        <div className="rounded-md bg-surface border border-border overflow-x-auto">
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
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => toggleRow(row)}
                          aria-expanded={open}
                          // Two rows can carry the same engine name at different
                          // versions, so the reference is what distinguishes them
                          // — for a screen reader as much as for a test.
                          aria-label={`Details for ${image.ref}`}
                          className="flex items-center gap-2 text-left hover:text-blue2 transition-colors"
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
                                  enabled={engine ? effectiveEnabled(engine.engine) : true}
                                />
                              : <span className="text-text-muted">{t("engines.unmanaged")}</span>}
                            {(engine?.version || image.version) && (
                              <span className="text-xs text-text-muted font-mono">
                                v{engine?.version || image.version}
                              </span>
                            )}
                          </span>
                        </button>
                        {/* Enable/disable is a per-engine setting, not a
                            per-image one — two variants of the same engine
                            share this switch, so it lives beside the badge
                            rather than in the per-row action column. */}
                        {engine && (
                          <Toggle
                            on={effectiveEnabled(engine.engine)}
                            disabled={togglingEngine === engine.engine}
                            onChange={(next) => toggleEngineEnabled(engine.engine, next)}
                            label={t("engines.toggleLabel", { engine: engine.engine })}
                          />
                        )}
                      </div>
                    </td>
                    <td className="p-3 font-mono min-w-0 max-w-sm">
                      {/* A digest-pinned tag is 64 hex characters — rendered in
                          full it was pushing the table past the viewport. It is
                          shortened for display, but the full ref stays in the
                          DOM (for a screen reader and for copy) and on hover. */}
                      <div
                        className="flex items-center gap-1 min-w-0"
                        title={isDigestTag(image.tag) ? image.ref : undefined}
                        aria-hidden={isDigestTag(image.tag) || undefined}
                      >
                        <span className="truncate">{image.repository}</span>
                        <span className="text-text-muted shrink-0">
                          :{shortImageTag(image.tag)}
                        </span>
                      </div>
                      {isDigestTag(image.tag) && <span className="sr-only">{image.ref}</span>}
                    </td>
                    <td className="p-3 min-w-36">
                      <div className="flex flex-wrap items-center gap-1.5">
                        {image.present ? (
                          <span className="whitespace-nowrap text-success">{t("engines.present")}</span>
                        ) : (
                          <span className="whitespace-nowrap text-text-muted">{t("engines.notPulled")}</span>
                        )}
                        {engine?.available === false && (
                          <span className="whitespace-nowrap px-1.5 py-0.5 rounded text-xs bg-text-muted/10 text-text-muted border border-border">
                            {t("engines.unpublished")}
                          </span>
                        )}
                        {reason && (
                          <span className="whitespace-nowrap px-1.5 py-0.5 rounded text-xs bg-warning/10 text-warning border border-warning/30">
                            {reason}
                          </span>
                        )}
                      </div>
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
                          className="p-1.5 rounded-sm text-text-muted hover:text-blue2 hover:bg-primary/10"
                        >
                          <RefreshCw size={15} />
                        </button>
                      )}
                      {image.present && peers.length > 0 && (
                        <button
                          aria-label={`Copy ${image.ref} to every node`}
                          title={t("engines.syncTitle")}
                          onClick={() => doSync(image.ref)}
                          className="p-1.5 rounded-sm text-text-muted hover:text-blue2 hover:bg-primary/10"
                        >
                          <Server size={15} />
                        </button>
                      )}
                      {image.present && (
                        <button
                          aria-label={`Delete ${image.ref}`}
                          onClick={() => setDeleteTarget(row)}
                          className="p-1.5 rounded-sm text-text-muted hover:text-danger hover:bg-danger/10"
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
          <p className="text-muted flex items-center gap-1.5">
            <Spinner size="sm" />
            {t("engines.asking")}
          </p>
        )}
        {peerCount > 0 && presence && presence !== "loading" && (
          <ul className="space-y-1">
            {presence.nodes.map((node) => (
              <li key={node.node} className="flex items-center justify-between gap-3">
                <span className="font-mono">{node.node}</span>
                {node.error ? (
                  <NodeState state="unknown" label={t("engines.couldNotAsk")} title={node.error} />
                ) : node.present ? (
                  <NodeState
                    state={node.matches ? "ok" : "warn"}
                    label={node.matches ? t("engines.sameImage") : t("engines.differentImage")}
                  />
                ) : (
                  <NodeState state="unknown" label={t("engines.absent")} />
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

  return (
    <NodeScopedDialog
      verb="delete"
      title={t("engines.deleteTitle")}
      body={t("engines.deleteBody", { ref: row.image.ref, size: formatSize(row.image.size_bytes) })}
      legend={t("engines.alsoRemoveFrom")}
      nodes={peers}
      presence={presence}
      // Until a node has answered, the dialog offers every peer: this is a
      // reclaim-disk dialog, and leaving a node out because nobody asked is
      // how a cluster keeps a 26 GB image nobody can see.
      selectionWhenUnknown={peers}
      noteFor={(_node, holds) =>
        holds === false ? <span className="text-[13px] text-muted">{t("engines.notThere")}</span> : null
      }
      confirmLabel={t("engines.deleteConfirm")}
      onConfirm={(nodes) => onConfirm(nodes)}
      onClose={onClose}
    />
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

  return (
    <section className="p-5 rounded-md bg-surface border border-line space-y-4">
      <div className="flex items-center justify-between pb-3 border-b border-line">
        <h3 className="font-semibold">{t("engines.registry")}</h3>
        <Button size="sm" icon={RefreshCw} loading={refreshing} onClick={onRefreshIndex}>
          {t("engines.refreshIndex")}
        </Button>
      </div>

      <Field label={t("engines.defaultEngine")}>
        {(control) => (
          <Input
            {...control}
            mono
            type="text"
            value={form.default_engine ?? ""}
            onChange={(e) => setForm({ ...form, default_engine: e.target.value })}
            placeholder="vllm"
          />
        )}
      </Field>

      <Field label={t("engines.indexes")} hint={t("engines.indexesHelp")}>
        {(control) => (
          <Textarea
            {...control}
            mono
            rows={3}
            value={(form.engine_indexes ?? []).join("\n")}
            onChange={(e) =>
              setForm({
                ...form,
                engine_indexes: e.target.value.split("\n").map((l) => l.trim()).filter(Boolean),
              })
            }
          />
        )}
      </Field>

      <Field label={t("engines.indexTtl")}>
        {(control) => (
          <div className="flex items-center gap-2">
            <Input
              {...control}
              mono
              type="number"
              min="0"
              className="w-28"
              value={form.engine_index_cache_ttl_seconds ?? 3600}
              onChange={(e) =>
                setForm({ ...form, engine_index_cache_ttl_seconds: parseInt(e.target.value) || 0 })
              }
            />
            <span className="text-[14px] text-muted">{t("engines.seconds")}</span>
          </div>
        )}
      </Field>

      <Button variant="primary" loading={saving} onClick={save}>
        {saving ? t("common.saving") : saved ? t("common.saved") : t("engines.saveRegistry")}
      </Button>
    </section>
  );
}
