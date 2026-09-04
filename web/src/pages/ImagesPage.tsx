/** Engine images — what is on this host, what a deploy would have to pull.
 *
 * Two failures from the first native deploy on hardware drive this page. An
 * image the host lacks used to download silently for tens of minutes behind a
 * deploy; and republishing an engine version changes its digest, so a host that
 * pulled a version yesterday can be running something else today. Both are
 * shown here as an "update available" marker with a one-click pull.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertCircle, ArrowDownToLine, Download, Layers, Loader2, RefreshCw, Trash2, X } from "lucide-react";
import {
  cancelImagePull,
  deleteImage,
  fetchImagePulls,
  fetchImages,
  startImagePull,
} from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { useSSEConnection } from "@/hooks/useSSEConnection";
import { SSEConnectionState } from "@/lib/operations";
import { formatSize } from "@/lib/utils";
import { setRefresh } from "@/lib/refresh";
import { AlertModal, ConfirmModal } from "@/components/Modal";
import type { ImageEntry, ImagePullJob } from "@/lib/types";

const ACTIVE_STATES = ["queued", "running"];

/** Shape of a DeploymentEvent frame as emitted by /sse/images. */
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

export default function ImagesPage() {
  const { data: images, loading, error, refetch } = useQuery(fetchImages);
  const [jobs, setJobs] = useState<ImagePullJob[]>([]);
  const [pulling, setPulling] = useState<string | null>(null);
  const [ref, setRef] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);

  useEffect(() => {
    setRefresh(refetch);
  }, [refetch]);

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

  const active = useMemo(() => jobs.filter((j) => ACTIVE_STATES.includes(j.status)), [jobs]);
  const recent = useMemo(
    () => jobs.filter((j) => !ACTIVE_STATES.includes(j.status)).slice(0, 5),
    [jobs],
  );
  const needsAttention = useMemo(
    () => (images ?? []).filter((i) => i.update_available).length,
    [images],
  );
  const onDisk = useMemo(
    () => (images ?? []).reduce((sum, i) => sum + (i.present ? i.size_bytes : 0), 0),
    [images],
  );

  const pull = async (target: string) => {
    if (!target.trim()) return;
    setPulling(target);
    try {
      const job = await startImagePull(target.trim());
      setJobs((current) => (current.some((j) => j.id === job.id) ? current : [job, ...current]));
      setRef("");
    } catch (err) {
      setAlert({ title: "Pull failed", message: err instanceof Error ? err.message : "Unknown error" });
    } finally {
      setPulling(null);
    }
  };

  const doDelete = async (target: string) => {
    try {
      await deleteImage(target);
      refetch();
    } catch (err) {
      setAlert({ title: "Delete failed", message: err instanceof Error ? err.message : "Unknown error" });
    }
  };

  const doCancel = async (jobId: string) => {
    try {
      await cancelImagePull(jobId);
      reloadJobs();
    } catch (err) {
      setAlert({ title: "Cancel failed", message: err instanceof Error ? err.message : "Unknown error" });
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-2xl font-bold">Engine images</h2>
          <p className="text-text-muted mt-1">
            What this host can deploy without waiting for a download.{" "}
            <Link to="/models" className="text-primary hover:underline">
              Models
            </Link>
          </p>
        </div>
        <div className="text-right">
          <p className="text-xs text-text-muted uppercase tracking-wide">On disk</p>
          <p className="text-2xl font-bold">{formatSize(onDisk)}</p>
          {needsAttention > 0 && (
            <p className="text-xs text-warning mt-1">{needsAttention} need attention</p>
          )}
        </div>
      </div>

      {/* Pull by ref */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          pull(ref);
        }}
        className="p-5 rounded-xl bg-surface border border-border space-y-3"
      >
        <h3 className="font-semibold flex items-center gap-2">
          <Download size={16} className="text-primary" />
          Pull image
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-2">
          <input
            aria-label="Image reference"
            placeholder="ghcr.io/org/engine:0.1.0 or repo@sha256:…"
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
            Pull
          </button>
        </div>
      </form>

      {/* Pull jobs */}
      <section className="space-y-2">
        <div className="flex items-center gap-2">
          <h3 className="font-semibold">Pulls</h3>
          <span className={connected ? "text-xs text-success" : "text-xs text-text-muted"}>
            {connected ? "live" : "polling"}
          </span>
        </div>
        {active.length === 0 && recent.length === 0 && (
          <p className="text-sm text-text-muted">No pulls yet.</p>
        )}
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

      {images && images.length > 0 && (
        <div className="rounded-xl bg-surface border border-border overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-text-muted border-b border-border">
                <th className="p-3 font-medium">Image</th>
                <th className="p-3 font-medium">Engine</th>
                <th className="p-3 font-medium">Status</th>
                <th className="p-3 font-medium">Size</th>
                <th className="p-3 font-medium">Digest</th>
                <th className="p-3 font-medium sr-only">Actions</th>
              </tr>
            </thead>
            <tbody>
              {images.map((image) => {
                const reason = updateReason(image);
                return (
                  <tr key={image.ref} data-testid={`image-${image.ref}`} className="border-b border-border last:border-0 hover:bg-surface-hover">
                    <td className="p-3 font-mono">
                      <span className="truncate">{image.repository}</span>
                      <span className="text-text-muted">:{image.tag}</span>
                    </td>
                    <td className="p-3">
                      {image.engine || "—"}
                      {image.variant ? <span className="text-text-muted">/{image.variant}</span> : null}
                    </td>
                    <td className="p-3">
                      {image.present ? (
                        <span className="text-success">present</span>
                      ) : (
                        <span className="text-text-muted">not pulled</span>
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
                      {image.present && (
                        <button
                          aria-label={`Delete ${image.ref}`}
                          onClick={() => setDeleteTarget(image.ref)}
                          className="p-1.5 rounded-lg text-text-muted hover:text-danger hover:bg-danger/10"
                        >
                          <Trash2 size={15} />
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {images && images.length === 0 && !loading && (
        <div className="text-center py-16 text-text-muted">
          <Layers size={40} className="mx-auto mb-4 opacity-50" />
          <p>No engine images known yet.</p>
        </div>
      )}

      {deleteTarget && (
        <ConfirmModal
          open={!!deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onConfirm={() => {
            doDelete(deleteTarget);
            setDeleteTarget(null);
          }}
          title="Delete image"
          message={`Delete "${deleteTarget}" from this host? Re-pulling it can take tens of minutes.`}
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
