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
 * The other half of the merge is per-node. Opening a row shows what the engine
 * is in a strip under it and asks each node what it holds, and both destructive verbs — delete, and
 * the copy that fills a disk — name the machines they touch before they touch
 * them. Pulling by reference is the page's primary action, in a dialog.
 */

import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { ArrowDownToLine, Copy, Layers, Trash2 } from "lucide-react";
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
import { useI18n, type Translator } from "@/lib/i18n";
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
  InfoTip,
  KeyValues,
  Modal,
  NodeScopedDialog,
  NodeState,
  ProgressRow,
  SearchInput,
  Section,
  Spinner,
  Table,
  Tabs,
  Td,
  Th,
  THead,
  Toggle,
  Toolbar,
  Tr,
  matchesQuery,
} from "@/ui";
import type { AlertTone } from "@/ui";
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
export function updateReason(image: ImageEntry, t: Translator["t"]): string {
  if (image.digest_drift) return t("engines.digestDrift");
  if (!image.present) return t("engines.notPulled");
  return "";
}

export type EngineFilter = "all" | "pulled" | "missing" | "update";

/** Does this image belong under a filter? */
export function inEngineFilter(image: ImageEntry, filter: EngineFilter): boolean {
  switch (filter) {
    case "pulled":
      return image.present;
    case "missing":
      return !image.present;
    case "update":
      return Boolean(image.digest_drift);
    default:
      return true;
  }
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
  /** The page header's Pull button opens the dialog this tab owns. */
  pullOpen?: boolean;
  onPullOpenChange?: (open: boolean) => void;
}

export default function EnginesTab({ images: imagesQuery, pullOpen = false, onPullOpenChange }: EnginesTabProps) {
  const { t } = useI18n();
  const { data: images, loading, error, refetch } = imagesQuery;
  const { data: engineData, refetch: refetchEngines } = useQuery(fetchEngines);
  const { data: nodes } = useQuery(fetchNodes);
  const { data: settings, refetch: refetchSettings } = useQuery(fetchSettings);
  const wide = useWideLayout();

  const [jobs, setJobs] = useState<ImagePullJob[]>([]);
  const [pulling, setPulling] = useState<string | null>(null);
  const [ref, setRef] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [presence, setPresence] = useState<Record<string, ImagePresence | "loading">>({});
  const [deleteTarget, setDeleteTarget] = useState<EngineRow | null>(null);
  const [syncTarget, setSyncTarget] = useState<EngineRow | null>(null);
  const [alert, setAlert] = useState<{ title: string; message: string; tone?: AlertTone } | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<EngineFilter>("all");
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
  const recent = useMemo(
    () => jobs.filter((j) => !ACTIVE_STATES.includes(j.status)).slice(0, 5),
    [jobs],
  );

  const counts = useMemo(() => {
    const all = images ?? [];
    return {
      all: all.length,
      pulled: all.filter((i) => inEngineFilter(i, "pulled")).length,
      missing: all.filter((i) => inEngineFilter(i, "missing")).length,
      update: all.filter((i) => inEngineFilter(i, "update")).length,
    };
  }, [images]);

  const shown = useMemo(
    () =>
      rows.filter(
        (row) =>
          inEngineFilter(row.image, filter) &&
          matchesQuery(query, row.image.ref, row.image.engine, row.image.variant, row.engine?.engine),
      ),
    [rows, filter, query],
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
      setAlert({ title: t("engines.lastEngineTitle"), message: t("engines.lastEngineMessage"), tone: "warning" });
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

  /** Open one image in the panel, asking the nodes about it once. */
  const selectRow = async (row: EngineRow) => {
    if (selected === row.key) {
      setSelected(null);
      return;
    }
    setSelected(row.key);
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
      onPullOpenChange?.(false);
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
          tone: "warning",
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

  /** The three verbs a row offers, as icon buttons that never wrap. */
  const actionsFor = (row: EngineRow) => {
    const { image } = row;
    const reason = updateReason(image, t);
    return (
      <>
        {reason && (
          <IconButton
            size="sm"
            icon={ArrowDownToLine}
            label={t("library.pullImage", { ref: image.ref })}
            loading={pulling === image.ref}
            onClick={() => pull(image.ref)}
          />
        )}
        {image.present && peers.length > 0 && (
          <IconButton
            size="sm"
            icon={Copy}
            label={t("library.copyImage", { ref: image.ref })}
            onClick={() => setSyncTarget(row)}
          />
        )}
        {image.present && (
          <IconButton
            size="sm"
            icon={Trash2}
            label={t("library.deleteImage", { ref: image.ref })}
            onClick={() => setDeleteTarget(row)}
          />
        )}
      </>
    );
  };

  /** The engine and its variant on one line — the cell that names a row. The
   *  button is what a keyboard user presses to open the panel. */
  const identityFor = (row: EngineRow) => {
    const { image, engine } = row;
    const on = engine ? effectiveEnabled(engine.engine) : true;
    return (
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          selectRow(row);
        }}
        aria-expanded={selected === row.key}
        // Two rows can carry the same engine name at different versions, so
        // the reference is what distinguishes them — for a screen reader as
        // much as for a test.
        aria-label={t("library.detailsFor", { ref: image.ref })}
        className={cn("whitespace-nowrap text-left font-semibold hover:text-blue2", !on && "text-muted")}
      >
        {/* The catalogue knows the engine name even for a version the
            registry no longer advertises — a second tag of an engine we have
            is still that engine, not an unmanaged image. Only something with
            no engine at all is unmanaged. */}
        {image.engine ? (
          <>
            {image.engine}
            {image.variant && image.variant !== "default" && (
              <span className="font-normal text-muted"> · {image.variant}</span>
            )}
          </>
        ) : (
          <span className="font-normal text-muted">{t("engines.unmanaged")}</span>
        )}
      </button>
    );
  };

  const toggleFor = (row: EngineRow) =>
    row.engine ? (
      <Toggle
        on={effectiveEnabled(row.engine.engine)}
        disabled={togglingEngine === row.engine.engine}
        onChange={(next) => toggleEngineEnabled(row.engine!.engine, next)}
        label={t("engines.toggleLabel", { engine: row.engine.engine })}
      />
    ) : null;

  /** Present or not, plus anything the status word does not already say. */
  /** One dot and a word, like every other status here. A republished digest
   *  is "Update available" in amber, and why is on the hover — the Pull
   *  button in the same row is what acts on it. */
  const statusFor = (row: EngineRow) => {
    const { image, engine } = row;
    const status = image.digest_drift ? (
      <NodeState state="warn" label={t("engines.statusUpdate")} title={t("engines.digestDriftTitle")} />
    ) : image.present ? (
      <NodeState state="ok" label={t("engines.present")} title={t("engines.presentTitle")} />
    ) : (
      <NodeState state="unknown" label={t("engines.notPulled")} title={t("engines.notPulledTitle")} />
    );
    return (
      <div className="flex items-center gap-2 whitespace-nowrap text-[12.5px]">
        {status}
        {engine?.available === false && <span className="text-muted">· {t("engines.unpublished")}</span>}
      </div>
    );
  };

  /** The repository can be long and is truncated; the tag never is. */
  const imageRefFor = (image: ImageEntry) => (
    <>
      <div
        className="flex min-w-0 items-center"
        title={image.ref}
        aria-hidden={isDigestTag(image.tag) || undefined}
      >
        <span className="min-w-0 truncate">{image.repository}</span>
        <span className="shrink-0 text-muted">:{shortImageTag(image.tag)}</span>
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
    <div className="space-y-4">
      {(active.length > 0 || recent.length > 0) && (
        <Section
          title={t("engines.pulls")}
          meta={connected ? t("engines.live") : t("engines.polling")}
          bodyClassName="space-y-2"
        >
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
        </Section>
      )}

      <div>
        <Toolbar>
          <SearchInput
            value={query}
            onChange={setQuery}
            label={t("engines.filterEngines")}
            clearLabel={t("common.clear")}
          />
          <Tabs
            label={t("engines.filterLabel")}
            value={filter}
            onChange={(id) => setFilter(id as EngineFilter)}
            tabs={[
              { id: "all", label: t("engines.filterAll"), count: counts.all },
              { id: "pulled", label: t("engines.filterPulled"), count: counts.pulled },
              { id: "missing", label: t("engines.filterMissing"), count: counts.missing },
              { id: "update", label: t("engines.filterUpdate"), count: counts.update },
            ]}
          />
          <InfoTip label={t("engines.aboutIndexes")} className="ml-auto">
            {t("library.enginesInSettings")}
          </InfoTip>
        </Toolbar>

        {loading && (
          <div className="flex justify-center py-16">
            <Spinner size="lg" label={t("common.loading")} />
          </div>
        )}
        <ErrorLine>{error}</ErrorLine>

        {rows.length > 0 && (
          <>
            <Section flush>
              {wide ? (
                <Table data-testid="engines-table">
                  <THead>
                    <Th>{t("engines.colEngine")}</Th>
                    <Th>{t("engines.colVersion")}</Th>
                    <Th>{t("engines.colImage")}</Th>
                    <Th>{t("engines.colStatus")}</Th>
                    <Th numeric>{t("engines.colSize")}</Th>
                    <Th>{t("engines.colEnabled")}</Th>
                    <Th srOnly>{t("engines.colActions")}</Th>
                  </THead>
                  <tbody>
                    {shown.map((row) => {
                      const { image, engine } = row;
                      const open = selected === row.key;
                      return (
                        <Fragment key={row.key}>
                          <Tr
                            data-testid={`engine-${image.ref}`}
                            interactive
                            selected={open}
                            onClick={() => selectRow(row)}
                          >
                            <Td>{identityFor(row)}</Td>
                            <Td mono muted className="whitespace-nowrap">
                              {engine?.version || image.version ? `v${engine?.version || image.version}` : "—"}
                            </Td>
                            <Td mono className="max-w-[300px]">{imageRefFor(image)}</Td>
                            <Td>{statusFor(row)}</Td>
                            <Td numeric>{image.present ? formatSize(image.size_bytes) : "—"}</Td>
                            <Td onClick={(e) => e.stopPropagation()}>{toggleFor(row)}</Td>
                            <Td actions>{actionsFor(row)}</Td>
                          </Tr>
                          {open && (
                            <DetailRow colSpan={7}>
                              <EngineDetail row={row} presence={presence[row.key]} peerCount={peers.length} digest={digestFor(image)} />
                            </DetailRow>
                          )}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </Table>
              ) : (
                <ul className="divide-y divide-line">
                  {shown.map((row) => {
                    const { image, engine } = row;
                    return (
                      <li key={row.key} data-testid={`engine-${image.ref}`} className="space-y-1.5 p-3">
                        <div className="flex items-center gap-2">
                          {identityFor(row)}
                          {(engine?.version || image.version) && (
                            <span className="font-mono text-[12.5px] text-muted">v{engine?.version || image.version}</span>
                          )}
                          <span className="ml-auto">{toggleFor(row)}</span>
                        </div>
                        <div className="font-mono text-[12.5px]">{imageRefFor(image)}</div>
                        <div className="flex items-center gap-2">
                          {statusFor(row)}
                          <span className="font-mono text-[12.5px] text-muted">
                            {image.present ? formatSize(image.size_bytes) : "—"}
                          </span>
                          <span className="ml-auto flex items-center gap-0.5">{actionsFor(row)}</span>
                        </div>
                        {selected === row.key && (
                          <div className="mt-2 border-t border-line pt-3">
                            <EngineDetail row={row} presence={presence[row.key]} peerCount={peers.length} digest={digestFor(image)} />
                          </div>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
              {shown.length === 0 && (
                <p className="px-3 py-8 text-center text-[13px] text-muted">{t("library.noMatch")}</p>
              )}
            </Section>
          </>
        )}

        {rows.length === 0 && !loading && <EmptyState icon={Layers}>{t("engines.empty")}</EmptyState>}
      </div>

      {pullOpen && (
        <Modal open onClose={() => onPullOpenChange?.(false)} title={t("engines.pullImage")} size="md">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              pull(ref);
            }}
            className="space-y-4"
          >
            <Field label={t("engines.refLabel")} hint={t("engines.refHint")}>
              {(control) => (
                <Input
                  {...control}
                  mono
                  autoFocus
                  placeholder={t("engines.refPlaceholder")}
                  value={ref}
                  onChange={(e) => setRef(e.target.value)}
                />
              )}
            </Field>
            <div className="flex justify-end gap-2">
              <Button type="button" onClick={() => onPullOpenChange?.(false)}>
                {t("common.cancel")}
              </Button>
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
        </Modal>
      )}

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
        <AlertModal open onClose={() => setAlert(null)} title={alert.title} message={alert.message} tone={alert.tone} />
      )}
    </div>
  );
}

/** What an engine is, and where its image is — the strip under an open row. */
function EngineDetail({
  row,
  presence,
  peerCount,
  digest,
}: {
  row: EngineRow;
  presence: ImagePresence | "loading" | undefined;
  peerCount: number;
  digest: React.ReactNode;
}) {
  const { t } = useI18n();
  const { engine, image } = row;
  const capabilities = engine
    ? Object.entries(engine.capabilities)
        .filter(([, on]) => on)
        .map(([name]) => name)
        .join(", ") || t("engines.noCapabilities")
    : null;

  return (
    <div className="space-y-3 text-[12.5px]" data-testid={`engine-detail-${image.ref}`}>
      <p className="text-[13px]">{engine?.description || image.description || t("engines.noDescription")}</p>
      <div className="flex flex-wrap gap-x-10 gap-y-3">
        <KeyValues
          items={[
            [t("engines.colVersion"), <span className="font-mono">{engine?.version || image.version || "—"}</span>],
            [t("engines.colSize"), <span className="font-mono">{image.present ? formatSize(image.size_bytes) : "—"}</span>],
            [t("engines.colDigest"), <span className="font-mono">{digest}</span>],
            ...(engine
              ? ([
                  [t("engines.capabilities"), capabilities],
                  [
                    t("engines.ports"),
                    <span className="font-mono">
                      :{engine.ports.api}
                      {engine.ports.rendezvous ? ` / :${engine.ports.rendezvous}` : ""}
                    </span>,
                  ],
                  [
                    t("engines.hardware"),
                    engine.verified.length > 0
                      ? t("engines.verifiedCount", { count: engine.verified.length })
                      : t("engines.neverVerified"),
                  ],
                  [t("engines.index"), t("engines.source", { source: engine.source })],
                ] as [React.ReactNode, React.ReactNode][])
              : []),
          ]}
        />

      <div className="min-w-[220px]">
        <p className="mb-1 text-[10.5px] font-semibold uppercase tracking-[0.08em] text-muted">{t("engines.onNodes")}</p>
        <div className="space-y-2" data-testid={`presence-${image.ref}`}>
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
