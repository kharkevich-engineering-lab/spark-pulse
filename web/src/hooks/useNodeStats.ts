/** Every node's own stats: one read of `/api/memory`, then the metrics stream.
 *
 * The overview's node cards and the Nodes page read the latest answer here;
 * `useGpuHistory` keeps the readings that went past.
 */

import { useEffect, useState } from "react";
import { connectMetricsStream, fetchMemory } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import type { MemoryResponse, NodeStats } from "@/lib/types";

export interface UseNodeStatsResult {
  nodes: NodeStats[];
  loading: boolean;
  error: string | null;
  /** Ask every node again, now — after a kill, say — rather than at the next frame. */
  refresh: () => void;
}

export function useNodeStats(): UseNodeStatsResult {
  const { data, loading, error, refetch } = useQuery(fetchMemory);
  const [live, setLive] = useState<MemoryResponse | null>(null);

  useEffect(
    () =>
      connectMetricsStream((event, payload) => {
        if (event === "metrics") setLive(payload as MemoryResponse);
      }),
    [],
  );

  const current = live ?? data;
  return {
    nodes: current?.nodes ?? [],
    loading: loading && !current,
    error: current ? null : error,
    refresh: () => {
      setLive(null);
      void refetch();
    },
  };
}

export default useNodeStats;
