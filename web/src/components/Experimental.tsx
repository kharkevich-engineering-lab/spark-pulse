import { FlaskConical } from "lucide-react";
import { cn } from "@/lib/utils";

/** A small "experimental" chip, for nav entries and section headings.
 *
 * Kept deliberately quiet: it marks a feature as unproven without dressing it
 * up as an error, because the feature still works as far as it has been taken.
 */
export function ExperimentalBadge({ className, title }: { className?: string; title?: string }) {
  return (
    <span
      title={title ?? "Experimental: not yet verified on real hardware"}
      className={cn(
        "inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
        "bg-warning/15 text-warning",
        className,
      )}
    >
      exp
    </span>
  );
}

/** A page-level notice saying what is unproven and why.
 *
 * `reason` should say plainly what has and has not been exercised, so an
 * operator can judge the risk instead of guessing at the word "experimental".
 */
export function ExperimentalBanner({
  title = "Experimental feature",
  reason,
  className,
}: {
  title?: string;
  reason: string;
  className?: string;
}) {
  return (
    <div
      role="note"
      className={cn(
        "flex items-start gap-3 rounded-xl border border-warning/30 bg-warning/10 p-4",
        className,
      )}
    >
      <FlaskConical size={18} className="mt-0.5 shrink-0 text-warning" />
      <div className="min-w-0">
        <p className="text-sm font-semibold text-warning">{title}</p>
        <p className="mt-1 text-sm text-text-muted">{reason}</p>
      </div>
    </div>
  );
}
