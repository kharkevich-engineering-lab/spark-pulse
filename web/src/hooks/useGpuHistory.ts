/** The GPU readings that went past, per node and GPU, since the page opened.
 *
 * Every point was reported by the backend; none is interpolated, and a GPU
 * that stops reporting simply stops gaining points. Nothing stores it — the
 * engine metrics have their own window on the server; this is the fleet's.
 */

import { useEffect, useMemo, useState } from "react";
import { useT } from "@/lib/i18n";
import type { GPUStats, NodeStats } from "@/lib/types";
import type { HealthSeries } from "@/components/HealthHistoryChart";

interface GPUSample {
  t: number;
  utilization: number | null;
  temperature: number | null;
}

/** An hour of five-second frames. */
export const MAX_SAMPLES = 720;

/** Node first: a GPU UUID is unique per machine, and two Sparks can — and in
 *  simulation do — report the same one. `|` appears in neither half. */
export const seriesKey = (nodeId: string, gpu: GPUStats) => `${nodeId}|${gpu.uuid}`;

export function useGpuHistory(nodes: NodeStats[]): Record<string, HealthSeries[]> {
  const t = useT();
  const [history, setHistory] = useState<Record<string, GPUSample[]>>({});

  useEffect(() => {
    if (nodes.length === 0) return;
    const at = Date.now();
    setHistory((prev) => {
      const next = { ...prev };
      for (const node of nodes) {
        for (const gpu of node.gpu) {
          const key = seriesKey(node.id, gpu);
          next[key] = [...(next[key] ?? []), { t: at, utilization: gpu.utilization, temperature: gpu.temperature }].slice(
            -MAX_SAMPLES,
          );
        }
      }
      return next;
    });
  }, [nodes]);

  return useMemo(() => {
    const out: Record<string, HealthSeries[]> = {};
    for (const [key, samples] of Object.entries(history)) {
      const of = (pick: (s: GPUSample) => number | null) =>
        samples.filter((s) => pick(s) !== null).map((s) => ({ t: s.t, value: pick(s) as number }));
      out[key] = [
        { label: t("monitoring.gpuUtilization"), unit: "%", color: "var(--color-primary)", samples: of((s) => s.utilization) },
        { label: t("monitoring.temperature"), unit: "°C", color: "var(--color-warning)", samples: of((s) => s.temperature) },
      ];
    }
    return out;
  }, [history, t]);
}

export default useGpuHistory;
