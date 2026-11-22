/** Runs: one list of everything this control plane has been asked to serve.
 *
 * It was three lists. Inference held the deployments with their logs; Cluster
 * held a second table of the live ones with their placement, on its own
 * fifteen-second poll; Benchmarking held a third that was really a fourth
 * thing — the measurements — behind a nav entry of its own and a launcher
 * whose first field asked the operator to type a deployment id by hand. Three
 * pages read the same endpoint, disagreed about what it said, and none of them
 * answered "what is serving, and is it fast".
 *
 * So: one page, three pills. **Live** is what is still going, **Finished** is
 * the history a stopped run becomes, and **Benchmarks** is the record of what
 * was measured — a tab rather than a destination, because a benchmark is
 * something you do to a run. `/benchmarking` still answers; it opens this page
 * on that tab.
 *
 * The list is `RunsTable` on a wide screen and `RunRow`'s cards under it,
 * full width either way. A run opens on its own page, `/jobs/:id`
 * (`RunDetailPage`), where the log stream, the per-rank read, the engine's
 * metrics window and the event timeline are.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Navigate, useSearchParams } from "react-router-dom";
import { useI18n } from "@/lib/i18n";
import { useConfig } from "@/lib/config";
import { fetchBenchmarks, stopDeployment, cancelScheduledDeploy } from "@/lib/api";
import { latestPullProgress } from "@/lib/operations";
import { useQuery } from "@/hooks/useQuery";
import { useDeployments, isLiveRun } from "@/hooks/useDeployments";
import {
  AlertModal,
  ConfirmModal,
  EmptyState,
  ErrorLine,
  PageHeader,
  Spinner,
  Tabs,
  Button,
  Toolbar,
  SearchInput,
  matchesQuery,
} from "@/ui";
import RunRow from "@/components/RunRow";
import { RunsTable, runPath, teardownKind } from "@/components/RunsTable";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import WaitingRunRow from "@/components/WaitingRunRow";
import { useWaitingDeploys } from "@/hooks/useWaitingDeploys";
import BenchmarkLauncher from "@/components/BenchmarkLauncher";
import BenchmarksPanel from "@/components/BenchmarksPanel";
import { Rocket, Terminal } from "lucide-react";
import { openCommandPalette } from "@/components/CommandPalette";
import type { BenchmarkResult, Deployment } from "@/lib/types";

export type RunTab = "live" | "finished" | "benchmarks";

export interface RunsPageProps {
  /** Which pill `/benchmarking` deep-links to. */
  initialTab?: RunTab;
}

export default function RunsPage({ initialTab }: RunsPageProps = {}) {
  const { t } = useI18n();
  const { config } = useConfig();
  const benchmarkingEnabled = config?.benchmarking_enabled ?? false;

  const { deployments, loading, error, refetch, events } = useDeployments();

  // Only asked for when the feature is on: with it off there is no launcher,
  // no tab and nothing on a row to fill, so the request would be for nothing.
  const readBenchmarks = useCallback(
    (signal?: AbortSignal): Promise<BenchmarkResult[]> =>
      benchmarkingEnabled ? fetchBenchmarks(signal) : Promise.resolve([]),
    [benchmarkingEnabled],
  );
  const {
    data: benchmarks,
    loading: benchmarksLoading,
    error: benchmarksError,
    refetch: refetchBenchmarks,
  } = useQuery(readBenchmarks);

  // While a benchmark runs, read again until it settles: the row says
  // "measuring…" and then the result, or why it failed, without a reload.
  const anyMeasuring = (benchmarks ?? []).some((b) => b.status === "running");
  useEffect(() => {
    if (!anyMeasuring) return;
    const timer = setInterval(() => refetchBenchmarks(), 3000);
    return () => clearInterval(timer);
  }, [anyMeasuring, refetchBenchmarks]);

  /** The most recent measurement per run, which is what a row shows. */
  const latestBenchmark = useMemo(() => {
    const latest = new Map<string, BenchmarkResult>();
    for (const bench of benchmarks ?? []) {
      const held = latest.get(bench.deployment_id);
      if (!held || new Date(bench.started_at) > new Date(held.started_at)) {
        latest.set(bench.deployment_id, bench);
      }
    }
    return latest;
  }, [benchmarks]);

  const live = useMemo(() => (deployments ?? []).filter(isLiveRun), [deployments]);
  // The search narrows both lists; the counts in the pills stay the totals.
  const [query, setQuery] = useState("");
  const matchesRun = useCallback(
    (r: Deployment) => matchesQuery(query, r.name, r.model, r.engine, r.variant, r.recipe_id, r.id),
    [query],
  );
  const pulls = useMemo(() => latestPullProgress(events), [events]);
  // Deploys waiting on their model: not runs yet, but the thing the operator
  // just started, so they are counted and shown with what is live.
  const { waiting, refetch: refetchWaiting } = useWaitingDeploys();
  const waitingCount = waiting.length;
  const finished = useMemo(
    () => (deployments ?? []).filter((d) => !isLiveRun(d)),
    [deployments],
  );
  const shownLive = useMemo(() => live.filter(matchesRun), [live, matchesRun]);
  const shownFinished = useMemo(() => finished.filter(matchesRun), [finished, matchesRun]);

  /** The operator's choice, once they make one. Until then the page opens on
   *  whichever pill has something in it — landing on an empty Live tab while
   *  a history sits behind Finished is a page that looks broken. */
  const [chosenTab, setChosenTab] = useState<RunTab | null>(initialTab ?? null);
  let tab: RunTab =
    chosenTab ?? (live.length === 0 && waitingCount === 0 && finished.length > 0 ? "finished" : "live");
  if (tab === "benchmarks" && !benchmarkingEnabled) tab = "live";

  const [teardownTarget, setTeardownTarget] = useState<Deployment | null>(null);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);
  const [benchmarkTarget, setBenchmarkTarget] = useState<Deployment | null>(null);

  // `?run=<id>` — an older link to one run. The run has a page of its own now.
  const [searchParams] = useSearchParams();
  const linkedRun = searchParams.get("run");

  const doTeardown = async (id: string) => {
    try {
      await stopDeployment(id);
      refetch();
    } catch (e) {
      setAlertModal({
        title: t("common.error"),
        message: e instanceof Error ? e.message : t("runs.stopFailed"),
      });
    }
  };

  // A console lays runs out as a table; under 1200px the rows stay cards.
  // Either way the list has the width, and a run opens on its own page.
  const wide = useMediaQuery("(min-width: 1200px)");

  const renderList = (runs: Deployment[], empty: React.ReactNode) => {
    if (runs.length === 0) return empty;
    if (wide) {
      return (
        <RunsTable runs={runs} onTeardown={setTeardownTarget} benchmarks={latestBenchmark} pulls={pulls} />
      );
    }
    return (
      <div className="space-y-2">
        {runs.map((run) => (
          <RunRow
            key={run.id}
            run={run}
            benchmark={latestBenchmark.get(run.id)}
            pull={run.status === "pulling" ? pulls.get(run.id) : undefined}
            onTeardown={() => setTeardownTarget(run)}
            onBenchmark={() => setBenchmarkTarget(run)}
          />
        ))}
      </div>
    );
  };

  const tabs = [
    { id: "live", label: t("runs.tabLive"), count: live.length + waitingCount },
    { id: "finished", label: t("runs.tabFinished"), count: finished.length },
    ...(benchmarkingEnabled
      ? [{ id: "benchmarks", label: t("runs.tabBenchmarks"), count: benchmarks?.length }]
      : []),
  ];

  const nothingAtAll =
    !loading && !error && live.length === 0 && waitingCount === 0 && finished.length === 0;

  const cancelWaiting = async (id: string) => {
    try {
      await cancelScheduledDeploy(id);
    } catch (e) {
      setAlertModal({ title: t("common.error"), message: e instanceof Error ? e.message : t("common.unknownError") });
    }
    void refetchWaiting();
  };

  const waitingRows = waiting.length > 0 && (
    <div className="space-y-2" data-testid="waiting-runs">
      {waiting.map((w) => (
        <WaitingRunRow key={w.entry.id} waiting={w} onCancel={() => void cancelWaiting(w.entry.id)} />
      ))}
    </div>
  );

  if (linkedRun) return <Navigate to={runPath(linkedRun)} replace />;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t("nav.runs")}
        title={t("runs.heading")}
        description={t("runs.counts", { live: live.length, finished: finished.length })}
        actions={
          <Button variant="primary" icon={Rocket} onClick={openCommandPalette}>
            {t("overview.deploy")}
          </Button>
        }
      />

      {/* Search, then the filters — the order every list page uses. */}
      <Toolbar>
        {tab !== "benchmarks" && (
          <SearchInput value={query} onChange={setQuery} label={t("runs.search")} clearLabel={t("common.clear")} />
        )}
        <Tabs
          label={t("runs.heading")}
          value={tab}
          onChange={(id) => setChosenTab(id as RunTab)}
          tabs={tabs}
        />
      </Toolbar>

      {loading && (
        <div className="flex justify-center py-20">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      )}
      <ErrorLine>{error}</ErrorLine>

      {tab === "benchmarks" ? (
        <BenchmarksPanel
          benchmarks={benchmarks}
          loading={benchmarksLoading}
          error={benchmarksError}
          refetch={refetchBenchmarks}
        />
      ) : nothingAtAll ? (
        <EmptyState icon={Terminal} hint={t("runs.emptyHint")}>
          {t("runs.empty")}
        </EmptyState>
      ) : tab === "live" ? (
        <div className="space-y-2">
          {waitingRows}
          {renderList(
            shownLive,
            waitingCount > 0 ? null : (
              <EmptyState icon={Terminal} hint={t("runs.noneLiveHint")}>
                {t("runs.noneLive")}
              </EmptyState>
            ),
          )}
        </div>
      ) : (
        renderList(
          shownFinished,
          <EmptyState icon={Terminal} hint={t("runs.noneFinishedHint")}>
            {t("runs.noneFinished")}
          </EmptyState>,
        )
      )}

      {teardownTarget && (() => {
        // Named once rather than recomputed for the title, the body and the
        // button: three copies of the same conditional is three chances for
        // them to disagree about what the button is going to do.
        const kind = teardownKind(teardownTarget);
        return (
          <ConfirmModal
            open
            onClose={() => setTeardownTarget(null)}
            onConfirm={() => { doTeardown(teardownTarget.id); setTeardownTarget(null); }}
            title={t(`runs.${kind}Title`)}
            message={t(`runs.${kind}Body`, { name: teardownTarget.name })}
            confirmLabel={t(`runs.${kind}`)}
            confirmVariant="danger"
          />
        );
      })()}

      {alertModal && (
        <AlertModal
          open
          onClose={() => setAlertModal(null)}
          title={alertModal.title}
          message={alertModal.message}
        />
      )}

      {benchmarkTarget && (
        <BenchmarkLauncher
          run={benchmarkTarget}
          enabled={benchmarkingEnabled}
          tool={config?.benchmarking_tool}
          onClose={() => setBenchmarkTarget(null)}
          onStarted={() => {
            setBenchmarkTarget(null);
            refetchBenchmarks();
          }}
        />
      )}
    </div>
  );
}
