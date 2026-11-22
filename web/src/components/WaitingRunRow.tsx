/** A deploy that is waiting on its model download, drawn where its run will be.
 *
 * Not a `RunRow`: there is no run yet — no containers, no logs, nothing to
 * benchmark — and pretending otherwise would put buttons on the row that do
 * nothing. What it has is a download: how far it has got, where to watch it,
 * and a way to call the deploy off.
 */

import { Link } from "react-router-dom";
import { X } from "lucide-react";
import { useT } from "@/lib/i18n";
import { formatSize } from "@/lib/utils";
import { Button } from "@/ui";
import { downloadPercent, type WaitingDeploy } from "@/hooks/useWaitingDeploys";

export interface WaitingRunRowProps {
  waiting: WaitingDeploy;
  onCancel: () => void;
}

export default function WaitingRunRow({ waiting, onCancel }: WaitingRunRowProps) {
  const t = useT();
  const { entry, download } = waiting;
  const percent = downloadPercent(download);
  const deploying = entry.status === "deploying";

  const progress = deploying
    ? t("runs.waitingDeploying")
    : download && download.status === "queued"
      ? t("runs.waitingQueued")
      : percent != null
        ? t("runs.waitingForModel", {
            percent,
            done: formatSize(download?.bytes_done ?? 0),
            total: formatSize(download?.bytes_total ?? 0),
          })
        : t("runs.waitingForModelUnknown");

  return (
    <div
      data-testid={`waiting-${entry.id}`}
      className="overflow-hidden rounded-md border border-dashed border-line bg-surface"
    >
      <div className="flex flex-col gap-4 p-4 min-[900px]:flex-row min-[900px]:items-center min-[900px]:gap-6">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <span className="max-w-full truncate text-[17px] font-semibold tracking-[-0.02em]">{entry.name}</span>
            <span className="inline-flex items-center gap-2 text-[13px] text-blue2">
              <span aria-hidden="true" className="h-[7px] w-[7px] shrink-0 animate-pulse rounded-full bg-current" />
              {deploying ? t("runs.deployingStatus") : t("runs.waitingStatus")}
            </span>
          </div>
          <p className="mt-2 text-[13px] text-muted" data-testid={`waiting-progress-${entry.id}`}>
            {progress}
            {" · "}
            <Link to="/models" className="text-blue2 hover:underline">
              {t("runs.waitingWatchDownload")}
            </Link>
          </p>
          <p className="mt-1 truncate font-mono text-[13px] text-muted" title={entry.model}>
            {entry.model}
          </p>
          {percent != null && !deploying && (
            <div className="mt-2 h-1.5 rounded-full bg-bg2 overflow-hidden">
              <div
                role="progressbar"
                aria-label={t("runs.waitingProgressLabel", { name: entry.name })}
                aria-valuenow={percent}
                className="h-full bg-blue-cta transition-all"
                style={{ width: `${percent}%` }}
              />
            </div>
          )}
        </div>
        {!deploying && (
          <div className="shrink-0">
            <Button size="sm" icon={X} onClick={onCancel} aria-label={t("runs.cancelWaiting", { name: entry.name })}>
              {t("runs.cancel")}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
