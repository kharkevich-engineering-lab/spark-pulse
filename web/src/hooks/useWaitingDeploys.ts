/** Deploys waiting on a model download, with that download's progress.
 *
 * "Download and deploy" records a deployment to run when the model lands, and
 * until it does there is no run: `/api/deployments` knows nothing about it.
 * Runs said nothing, the recipe looked untouched, and an operator who had just
 * clicked was left thinking nothing happened while 5 GB downloaded on another
 * page. So a waiting deploy is read here — the scheduled entry and its
 * download job, joined — and shown where a run would be.
 *
 * Polled only while something is waiting, the same rule `useDeployments`
 * keeps: an idle control plane is asked nothing. A page that has just
 * scheduled one calls `refetch` so it appears at once.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchModelDownloads, fetchScheduledDeploys } from "@/lib/api";
import type { ModelDownloadJob, ScheduledDeploy } from "@/lib/types";

/** How often a waiting deploy's download is re-read. */
export const WAITING_POLL_MS = 3000;

export interface WaitingDeploy {
  entry: ScheduledDeploy;
  /** The download it waits on, when the control plane still has the job. */
  download: ModelDownloadJob | null;
}

export function useWaitingDeploys() {
  const [waiting, setWaiting] = useState<WaitingDeploy[]>([]);
  const mounted = useRef(true);

  const refetch = useCallback(async () => {
    try {
      // `active_only`: waiting and deploying. A settled one is a run by now,
      // or a failure the Models page explains.
      const [entries, jobs] = await Promise.all([fetchScheduledDeploys(true), fetchModelDownloads()]);
      if (!mounted.current) return;
      const byId = new Map(jobs.map((j) => [j.id, j]));
      setWaiting(entries.map((entry) => ({ entry, download: byId.get(entry.download_job_id) ?? null })));
    } catch {
      // A failed read keeps what was shown; the next poll tries again.
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void refetch();
    return () => {
      mounted.current = false;
    };
  }, [refetch]);

  const any = waiting.length > 0;
  useEffect(() => {
    if (!any) return;
    const timer = setInterval(() => void refetch(), WAITING_POLL_MS);
    return () => clearInterval(timer);
  }, [any, refetch]);

  return { waiting, refetch };
}

/** How far the download has got, 0–100, or null while its size is unknown. */
export function downloadPercent(job: ModelDownloadJob | null): number | null {
  if (!job) return null;
  if (job.status === "completed") return 100;
  if (!job.bytes_total) return null;
  return Math.min(100, Math.round(((job.bytes_done ?? 0) / job.bytes_total) * 100));
}
