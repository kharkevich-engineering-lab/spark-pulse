/** The pre-flight result, in the deploy preview.
 *
 * This is where an operator decides, so this is where the answer belongs.
 * `docs/cluster-agent-plan.md` section 8 asks for diagnostics rather than
 * mysteries, and the whole point of the panel is that a problem arrives with
 * its node and its remedy attached rather than as a colour.
 *
 * Three things are deliberate:
 *
 * * **Three verdicts, shown as three verdicts.** `blocked` and `slow` are not
 *   shades of the same red. Needing a 26 GB pull is a wait worth planning for;
 *   an unreachable node is a stop. A panel that renders both as "problems"
 *   teaches an operator to ignore it.
 * * **Passing checks are counted, not listed.** Nine green rows per node push
 *   the one row that matters off the screen. The count is there so the panel
 *   is visibly not empty when everything is fine.
 * * **Every listed row names its node and its remedy.** Never "docker
 *   missing" on its own.
 */

import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react";
import type { PreflightCheck, PreflightReport, PreflightVerdict } from "@/lib/types";
import { formatSize } from "@/lib/utils";

/** How each verdict reads, and the tone it is shown in. */
export const VERDICT_STYLE: Record<
  PreflightVerdict,
  { label: string; className: string }
> = {
  ready: { label: "Ready", className: "bg-success/20 text-success border-success/30" },
  slow: {
    label: "Ready, but slow",
    className: "bg-warning/20 text-warning border-warning/30",
  },
  blocked: {
    label: "Blocked",
    className: "bg-danger/20 text-danger border-danger/30",
  },
};

/** The checks worth a row: everything that did not pass, failures first.
 *
 * Order is the operator's order of work — what stops the deploy, then what
 * delays it, then what is merely worth knowing.
 */
export function checksToShow(report: PreflightReport): PreflightCheck[] {
  return [...report.blocking, ...report.delaying, ...report.advisories];
}

/** One line under the verdict: what it will cost, in the units time is in. */
export function describeCost(report: PreflightReport): string {
  if (report.verdict === "blocked") {
    const nodes = new Set(report.blocking.map((c) => c.node));
    return `${report.blocking.length} check${report.blocking.length === 1 ? "" : "s"} failed on ${[...nodes].join(", ")}`;
  }
  if (report.delaying.length === 0) return report.summary;
  const bytes = report.estimated_transfer_bytes;
  const moved = bytes > 0 ? formatSize(bytes) : "data of unreported size";
  const nodes = new Set(report.delaying.map((c) => c.node));
  return `${moved} has to transfer to ${[...nodes].join(", ")} before this starts`;
}

const STATUS_ICON = {
  fail: XCircle,
  warn: AlertTriangle,
  pass: CheckCircle2,
} as const;

const STATUS_TONE = {
  fail: "text-danger",
  warn: "text-warning",
  pass: "text-success",
} as const;

export default function PreflightPanel({ report }: { report: PreflightReport }) {
  const rows = checksToShow(report);
  const style = VERDICT_STYLE[report.verdict];

  return (
    <div className="space-y-2" data-testid="preflight">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs uppercase tracking-wide text-text-muted">Pre-flight</span>
        <span
          className={`px-2 py-0.5 rounded-full border text-xs font-medium ${style.className}`}
          data-testid="preflight-verdict"
        >
          {style.label}
        </span>
        <span className="text-xs text-text-muted" data-testid="preflight-summary">
          {describeCost(report)}
        </span>
      </div>

      <p className="text-xs text-text-muted">
        {report.counts.pass} check{report.counts.pass === 1 ? "" : "s"} passed across{" "}
        {report.nodes.length} node{report.nodes.length === 1 ? "" : "s"}
        {report.nodes.length > 0 ? ` (${report.nodes.map((n) => n.label).join(", ")})` : ""}.
      </p>

      {rows.length === 0 ? (
        <p className="flex items-center gap-1.5 text-xs text-success">
          <CheckCircle2 size={13} className="shrink-0" />
          Nothing to fix and nothing to download first.
        </p>
      ) : (
        <ul className="space-y-2" data-testid="preflight-checks">
          {rows.map((check) => {
            const Icon = STATUS_ICON[check.status];
            return (
              <li
                key={`${check.node_id}-${check.id}-${check.title}`}
                className="flex items-start gap-2 p-2 rounded-lg bg-surface border border-border"
                data-testid={`preflight-check-${check.status}`}
              >
                <Icon size={14} className={`shrink-0 mt-0.5 ${STATUS_TONE[check.status]}`} />
                <div className="min-w-0 space-y-0.5">
                  <p className="text-xs">
                    <span className="font-medium">{check.title}</span>
                    <span className="text-text-muted"> · </span>
                    <span className="font-mono text-text-muted">{check.node}</span>
                  </p>
                  <p className="text-xs text-text-muted">{check.observed}</p>
                  {check.remedy && (
                    <p className="flex items-start gap-1.5 text-xs text-text-muted">
                      <Info size={12} className="shrink-0 mt-0.5" />
                      <span>{check.remedy}</span>
                    </p>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
