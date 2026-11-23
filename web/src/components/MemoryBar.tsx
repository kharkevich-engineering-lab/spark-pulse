/** One node's memory in the deploy preview: who holds it, what the host keeps,
 *  what this run would claim and what is left. The bar is the picture; the
 *  legend under it carries every figure as text, so nothing rests on colour. */

import { useI18n, type Translator } from "@/lib/i18n";
import type { MemoryBudget } from "@/lib/types";
import { formatSize } from "@/lib/utils";

/** Held outside any run below this is the desktop's noise, not worth a segment. */
export const UNATTRIBUTED_MIN_BYTES = 2 * 1024 ** 3;

export interface MemorySegment {
  key: string;
  label: string;
  bytes: number;
  /** Tailwind background; `bg-danger` is reserved for overflow. */
  tone: string;
}

export interface MemoryLayout {
  segments: MemorySegment[];
  total: number;
  /** What the bar is scaled to: the node's total, or more when the claim spills over. */
  scale: number;
  free: number;
  overflow: number;
}

/** The bar's segments for one node, or null when the node's memory was not read. */
export function memoryLayout(entry: MemoryBudget, i18n: Translator): MemoryLayout | null {
  const total = entry.total_bytes;
  if (total === null || total <= 0) return null;
  const segments: MemorySegment[] = [];
  entry.holders.forEach((h) => {
    if (h.bytes) segments.push({ key: `h-${h.id}`, label: h.name, bytes: h.bytes, tone: "bg-muted" });
  });
  if (entry.reserve_bytes > 0) {
    segments.push({
      key: "reserve",
      label: i18n.t("memoryBudget.seg.reserve"),
      bytes: entry.reserve_bytes,
      tone: "bg-line-strong",
    });
  }
  if ((entry.unattributed_bytes ?? 0) >= UNATTRIBUTED_MIN_BYTES) {
    segments.push({
      key: "outside",
      label: i18n.t("memoryBudget.seg.outside"),
      bytes: entry.unattributed_bytes as number,
      tone: "bg-warn",
    });
  }
  const before = segments.reduce((sum, s) => sum + s.bytes, 0);
  const claim = entry.claim_bytes ?? 0;
  const overflow = Math.max(0, before + claim - total);
  const fitting = claim - overflow;
  if (fitting > 0) {
    segments.push({ key: "claim", label: i18n.t("memoryBudget.seg.claim"), bytes: fitting, tone: "bg-primary" });
  }
  if (overflow > 0) {
    segments.push({ key: "over", label: i18n.t("memoryBudget.seg.over"), bytes: overflow, tone: "bg-danger" });
  }
  const free = Math.max(0, total - before - claim);
  return { segments, total, scale: Math.max(total, before + claim), free, overflow };
}

export default function MemoryBar({ entry }: { entry: MemoryBudget }) {
  const i18n = useI18n();
  const layout = memoryLayout(entry, i18n);
  if (!layout) return null;
  const { segments, scale, free, total } = layout;
  const parts = [...segments.map((s) => `${s.label} ${formatSize(s.bytes)}`)];
  parts.push(`${i18n.t("memoryBudget.seg.free")} ${formatSize(free)}`);
  const summary = i18n.t("memoryBudget.barLabel", {
    node: entry.label,
    parts: `${parts.join(", ")} / ${formatSize(total)}`,
  });
  const pct = (bytes: number) => `${(bytes / scale) * 100}%`;
  return (
    <div className="space-y-1" data-testid="deploy-memory-bar">
      <div
        role="img"
        aria-label={summary}
        className="flex h-2.5 w-full overflow-hidden rounded-sm border border-line bg-surface"
      >
        {segments.map((s) => (
          <span key={s.key} className={`${s.tone} h-full`} style={{ width: pct(s.bytes) }} />
        ))}
      </div>
      <ul className="flex flex-wrap gap-x-3 gap-y-0.5 text-[12px] text-muted" aria-hidden="true">
        {[...segments, { key: "free", label: i18n.t("memoryBudget.seg.free"), bytes: free, tone: "bg-surface border border-line" }].map(
          (s) => (
            <li key={s.key} className="flex items-center gap-1" data-testid={`memory-seg-${s.key}`}>
              <span className={`inline-block h-2 w-2 rounded-sm ${s.tone}`} />
              {s.label} {formatSize(s.bytes)}
            </li>
          ),
        )}
      </ul>
    </div>
  );
}
