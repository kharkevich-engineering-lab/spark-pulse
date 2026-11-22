/** How one node's unified memory divides between the runs on it.
 *
 * A GB10 has one pool: the GPU's allocations are the host's. So the bar is
 * the host's total, and it is cut by what the node itself reported — each GPU
 * process's `used_memory`, attributed to a run when the control plane started
 * its container (`deployment`), and the rest of `used` as the system's.
 * Nothing is estimated; a process that names no run is "other", never a run
 * guessed from its name. Units are the protocol's megabytes throughout.
 */

import type { Deployment, NodeStats } from "@/lib/types";

export type SegmentKind = "run" | "other" | "system";

export interface MemorySegment {
  kind: SegmentKind;
  /** The deployment id for a run segment; empty otherwise. */
  id: string;
  /** The run's name for a run segment; empty otherwise (the caller words it). */
  label: string;
  mb: number;
  /** A CSS colour: a run's palette slot, or the system/other neutral. */
  color: string;
}

export interface MemoryLayout {
  totalMb: number;
  usedMb: number;
  freeMb: number;
  segments: MemorySegment[];
}

const RUN_COLORS = ["var(--run-1)", "var(--run-2)", "var(--run-3)", "var(--run-4)", "var(--run-5)"];

/** A colour per run that is the same on every node the run occupies.
 *
 * Keyed by the run's position in the list it is given — the page passes the
 * same list to every card — so a two-rank run is one colour across two bars. */
export function runColors(runs: Pick<Deployment, "id">[]): Map<string, string> {
  const colors = new Map<string, string>();
  runs.forEach((run, i) => colors.set(run.id, RUN_COLORS[i % RUN_COLORS.length]));
  return colors;
}

export function memoryLayout(
  node: NodeStats,
  runs: Pick<Deployment, "id" | "name">[],
  colors: Map<string, string>,
): MemoryLayout | null {
  const cpu = node.cpu;
  if (!node.reachable || !cpu || !(cpu.total > 0)) return null;

  const byRun = new Map<string, number>();
  let other = 0;
  for (const p of node.processes ?? []) {
    const mb = Number(p.used_memory) || 0;
    if (mb <= 0) continue;
    if (p.deployment) byRun.set(p.deployment, (byRun.get(p.deployment) ?? 0) + mb);
    else other += mb;
  }

  const names = new Map(runs.map((r) => [r.id, r.name]));
  const segments: MemorySegment[] = [];
  let attributed = 0;
  for (const [id, mb] of byRun) {
    attributed += mb;
    segments.push({
      kind: "run",
      id,
      label: names.get(id) ?? id.slice(0, 12),
      mb,
      color: colors.get(id) ?? RUN_COLORS[0],
    });
  }
  segments.sort((a, b) => b.mb - a.mb);
  if (other > 0) {
    attributed += other;
    segments.push({ kind: "other", id: "", label: "", mb: other, color: "var(--warn)" });
  }

  const used = Math.min(cpu.total, Math.max(cpu.used ?? 0, attributed));
  const system = Math.max(0, used - attributed);
  if (system > 0) segments.unshift({ kind: "system", id: "", label: "", mb: system, color: "var(--mem-system)" });

  return { totalMb: cpu.total, usedMb: used, freeMb: Math.max(0, cpu.total - used), segments };
}

/** Megabytes as the cards print them: GB with one decimal, or MB under one. */
export function formatMb(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}
