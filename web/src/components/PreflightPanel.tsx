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
import { useT } from "@/lib/i18n";
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

/** The memory estimate, shown even when it passes.
 *
 * Counting passing checks rather than listing them is right for "docker is
 * installed": there is nothing to read. It is wrong for this one. "19.0 GB
 * needed, 96.7 GB free, 16.0 GB of it KV cache at 8,192 tokens" is the
 * sentence an operator wants *before* choosing a context length — and a
 * check that only ever speaks up in red never teaches anyone what the
 * headroom was on the deploys that worked.
 *
 * Only when there is a figure. The check also passes when it could not work
 * the fit out at all, and "could not tell" is a line for the API, not for the
 * panel where the decision is made.
 */
export function memoryLine(report: PreflightReport): string {
  const shown = new Set(checksToShow(report).map((c) => `${c.node_id}-${c.id}`));
  const estimate = report.checks.find(
    (c) =>
      c.id === "vram" &&
      c.detail?.total_bytes != null &&
      !shown.has(`${c.node_id}-${c.id}`),
  );
  return estimate ? estimate.observed : "";
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
  const t = useT();
  const rows = checksToShow(report);
  const memory = memoryLine(report);
  const style = VERDICT_STYLE[report.verdict];

  return (
    <div className="space-y-2" data-testid="preflight">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs uppercase tracking-wide text-text-muted">{t("preflight.label")}</span>
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

      {memory && (
        <p className="text-xs text-text-muted" data-testid="preflight-memory">
          {memory}
        </p>
      )}

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
