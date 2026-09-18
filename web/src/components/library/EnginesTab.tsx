/** Engines: what this cluster can run, and what it costs in disk.
 *
 * There used to be two answers to that one question. "Engines" was a Settings
 * tab listing what the registry knows about; "Images" was a page listing what
 * is on this host. They describe the same object — an engine *is* its image —
 * and an operator deciding whether to keep a 26 GB image had to read the
 * capability list on one page and the size on another.
 *
 * So: one row per engine, carrying both. Where engines *come from* — the
 * indexes, the cache lifetime, the default — is configuration, and lives with
 * the rest of it in Settings; this tab is what is here now.
 *
 * The other half of the merge is per-node. Expanding a row asks each node what
 * it holds, and both destructive verbs — delete, and the copy that fills a
 * disk — name the machines they touch before they touch them.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowDownToLine, ChevronDown, ChevronRight, Layers } from "lucide-react";
import {
  cancelImagePull,
  deleteImage,
  fetchEngines,
  fetchImagePresence,
  fetchImagePulls,
  fetchNodes,
  fetchSettings,
  startImagePull,
  syncImageToNodes,
  updateSettings,
} from "@/lib/api";
import { useQuery, type UseQueryResult } from "@/hooks/useQuery";
import { useWideLayout } from "@/hooks/useMediaQuery";
import { useSSEConnection } from "@/hooks/useSSEConnection";
import { SSEConnectionState } from "@/lib/operations";
import { useI18n } from "@/lib/i18n";
import { formatSize } from "@/lib/utils";
import {
  ACTIVE_STATES,
  AlertModal,
  Button,
  EmptyState,
  ErrorLine,
  Field,
  Input,
  NodeScopedDialog,
  NodeState,
  ProgressRow,
  Spinner,
  Toggle,
} from "@/ui";
import EngineBadge from "@/components/EngineBadge";
import type { EngineSummary, ImageEntry, ImagePresence, ImagePullJob } from "@/lib/types";

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

export interface EnginesTabProps {
  images: UseQueryResult<ImageEntry[]>;
}

export default function EnginesTab({ images: imagesQuery }: EnginesTabProps) {
  const { t } = useI18n();
  const { data: images, loading, error, refetch } = imagesQuery;
  const { data: engineData, refetch: refetchEngines } = useQuery(fetchEngines);
  const { data: nodes } = useQuery(fetchNodes);
  const { data: settings, refetch: refetchSettings } = useQuery(fetchSettings);
  const wide = useWideLayout();

  const [jobs, setJobs] = useState<ImagePullJob[]>([]);
  const [pulling, setPulling] = useState<string | null>(null);
  const [ref, setRef] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [presence, setPresence] = useState<Record<string, ImagePresence | "loading">>({});
  const [deleteTarget, setDeleteTarget] = useState<EngineRow | null>(null);
  const [syncTarget, setSyncTarget] = useState<EngineRow | null>(null);
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

  const rows = useMemo(() => joinEngines(images, engineData?.engines ?? null), [images, engineData]);
  const peers = useMemo(() => (nodes ?? []).filter((n) => !n.is_control_plane), [nodes]);
  const peerAddresses = useMemo(() => peers.map((n) => n.address), [peers]);
  const active = useMemo(() => jobs.filter((j) => ACTIVE_STATES.includes(j.status)), [jobs]);
  const needsAttention = useMemo(
    () => (images ?? []).filter((i) => i.update_available).length,
    [images],
  );
  const recent = useMemo(
    () => jobs.filter((j) => !ACTIVE_STATES.includes(j.status)).slice(0, 5),
    [jobs],
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
      const merged = { ...currentEngines, [engineName]: { ...existing, enabled: next } };
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
        message: err instanceof Error ? err.message : t("common.unknownError"),
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
      const answer = await fetchImagePresence(row.image.ref, peerAddresses);
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
      setAlert({
        title: t("engines.pullFailed"),
        message: err instanceof Error ? err.message : t("common.unknownError"),
      });
    } finally {
      setPulling(null);
    }
  };

  const forget = (key: string) =>
    setPresence((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });

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
      forget(target);
      refetch();
    } catch (err) {
      setAlert({
        title: t("engines.deleteFailed"),
        message: err instanceof Error ? err.message : t("common.unknownError"),
      });
    }
  };

  const doSync = async (target: string, onNodes: string[]) => {
    if (onNodes.length === 0) return;
    try {
      await syncImageToNodes(target, onNodes);
      forget(target);
    } catch (err) {
      setAlert({
        title: t("engines.syncFailed"),
        message: err instanceof Error ? err.message : t("common.unknownError"),
      });
    }
  };

  const doCancel = async (jobId: string) => {
    try {
      await cancelImagePull(jobId);
      reloadJobs();
    } catch (err) {
      setAlert({
        title: t("engines.cancelFailed"),
        message: err instanceof Error ? err.message : t("common.unknownError"),
      });
    }
  };

  /** The three verbs a row offers, as text links rather than a row of icons:
   *  the same set in the table and in the card. */
  const actionsFor = (row: EngineRow) => {
    const { image } = row;
    const reason = updateReason(image);
    return (
      <div className="flex flex-wrap items-center justify-end gap-x-3 gap-y-1 text-[13px] max-[899px]:justify-start">
        {reason && (
          <button
            type="button"
            aria-label={t("library.pullImage", { ref: image.ref })}
            onClick={() => pull(image.ref)}
            className="text-blue2 hover:underline"
          >
            {t("engines.pull")}
          </button>
        )}
        {image.present && peers.length > 0 && (
          <button
            type="button"
            aria-label={t("library.copyImage", { ref: image.ref })}
            title={t("engines.syncTitle")}
            onClick={() => setSyncTarget(row)}
            className="text-blue2 hover:underline"
          >
            {t("library.copy")}
          </button>
        )}
        {image.present && (
          <button
            type="button"
            aria-label={t("library.deleteImage", { ref: image.ref })}
            onClick={() => setDeleteTarget(row)}
            className="text-blue2 hover:underline"
          >
            {t("common.delete")}
          </button>
        )}
      </div>
    );
  };

  /** The engine badge, its version and its switch — the cell that identifies
   *  a row, in the table and in the card alike. */
  const identityFor = (row: EngineRow, open: boolean) => {
    const { image, engine } = row;
    return (
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => toggleRow(row)}
          aria-expanded={open}
          // Two rows can carry the same engine name at different versions, so
          // the reference is what distinguishes them — for a screen reader as
          // much as for a test.
          aria-label={t("library.detailsFor", { ref: image.ref })}
          className="flex items-center gap-2 text-left hover:text-blue2 transition-colors"
        >
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <span className="flex items-center gap-2">
            {/* The catalogue knows the engine name even for a version the
                registry no longer advertises — a second tag of an engine we
                have is still that engine, not an unmanaged image. Only
                something with no engine at all is unmanaged. */}
            {image.engine ? (
              <EngineBadge
                engine={image.engine}
                variant={image.variant}
                enabled={engine ? effectiveEnabled(engine.engine) : true}
              />
            ) : (
              <span className="text-muted">{t("engines.unmanaged")}</span>
            )}
            {(engine?.version || image.version) && (
              <span className="text-[13px] text-muted font-mono">
                v{engine?.version || image.version}
              </span>
            )}
          </span>
        </button>
        {/* Enable/disable is a per-engine setting, not a per-image one — two
            variants of the same engine share this switch, so it lives beside
            the badge rather than in the per-row action column. */}
        {engine && (
          <Toggle
            on={effectiveEnabled(engine.engine)}
            disabled={togglingEngine === engine.engine}
            onChange={(next) => toggleEngineEnabled(engine.engine, next)}
            label={t("engines.toggleLabel", { engine: engine.engine })}
          />
        )}
      </div>
    );
  };

  /** Present or not, plus anything the status word does not already say.
   *
   *  The chip used to repeat it: an image that was never pulled read "not
   *  pulled  not pulled", once as the status and once as the reason it wants
   *  attention, which is the same fact in two type sizes and two colours. The
   *  chip is for the reason the status cannot carry — a tag that now resolves
   *  to a different image than the one on disk. */
  const statusFor = (row: EngineRow) => {
    const { image, engine } = row;
    return (
      <div className="flex flex-wrap items-center gap-1.5 text-[13px]">
        {image.present ? (
          <span className="whitespace-nowrap text-good">{t("engines.present")}</span>
        ) : (
          <span className="whitespace-nowrap text-muted">{t("engines.notPulled")}</span>
        )}
        {engine?.available === false && (
          <span className="whitespace-nowrap px-1.5 py-0.5 rounded-sm border border-line text-muted">
            {t("engines.unpublished")}
          </span>
        )}
        {image.digest_drift && (
          <span className="whitespace-nowrap px-1.5 py-0.5 rounded-sm border border-warn text-warn">
            {updateReason(image)}
          </span>
        )}
      </div>
    );
  };

  const imageRefFor = (image: ImageEntry) => (
    <>
      {/* A digest-pinned tag is 64 hex characters — rendered in full it was
          pushing the table past the viewport. It is shortened for display, but
          the full ref stays in the DOM (for a screen reader and for copy) and
          on hover. */}
      <div
        className="flex items-center gap-1 min-w-0"
        title={isDigestTag(image.tag) ? image.ref : undefined}
        aria-hidden={isDigestTag(image.tag) || undefined}
      >
        <span className="truncate">{image.repository}</span>
        <span className="text-muted shrink-0">:{shortImageTag(image.tag)}</span>
      </div>
      {isDigestTag(image.tag) && <span className="sr-only">{image.ref}</span>}
    </>
  );

  const digestFor = (image: ImageEntry) => (
    <span title={image.local_digest || undefined}>
      {shortDigest(image.local_digest)}
      {image.digest_drift && (
        <>
          {" → "}
          <span className="text-warn" title={image.index_digest || undefined}>
            {shortDigest(image.index_digest)}
          </span>
        </>
      )}
    </span>
  );

  return (
    <div className="space-y-6">
      {/* Pull by reference. Kept from the Images page: an engine index is the
          usual way an image arrives, but not the only one. */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          pull(ref);
        }}
        className="grid grid-cols-1 gap-3 min-[520px]:grid-cols-[1fr_auto] min-[520px]:items-end"
      >
        <Field label={t("engines.refLabel")}>
          {(control) => (
            <Input
              {...control}
              mono
              placeholder={t("engines.refPlaceholder")}
              value={ref}
              onChange={(e) => setRef(e.target.value)}
            />
          )}
        </Field>
        <Button
          type="submit"
          variant="primary"
          icon={ArrowDownToLine}
          loading={!!pulling}
          disabled={!ref.trim()}
          className="max-[519px]:w-full min-[520px]:mb-[26px]"
        >
          {t("engines.pull")}
        </Button>
      </form>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {needsAttention > 0 && (
          <p className="text-[13px] text-warn">{t("engines.needAttention", { count: needsAttention })}</p>
        )}
        <p className="text-[13px] text-muted">{t("library.enginesInSettings")}</p>
      </div>

      {(active.length > 0 || recent.length > 0) && (
        <section className="space-y-2">
          <div className="flex items-center gap-2">
            <h3 className="text-[17px] font-semibold tracking-[-0.02em]">{t("engines.pulls")}</h3>
            <span className="text-[13px] text-muted">
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
              progressLabel={t("library.pullProgress", { ref: job.ref })}
              cancelLabel={t("library.cancelPull", { ref: job.ref })}
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

      {rows.length > 0 && wide && (
        <div className="rounded-md bg-surface border border-line overflow-x-auto">
          <table className="w-full text-[14px]">
            <thead>
              <tr className="text-left text-muted border-b border-line">
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
                const { image } = row;
                const open = expanded === row.key;
                return [
                  <tr
                    key={row.key}
                    data-testid={`engine-${image.ref}`}
                    className="border-b border-line last:border-0"
                  >
                    <td className="p-3">{identityFor(row, open)}</td>
                    <td className="p-3 font-mono min-w-0 max-w-[15rem]">{imageRefFor(image)}</td>
                    <td className="p-3">{statusFor(row)}</td>
                    <td className="p-3 font-mono">
                      {image.present ? formatSize(image.size_bytes) : "—"}
                    </td>
                    <td className="p-3 font-mono text-muted">{digestFor(image)}</td>
                    <td className="p-3">{actionsFor(row)}</td>
                  </tr>,
                  open && (
                    <tr key={`${row.key}-detail`} className="border-b border-line last:border-0 bg-bg2">
                      <td colSpan={6} className="p-4">
                        <EngineDetail row={row} presence={presence[row.key]} peerCount={peers.length} />
                      </td>
                    </tr>
                  ),
                ];
              })}
            </tbody>
          </table>
        </div>
      )}

      {rows.length > 0 && !wide && (
        <div className="space-y-3">
          {rows.map((row) => {
            const { image } = row;
            const open = expanded === row.key;
            return (
              <div
                key={row.key}
                data-testid={`engine-${image.ref}`}
                className="rounded-md bg-surface border border-line p-4 space-y-2"
              >
                {identityFor(row, open)}
                <p className="font-mono text-[13px] break-all">{imageRefFor(image)}</p>
                {statusFor(row)}
                <p className="font-mono text-[13px] text-muted">
                  {image.present ? formatSize(image.size_bytes) : "—"} · {digestFor(image)}
                </p>
                {actionsFor(row)}
                {open && (
                  <div className="pt-2 border-t border-line">
                    <EngineDetail row={row} presence={presence[row.key]} peerCount={peers.length} />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {rows.length === 0 && !loading && <EmptyState icon={Layers}>{t("engines.empty")}</EmptyState>}

      {deleteTarget && (
        <DeleteDialog
          row={deleteTarget}
          peers={peerAddresses}
          presence={presence[deleteTarget.key]}
          onClose={() => setDeleteTarget(null)}
          onConfirm={(onNodes) => {
            const target = deleteTarget.image.ref;
            setDeleteTarget(null);
            doDelete(target, onNodes);
          }}
        />
      )}

      {syncTarget && (
        <SyncDialog
          row={syncTarget}
          peers={peerAddresses}
          presence={presence[syncTarget.key]}
          onClose={() => setSyncTarget(null)}
          onConfirm={(onNodes) => {
            const target = syncTarget.image.ref;
            setSyncTarget(null);
            doSync(target, onNodes);
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
      <div className="space-y-2 text-[13px]">
        <p className="font-medium text-[14px]">
          {engine?.description || image.description || t("engines.noDescription")}
        </p>
        <p className="font-mono text-muted break-all">{image.ref}</p>
        {engine && (
          <>
            <p className="text-muted">
              {Object.entries(engine.capabilities)
                .filter(([, on]) => on)
                .map(([name]) => name)
                .join(", ") || t("engines.noCapabilities")}
              {" · "}
              <span className="font-mono">:{engine.ports.api}</span>
              {engine.ports.rendezvous ? (
                <span className="font-mono"> / :{engine.ports.rendezvous}</span>
              ) : null}
            </p>
            <p className="text-muted">
              {engine.verified.length > 0
                ? t("engines.verifiedCount", { count: engine.verified.length })
                : t("engines.neverVerified")}
              {" · "}
              {t("engines.source", { source: engine.source })}
            </p>
          </>
        )}
      </div>

      <div className="space-y-2 text-[13px]" data-testid={`presence-${image.ref}`}>
        <p className="font-medium text-[14px]">{t("engines.onNodes")}</p>
        {peerCount === 0 && <p className="text-muted">{t("engines.soloNode")}</p>}
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

/** Choosing where a copy lands, rather than filling every disk.
 *
 * "Copy to every registered node" was one click and no question — on a
 * four-node cluster that is 26 GB per machine, decided by a button whose
 * label said "every". It asks now, and it preselects the nodes that do not
 * already hold the image.
 */
export function SyncDialog({
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
      verb="replicate"
      title={t("library.syncTitle")}
      body={t("library.syncBody", { ref: row.image.ref, size: formatSize(row.image.size_bytes) })}
      legend={t("library.syncTo")}
      nodes={peers}
      presence={presence}
      selectionWhenUnknown={peers}
      requireSelection
      noteFor={(_node, holds) =>
        holds === true ? <span className="text-[13px] text-muted">{t("models.alreadyThere")}</span> : null
      }
      confirmLabel={t("library.syncConfirm")}
      onConfirm={(nodes) => onConfirm(nodes)}
      onClose={onClose}
    />
  );
}
