/** What a resource is, and — separately — whether it has settled.
 *
 * `status` is the lifecycle: running, pulling, stopped, error. `sync` is
 * convergence: the control plane recorded an intent and the nodes have not
 * caught up yet. They are different questions and an operator reads both.
 * *Running · deleting* is a real situation, and collapsing it into one word is
 * how a page comes to say "stopped" about a container still holding 90 GB.
 *
 * A dot and a word, per the brief — the coloured pill it used to draw made
 * every row in a table look like a warning. Colour is the status vocabulary:
 * good, warn, bad, muted, and nothing else.
 *
 * The text goes through `t()`. It used to capitalise whatever the API sent,
 * which meant the French page said "Running". A status this build has never
 * heard of still names itself, capitalised, rather than rendering blank or
 * showing a translation key: a new backend state must read as itself.
 */

import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export type StatusTone = "good" | "warn" | "bad" | "muted";

export const TONE_TEXT: Record<StatusTone, string> = {
  good: "text-good",
  warn: "text-warn",
  bad: "text-bad",
  muted: "text-muted",
};

/** Every lifecycle state the API emits, and how it reads. */
const STATUS_TONE: Record<string, StatusTone> = {
  running: "good",
  ready: "good",
  pulling: "warn",
  starting: "warn",
  pending: "warn",
  error: "bad",
  stopped: "muted",
  unknown: "muted",
};

/** Convergence. `in_sync` is absent on purpose: a settled record says nothing
 *  extra, which is what every caller had before this existed. */
const SYNC_TONE: Record<string, { tone: StatusTone; key: string }> = {
  in_progress: { tone: "warn", key: "status.in_progress" },
  deleting: { tone: "bad", key: "status.deleting" },
  // A node that could not be asked has not said no.
  unknown: { tone: "warn", key: "status.unverified" },
};

/** Whether this resource is mid-change, so callers can disable actions that
 *  would only ask again. */
export function isSettling(sync?: string): boolean {
  return sync === "in_progress" || sync === "deleting";
}

/** The tone for a lifecycle state, for a caller that needs the colour alone. */
export function statusTone(status: string): StatusTone {
  return STATUS_TONE[status.toLowerCase()] ?? "muted";
}

export interface StatusBadgeProps {
  status: string;
  /** `in_sync`, `in_progress`, `deleting`, `unknown`, or absent. */
  sync?: string;
  /** Why it has not settled, shown on hover. */
  syncReason?: string;
  className?: string;
}

function Dot({ pulse }: { pulse: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={cn("w-[7px] h-[7px] rounded-full bg-current shrink-0", pulse && "animate-pulse")}
    />
  );
}

export function StatusBadge({ status, sync, syncReason, className }: StatusBadgeProps) {
  const t = useT();
  const key = status.toLowerCase();
  const tone = STATUS_TONE[key];
  // Untranslated on purpose: a state this build has never heard of has no key
  // to look up, and `status.quiescing` on screen would be worse than
  // "Quiescing".
  const label = tone ? t(`status.${key}`) : status.charAt(0).toUpperCase() + status.slice(1);
  const converging = sync ? SYNC_TONE[sync] : undefined;

  return (
    <span className={cn("inline-flex items-center gap-2 flex-wrap", className)}>
      <span
        className={cn(
          "inline-flex items-center gap-1.5 text-[13px] font-medium",
          TONE_TEXT[tone ?? "muted"],
        )}
      >
        <Dot pulse={key === "running"} />
        {label}
      </span>
      {converging && (
        <span
          title={syncReason || undefined}
          data-testid={`sync-${sync}`}
          className={cn(
            "inline-flex items-center gap-1.5 text-[13px] font-medium",
            TONE_TEXT[converging.tone],
          )}
        >
          <Dot pulse={true} />
          {t(converging.key)}
        </span>
      )}
    </span>
  );
}

export default StatusBadge;
