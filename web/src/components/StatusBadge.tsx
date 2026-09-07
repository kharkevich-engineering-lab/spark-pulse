/** What a resource is, and — separately — whether it has settled.
 *
 * `status` is the lifecycle: running, pulling, stopped, error. `sync` is
 * convergence: the control plane recorded an intent and the nodes have not
 * caught up yet. They are different questions and an operator reads both.
 * *Running · deleting* is a real situation, and collapsing it into one word is
 * how a page comes to say "stopped" about a container still holding 90 GB.
 *
 * The sync chip appears only when there is something to say. A settled
 * deployment shows its status alone, which is what every caller had before
 * this existed.
 */

import { Loader2 } from "lucide-react";

interface StatusBadgeProps {
  status: string;
  /** `in_sync`, `in_progress`, `deleting`, `unknown`, or absent. */
  sync?: string;
  /** Why it has not settled, shown on hover. */
  syncReason?: string;
}

const COLORS: Record<string, string> = {
  running: "bg-success/20 text-success border-success/30",
  stopped: "bg-text-muted/10 text-text-muted border-text-muted/30",
  error: "bg-danger/20 text-danger border-danger/30",
  pending: "bg-warning/20 text-warning border-warning/30",
  // A pull is work in progress, not a stopped deployment. Without this it fell
  // through to the grey default and read as though nothing was happening.
  pulling: "bg-warning/20 text-warning border-warning/30",
};

/** How each convergence state reads, and whether it is still moving. */
const SYNC: Record<string, { label: string; className: string; busy: boolean }> = {
  in_progress: {
    label: "in progress",
    className: "bg-primary/15 text-primary border-primary/30",
    busy: true,
  },
  deleting: {
    label: "deleting",
    className: "bg-danger/15 text-danger border-danger/30",
    busy: true,
  },
  unknown: {
    label: "unverified",
    className: "bg-warning/15 text-warning border-warning/30",
    busy: false,
  },
};

/** Whether this resource is mid-change, so callers can disable their actions. */
export function isSettling(sync?: string): boolean {
  return sync === "in_progress" || sync === "deleting";
}

export default function StatusBadge({ status, sync, syncReason }: StatusBadgeProps) {
  const c = COLORS[status.toLowerCase()] || COLORS.stopped;
  const converging = sync ? SYNC[sync] : undefined;

  return (
    <span className="inline-flex items-center gap-1.5 flex-wrap">
      <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border ${c}`}>
        <span
          className="w-1.5 h-1.5 rounded-full bg-current"
          style={{ animation: status === "running" ? "pulse 2s infinite" : "none" }}
        />
        {status.charAt(0).toUpperCase() + status.slice(1)}
      </span>
      {converging && (
        <span
          title={syncReason || undefined}
          data-testid={`sync-${sync}`}
          className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border ${converging.className}`}
        >
          {converging.busy && <Loader2 size={10} className="animate-spin" />}
          {converging.label}
        </span>
      )}
    </span>
  );
}
