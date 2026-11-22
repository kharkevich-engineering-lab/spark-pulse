/** Overview: what the fleet is doing right now, on one screen.
 *
 * Three pages used to hold the answer — Runs for what is serving, Fleet →
 * Monitoring for how full each node is, and the node list for what is wrong —
 * and the one decision an operator makes most, "does this fit beside what is
 * running", needed all three. This page puts them in reading order: a card per
 * node with its memory cut by run, the live runs, what just happened, and any
 * fleet-level problem once at the top rather than repeated under every node.
 *
 * Everything here is a summary that links to the page that acts on it; the
 * only action of its own is Deploy, which is the command palette's.
 */

import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { AlertTriangle, Rocket, Terminal } from "lucide-react";
import { useI18n, useT } from "@/lib/i18n";

type T = (key: string, vars?: Record<string, string | number>) => string;
import { useQuery } from "@/hooks/useQuery";
import { useDeployments, isLiveRun } from "@/hooks/useDeployments";
import { useNodeStats } from "@/hooks/useNodeStats";
import { fetchDeploymentEvents, fetchNodeDiagnostics, fetchNodes } from "@/lib/api";
import { eventFromFrame, mergeEvents, type DeploymentEvent } from "@/lib/operations";
import { memoryLayout, runColors } from "@/lib/memoryLayout";
import { cn, formatDuration } from "@/lib/utils";
import { Button, EmptyState, ErrorLine, PageHeader, Spinner, StatusBadge } from "@/ui";
import NodeCard from "@/components/overview/NodeCard";
import { placement } from "@/components/RunRow";
import { openCommandPalette } from "@/components/CommandPalette";
import type { ClusterNode, Deployment, NodeFinding } from "@/lib/types";

/** How many events the activity column shows. */
const ACTIVITY_LIMIT = 10;
/** How many finished runs sit under the live ones. */
const RECENT_LIMIT = 5;
/** How many runs' stored timelines are read to seed it. */
const ACTIVITY_RUNS = 4;

function uptime(run: Deployment, now: number): string {
  const from = run.started_at || run.created_at;
  const start = from ? new Date(from).getTime() : NaN;
  if (!Number.isFinite(start)) return "—";
  return formatDuration(Math.max(0, Math.floor((now - start) / 1000)));
}

/** When a finished run ended, and how long it had lasted. */
function ending(run: Deployment): { at: string; lasted: string } {
  const end = run.stopped_at ? new Date(run.stopped_at).getTime() : NaN;
  const from = run.started_at || run.created_at;
  const start = from ? new Date(from).getTime() : NaN;
  const at = Number.isFinite(end)
    ? new Date(end).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
    : "—";
  const lasted = Number.isFinite(end) && Number.isFinite(start) ? formatDuration(Math.max(0, Math.floor((end - start) / 1000))) : "—";
  return { at, lasted };
}

function timeOf(iso: string): string {
  const d = new Date(iso);
  return Number.isFinite(d.getTime())
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
    : "";
}

/** A finding about several nodes is one line, naming them. */
function FleetFindings({ findings }: { findings: NodeFinding[] }) {
  const t = useT();
  const warnings = findings.filter((f) => f.severity === "warning");
  if (warnings.length === 0) return null;
  return (
    <div data-testid="fleet-findings" className="space-y-1.5">
      {warnings.map((f) => (
        <div
          key={f.code}
          role="note"
          className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px]"
        >
          <AlertTriangle size={15} className="shrink-0 text-warn" aria-hidden="true" />
          <span className="min-w-0 flex-1">{f.summary}</span>
          <Link to="/cluster" className="shrink-0 text-[13px]">
            {t("overview.howToFix")}
          </Link>
        </div>
      ))}
    </div>
  );
}

function LiveRuns({ runs, now, colors, t }: { runs: Deployment[]; now: number; colors: Map<string, string>; t: T }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[13px]" data-testid="overview-runs">
        <thead>
          <tr className="text-left text-[10.5px] uppercase tracking-[0.08em] text-muted">
            <th className="px-3 py-2 font-semibold">{t("overview.colRun")}</th>
            <th className="px-3 py-2 font-semibold">{t("overview.colPhase")}</th>
            <th className="px-3 py-2 font-semibold">{t("overview.colEngine")}</th>
            <th className="px-3 py-2 font-semibold">{t("overview.colNodes")}</th>
            <th className="px-3 py-2 font-semibold">{t("overview.colPort")}</th>
            <th className="px-3 py-2 font-semibold">{t("overview.colUp")}</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id} className="border-t border-line">
              <td className="max-w-[280px] truncate px-3 py-2">
                <span
                  aria-hidden="true"
                  className="mr-2 inline-block h-2.5 w-2.5 rounded-sm align-[-1px]"
                  style={{ background: colors.get(run.id) }}
                />
                <Link to={`/jobs/${encodeURIComponent(run.id)}`} className="font-semibold text-text hover:text-blue2">
                  {run.name}
                </Link>
              </td>
              <td className="whitespace-nowrap px-3 py-2">
                <StatusBadge
                  status={run.status}
                  sync={run.sync}
                  syncIntent={run.sync_intent}
                  phase={run.phase}
                  phaseVerified={run.phase_verified}
                  phaseReason={run.phase_reason}
                />
              </td>
              <td className="whitespace-nowrap px-3 py-2 text-muted">{run.engine || "—"}</td>
              <td className="whitespace-nowrap px-3 py-2 text-muted">{placement(run, t("runs.thisNode"))}</td>
              <td className="whitespace-nowrap px-3 py-2 font-mono tabular-nums">{run.port ? `:${run.port}` : "—"}</td>
              <td className="whitespace-nowrap px-3 py-2 font-mono tabular-nums text-muted">
                {run.status === "running" ? uptime(run, now) : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The runs that ended last: what was just there, so a quiet fleet still
 *  says what it was doing, and a run that failed is one click from its logs. */
function RecentRuns({ runs, t }: { runs: Deployment[]; t: T }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[13px]" data-testid="overview-recent">
        <thead>
          <tr className="text-left text-[10.5px] uppercase tracking-[0.08em] text-muted">
            <th className="px-3 py-2 font-semibold">{t("overview.colRun")}</th>
            <th className="px-3 py-2 font-semibold">{t("overview.colPhase")}</th>
            <th className="px-3 py-2 font-semibold">{t("overview.colEngine")}</th>
            <th className="px-3 py-2 font-semibold">{t("overview.colEnded")}</th>
            <th className="px-3 py-2 font-semibold">{t("overview.colLasted")}</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => {
            const { at, lasted } = ending(run);
            return (
              <tr key={run.id} className="border-t border-line">
                <td className="max-w-[280px] truncate px-3 py-2">
                  <Link to={`/jobs/${encodeURIComponent(run.id)}`} className="text-text hover:text-blue2">
                    {run.name}
                  </Link>
                </td>
                <td className="whitespace-nowrap px-3 py-2">
                  <StatusBadge
                    status={run.status}
                    sync={run.sync}
                    syncIntent={run.sync_intent}
                    phase={run.phase}
                    phaseVerified={run.phase_verified}
                    phaseReason={run.phase_reason}
                  />
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-muted">{run.engine || "—"}</td>
                <td className="whitespace-nowrap px-3 py-2 font-mono tabular-nums text-muted">{at}</td>
                <td className="whitespace-nowrap px-3 py-2 font-mono tabular-nums text-muted">{lasted}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Activity({ events, names, t }: { events: DeploymentEvent[]; names: Map<string, string>; t: T }) {
  if (events.length === 0) return <p className="px-3 py-3 text-[13px] text-muted">{t("overview.noActivity")}</p>;
  return (
    <ol className="divide-y divide-line" data-testid="overview-activity">
      {events.map((e) => (
        <li key={e.event_id} className="flex gap-3 px-3 py-1.5 text-[12.5px]">
          <span className="w-11 shrink-0 font-mono tabular-nums text-muted">{timeOf(e.timestamp)}</span>
          <span
            title={e.message}
            className={cn(
              "min-w-0 truncate",
              e.severity === "error" ? "text-bad" : e.severity === "warning" ? "text-warn" : undefined,
            )}
          >
            <span className="font-medium">{names.get(e.resource) ?? e.resource.slice(0, 12)}</span>{" "}
            <span className="text-muted">{e.message}</span>
          </span>
        </li>
      ))}
    </ol>
  );
}

function Panel({ title, meta, children }: { title: string; meta?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="min-w-0 overflow-hidden rounded-md border border-line bg-surface">
      <header className="flex items-center justify-between gap-3 border-b border-line px-3 py-2">
        <h2 className="text-[13px] font-semibold tracking-normal">{title}</h2>
        {meta && <span className="text-[12px] text-muted">{meta}</span>}
      </header>
      {children}
    </section>
  );
}

export default function OverviewPage() {
  const { t, plural } = useI18n();
  const navigate = useNavigate();
  const { deployments, loading: runsLoading, error: runsError, events: liveEvents } = useDeployments();
  const { nodes: stats, loading: statsLoading, error: statsError } = useNodeStats();
  const { data: registry } = useQuery<ClusterNode[]>(fetchNodes);
  const { data: diagnostics } = useQuery(fetchNodeDiagnostics);
  const [stored, setStored] = useState<DeploymentEvent[]>([]);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30000);
    return () => clearInterval(timer);
  }, []);

  const runs = useMemo(() => deployments ?? [], [deployments]);
  const live = useMemo(() => runs.filter(isLiveRun), [runs]);
  const recent = useMemo(
    () =>
      runs
        .filter((r) => !isLiveRun(r))
        .sort((a, b) => (b.stopped_at || b.created_at || "").localeCompare(a.stopped_at || a.created_at || ""))
        .slice(0, RECENT_LIMIT),
    [runs],
  );
  const colors = useMemo(() => runColors(live), [live]);
  const names = useMemo(() => new Map(runs.map((r) => [r.id, r.name])), [runs]);
  const byId = useMemo(() => new Map((registry ?? []).map((n) => [n.id, n])), [registry]);

  // Seed the activity column from the stored timelines of the most recent
  // runs, so a page opened after the fact is not empty; live frames merge on.
  const recentIds = useMemo(
    () =>
      [...runs]
        .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))
        .slice(0, ACTIVITY_RUNS)
        .map((r) => r.id)
        .join(","),
    [runs],
  );
  useEffect(() => {
    if (!recentIds) return;
    let cancelled = false;
    Promise.all(
      recentIds.split(",").map((id) =>
        fetchDeploymentEvents(id, { limit: ACTIVITY_LIMIT }).then(
          (page) => page.events.map(eventFromFrame),
          () => [] as DeploymentEvent[],
        ),
      ),
    ).then((lists) => {
      if (!cancelled) setStored(mergeEvents(...lists));
    });
    return () => {
      cancelled = true;
    };
  }, [recentIds]);

  const activity = useMemo(
    () => mergeEvents(liveEvents, stored).filter((e) => (e.event_type as string) !== "deployment_log").slice(0, ACTIVITY_LIMIT),
    [liveEvents, stored],
  );

  const running = live.filter((r) => r.status === "running").length;

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={t("nav.overview")}
        title={t("overview.heading")}
        description={
          stats.length > 0
            ? `${plural("fleet.nodes", stats.length)} · ${plural("overview.runningCount", running)}`
            : undefined
        }
        actions={
          <Button variant="primary" icon={Rocket} onClick={openCommandPalette}>
            {t("overview.deploy")}
          </Button>
        }
      />

      <FleetFindings findings={diagnostics?.findings ?? []} />

      {statsLoading ? (
        <div className="flex justify-center py-10">
          <Spinner label={t("common.loading")} />
        </div>
      ) : (
        <>
          <ErrorLine>{statsError}</ErrorLine>
          <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,340px),1fr))] gap-3">
            {stats.map((s) => (
              <NodeCard key={s.id} stats={s} node={byId.get(s.id)} layout={memoryLayout(s, live, colors)} />
            ))}
          </div>
        </>
      )}

      <div className="grid grid-cols-1 items-start gap-3 min-[1100px]:grid-cols-[minmax(0,1fr)_400px]">
        <div className="min-w-0 space-y-3">
        <Panel
          title={t("overview.liveRuns")}
          meta={<Link to="/jobs">{t("overview.allRuns")}</Link>}
        >
          {runsLoading && !deployments ? (
            <div className="flex justify-center py-8">
              <Spinner label={t("common.loading")} />
            </div>
          ) : runsError ? (
            <ErrorLine className="p-3">{runsError}</ErrorLine>
          ) : live.length === 0 ? (
            <EmptyState
              icon={Terminal}
              hint={t("overview.noRunsHint")}
              action={
                <Button size="sm" onClick={() => navigate("/recipes")}>
                  {t("overview.browseRecipes")}
                </Button>
              }
            >
              {t("overview.noRuns")}
            </EmptyState>
          ) : (
            <LiveRuns runs={live} now={now} colors={colors} t={t} />
          )}
        </Panel>
        {recent.length > 0 && (
          <Panel title={t("overview.recentRuns")} meta={<Link to="/jobs">{t("overview.allRuns")}</Link>}>
            <RecentRuns runs={recent} t={t} />
          </Panel>
        )}
        </div>
        <Panel title={t("overview.activity")}>
          <Activity events={activity} names={names} t={t} />
        </Panel>
      </div>
    </div>
  );
}
