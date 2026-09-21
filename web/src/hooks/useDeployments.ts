/** One read of `/api/deployments`, for every page that shows runs.
 *
 * Two pages ask what is running: Runs, which is the list, and Recipes, which
 * only needs to badge the cards whose recipe is already serving. They used to
 * do it differently — Runs polled every ten seconds and refetched on two SSE
 * frames, Recipes fetched once and never looked again, and Fleet ran a third
 * poll at fifteen — so a recipe stayed marked "running" long after the run had
 * stopped, and three pages disagreed about the same list.
 *
 * What this settles:
 *
 * * **The stream drives it.** `/sse/events/deployments` already carries every
 *   lifecycle frame and the reconciler's own `deployment_sync`. A frame that
 *   can change a record re-reads the list; a log line does not.
 * * **The poll is a fallback, and only while something is live.** A page
 *   showing nothing but history has nothing that can change on its own, so it
 *   asks for nothing. That is the common case on an idle control plane.
 * * **The events are kept here too**, because the one connection that drives
 *   the refetch is the same one the expanded row reads its event list from.
 *   Opening a second `EventSource` for the same stream would double the work
 *   and still leave the two views able to disagree.
 */

import { useCallback, useEffect, useState } from "react";
import { fetchDeployments } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { useSSEConnection } from "@/hooks/useSSEConnection";
import { eventFromFrame } from "@/lib/operations";
import type { DeploymentEvent, DeploymentEventFrame } from "@/lib/operations";
import type { Deployment } from "@/lib/types";

/** How often the list is re-read while a run is live, in ms. */
export const RUNS_POLL_MS = 10000;

/** How many event frames the expanded row keeps. */
const EVENT_LIMIT = 100;

/** The states a run is *finished* in. Everything else is live.
 *
 * Stated this way round on purpose: a status this build has never heard of is
 * something still happening, not something silently dropped off the page. */
const FINISHED = new Set(["stopped", "error"]);

/** Whether a run is still going — what the Live tab holds. */
export function isLiveRun(run: Deployment): boolean {
  return !FINISHED.has(run.status);
}

/** Event types that can have changed a record, so the list is now stale.
 *
 * `deployment_sync` is the reconciler saying convergence moved; the rest are
 * the lifecycle. A `deployment_log` frame changes nothing about the list and
 * re-reading on it would turn a chatty engine into a request per line. */
const REFETCH_ON = new Set([
  "deployment_sync",
  "deployment_deleted",
  "deployment_planned",
  "deployment_starting",
  "deployment_started",
  "deployment_container_started",
  "deployment_ready",
  "deployment_serving",
  "deployment_stopped",
  "deployment_error",
]);

export interface UseDeploymentsResult {
  deployments: Deployment[] | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
  /** Newest first, capped. Only the frames this connection has seen. */
  events: DeploymentEvent[];
  /** Drop one run's events without touching the others. */
  clearEvents: (resource: string) => void;
}

export function useDeployments(): UseDeploymentsResult {
  const { data, loading, error, refetch } = useQuery(fetchDeployments);
  const [events, setEvents] = useState<DeploymentEvent[]>([]);

  const onMessage = useCallback(
    (_event: string, payload: unknown) => {
      if (!payload || typeof payload !== "object" || !("type" in payload)) return;
      const frame = payload as DeploymentEventFrame;
      setEvents((prev) => [eventFromFrame(frame), ...prev].slice(0, EVENT_LIMIT));
      if (REFETCH_ON.has(frame.type as string)) refetch();
    },
    [refetch],
  );

  useSSEConnection("/sse/events/deployments", onMessage);

  // The poll exists for what the stream cannot tell us: a connection that
  // dropped, a frame the backend does not emit. Nothing live means nothing can
  // change without a frame, so an idle control plane is asked nothing.
  const anyLive = (data ?? []).some(isLiveRun);
  useEffect(() => {
    if (!anyLive) return;
    const timer = setInterval(refetch, RUNS_POLL_MS);
    return () => clearInterval(timer);
  }, [anyLive, refetch]);

  const clearEvents = useCallback((resource: string) => {
    setEvents((prev) => prev.filter((e) => e.resource !== resource));
  }, []);

  return { deployments: data, loading, error, refetch, events, clearEvents };
}

export default useDeployments;
