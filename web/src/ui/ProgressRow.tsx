/** One transfer in flight: what it is, how far it has got, how to stop it.
 *
 * Models and Engines had the same card twice — the same mono title, the same
 * status-and-detail line, the same `bytes done / bytes total`, the same 1.5px
 * bar, the same cancel button — differing only in the noun. They also had two
 * copies of `ACTIVE_STATES` and two `progressPercent`s that disagreed: one
 * measured bytes, the other read the percent the backend already sent. Both
 * are right for their job, so the shared one uses the percent when there is
 * one and the bytes when there is not.
 */

import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { formatSize } from "@/lib/utils";
import { IconButton } from "./Button";

/** A job that has not finished and can still be cancelled. */
export const ACTIVE_STATES = ["queued", "running"];

export interface ProgressJob {
  status: string;
  bytes_done?: number;
  bytes_total?: number | null;
  /** Some backends count layers rather than bytes and send this instead. */
  percent?: number | null;
}

export function isActive(job: { status: string }): boolean {
  return ACTIVE_STATES.includes(job.status);
}

export function progressPercent(job: ProgressJob): number {
  if (job.status === "completed") return 100;
  if (typeof job.percent === "number") return Math.max(0, Math.min(100, Math.round(job.percent)));
  if (!job.bytes_total) return 0;
  return Math.min(100, Math.round(((job.bytes_done ?? 0) / job.bytes_total) * 100));
}

/** "1.2 GB / 4.0 GB", or "1.2 GB / ?" while the total is still unknown. */
export function transferred(job: ProgressJob): string {
  return `${formatSize(job.bytes_done ?? 0)} / ${job.bytes_total ? formatSize(job.bytes_total) : "?"}`;
}

export interface ProgressRowProps {
  /** The thing being fetched — a model id, an image reference. */
  title: string;
  /** Status, current file, error: one quiet line under the title. */
  detail?: React.ReactNode;
  job: ProgressJob;
  /** Absent means this job cannot be cancelled. */
  onCancel?: () => void;
  cancelLabel?: string;
  /** Names the bar for a screen reader. */
  progressLabel: string;
  /** Anything the page hangs off this job — a scheduled deploy, say. */
  children?: React.ReactNode;
  "data-testid"?: string;
  className?: string;
}

export function ProgressRow({
  title,
  detail,
  job,
  onCancel,
  cancelLabel,
  progressLabel,
  children,
  className,
  ...rest
}: ProgressRowProps) {
  const percent = progressPercent(job);
  return (
    <div
      data-testid={rest["data-testid"]}
      className={cn("p-4 rounded-md bg-surface border border-line", className)}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="font-mono text-[13px] truncate">{title}</p>
          {detail && <p className="text-[13px] text-muted">{detail}</p>}
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <span className="text-[13px] font-mono text-muted">{transferred(job)}</span>
          {onCancel && (
            <IconButton
              size="sm"
              icon={X}
              label={cancelLabel ?? "Cancel"}
              onClick={onCancel}
              className="border-transparent text-muted hover:text-bad hover:border-line"
            />
          )}
        </div>
      </div>
      <div className="mt-2 h-1.5 rounded-full bg-bg2 overflow-hidden">
        <div
          role="progressbar"
          aria-label={progressLabel}
          aria-valuenow={percent}
          className="h-full bg-blue-cta transition-all"
          style={{ width: `${percent}%` }}
        />
      </div>
      {children}
    </div>
  );
}

export default ProgressRow;
