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
 * The rows are `RunRow`. What stays here is everything that needs one row open
 * at a time: the log stream, the per-rank read, the engine's metrics window.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useI18n } from "@/lib/i18n";
import { useConfig } from "@/lib/config";
import {
  fetchBenchmarks,
  fetchDeployment,
  fetchEngineMetrics,
  stopDeployment,
  connectLogStream,
} from "@/lib/api";
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
} from "@/ui";
import EventStreamViewer from "@/components/EventStreamViewer";
import RankList from "@/components/RankList";
import EngineMetricsPanel from "@/components/EngineMetrics";
import RunRow from "@/components/RunRow";
import BenchmarkLauncher from "@/components/BenchmarkLauncher";
import BenchmarksPanel from "@/components/BenchmarksPanel";
import { ExperimentalBanner } from "@/components/Experimental";
import {
  MULTI_NODE_REASON,
  MULTI_NODE_TITLE,
  MULTI_NODE_UNPROVEN,
} from "@/lib/experimental";
import { Terminal } from "lucide-react";
import type { BenchmarkResult, Deployment, EngineMetricsWindow } from "@/lib/types";

/** How often the open row's per-rank state is re-read, in ms. Matches the
 *  list's own poll so the two never drift by more than one interval. */
const RANK_POLL_MS = 10000;

/** How often the open row re-reads the engine's metrics window, in ms.
 *  Matches the backend sampler's own cadence: reading faster would return the
 *  same window twice and would not make a new measurement exist. */
const METRICS_POLL_MS = 5000;

export type RunTab = "live" | "finished" | "benchmarks";

export interface RunsPageProps {
  /** Which pill `/benchmarking` deep-links to. */
  initialTab?: RunTab;
}

export default function RunsPage({ initialTab }: RunsPageProps = {}) {
  const { t } = useI18n();
  const { config } = useConfig();
  const benchmarkingEnabled = config?.benchmarking_enabled ?? false;

  const { deployments, loading, error, refetch, events, clearEvents } = useDeployments();

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
  const finished = useMemo(
    () => (deployments ?? []).filter((d) => !isLiveRun(d)),
    [deployments],
  );

  /** The operator's choice, once they make one. Until then the page opens on
   *  whichever pill has something in it — landing on an empty Live tab while
   *  a history sits behind Finished is a page that looks broken. */
  const [chosenTab, setChosenTab] = useState<RunTab | null>(initialTab ?? null);
  let tab: RunTab = chosenTab ?? (live.length === 0 && finished.length > 0 ? "finished" : "live");
  if (tab === "benchmarks" && !benchmarkingEnabled) tab = "live";

  const [expandedId, setExpandedId] = useState<string | null>(null);
  /** The open row's live detail. Only ever the one row — see `rankDetail`. */
  const [detail, setDetail] = useState<Deployment | null>(null);
  /** Which deployment's detail is currently wanted, for discarding late replies. */
  const wantedRef = useRef<string | null>(null);
  const [logs, setLogs] = useState<Record<string, string[]>>({});
  const [streaming, setStreaming] = useState<Record<string, boolean>>({});
  const logRef = useRef<Record<string, HTMLDivElement | null>>({});
  const stopRef = useRef<Record<string, () => void>>({});
  const atBottomRef = useRef<Record<string, boolean>>({});
  /** The open row's engine metrics window, and which row it belongs to. */
  const [metrics, setMetrics] = useState<EngineMetricsWindow | null>(null);
  const [metricsLoading, setMetricsLoading] = useState(false);
  const metricsWantedRef = useRef<string | null>(null);
  const [teardownTarget, setTeardownTarget] = useState<Deployment | null>(null);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);
  const [benchmarkTarget, setBenchmarkTarget] = useState<Deployment | null>(null);

  /** Read one deployment's live per-rank container state.
   *
   * The list deliberately does not carry it. `GET /deployments` is a single
   * Docker enumerate on this machine however many deployments and ranks there
   * are; a rank's live state is an inspect *per rank*, through that rank's own
   * node. Putting that in the list would charge four agent round trips per
   * four-node deployment on every ten-second poll — for rows nobody has
   * opened. So it is paid for one deployment, only while its row is open.
   *
   * A failure leaves the list's ranks standing: no container state is the
   * status quo, and an error banner over a log pane would say less than the
   * row's own badge already does.
   */
  const loadDetail = useCallback((id: string) => {
    // Asked for a row that is no longer the one wanted: not sent, and a reply
    // that arrives after the row changed is dropped rather than shown against
    // whatever is open now.
    if (wantedRef.current !== id) return;
    fetchDeployment(id)
      .then((dep) => { if (wantedRef.current === id) setDetail(dep); })
      .catch(() => {});
  }, []);

  // Only a running deployment is asked. A pending one has no containers yet
  // and a stopped one has none by design; inspecting either would paint every
  // rank red for saying exactly what the row already says.
  const expandedRunning =
    expandedId && deployments?.find((d) => d.id === expandedId)?.status === "running"
      ? expandedId
      : null;
  wantedRef.current = expandedRunning;

  useEffect(() => {
    setDetail(null);
    if (!expandedRunning) return;
    loadDetail(expandedRunning);
    const i = setInterval(() => loadDetail(expandedRunning), RANK_POLL_MS);
    return () => clearInterval(i);
  }, [expandedRunning, loadDetail]);

  /** The open row's detail, or nothing when it belongs to another row. */
  const rankDetail = (id: string) => (detail && detail.id === id ? detail : null);

  /** Read the open row's engine metrics window.
   *
   * Asked for every open row, running or not: a stopped deployment answers
   * with "there is no engine to ask", which is worth showing. A failure is
   * left as the previous window rather than blanked, because a single missed
   * poll is not evidence that the engine stopped publishing.
   */
  const loadMetrics = useCallback((id: string) => {
    if (metricsWantedRef.current !== id) return;
    fetchEngineMetrics(id)
      .then((w) => {
        if (metricsWantedRef.current !== id) return;
        setMetrics(w);
        setMetricsLoading(false);
      })
      .catch(() => {
        if (metricsWantedRef.current === id) setMetricsLoading(false);
      });
  }, []);

  metricsWantedRef.current = expandedId;

  useEffect(() => {
    setMetrics(null);
    if (!expandedId) return;
    setMetricsLoading(true);
    loadMetrics(expandedId);
    const i = setInterval(() => loadMetrics(expandedId), METRICS_POLL_MS);
    return () => clearInterval(i);
  }, [expandedId, loadMetrics]);

  /** The open row's metrics window, or nothing when it belongs to another. */
  const metricsFor = (id: string) =>
    metrics && metrics.deployment_id === id ? metrics : null;

  // Auto-scroll only if already pinned to the bottom
  useEffect(() => {
    if (!expandedId) return;
    const el = logRef.current[expandedId];
    if (el && atBottomRef.current[expandedId] !== false) {
      el.scrollTop = el.scrollHeight;
    }
  }, [logs[expandedId || ""]]);

  const handleLogScroll = (id: string) => {
    const el = logRef.current[id];
    if (el) atBottomRef.current[id] = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const toggle = (id: string) => {
    if (expandedId === id) { stopRef.current[id]?.(); setStreaming((s) => ({ ...s, [id]: false })); setExpandedId(null); return; }
    setExpandedId(id);
    atBottomRef.current[id] = true; // start pinned to bottom
    setStreaming((s) => ({ ...s, [id]: true }));
    stopRef.current[id] = connectLogStream(id, (event, data) => {
      if (event === "log") {
        setLogs((l) => {
          const prev = l[id] || [];
          return { ...l, [id]: [...prev.slice(-499), (data as { text: string }).text] };
        });
      }
      else if (event === "status") { refetch(); loadDetail(id); }
      // The stream is over — the deployment is no longer running, so there is
      // nothing further to tail. The connection has already closed itself.
      else if (event === "end") { setStreaming((s) => ({ ...s, [id]: false })); }
    });
  };

  const doTeardown = async (id: string) => {
    try {
      await stopDeployment(id);
      stopRef.current[id]?.();
      setStreaming((s) => ({ ...s, [id]: false }));
      refetch();
    } catch (e) {
      setAlertModal({
        title: t("common.error"),
        message: e instanceof Error ? e.message : t("runs.stopFailed"),
      });
    }
  };

  /** Which of the three words the confirmation uses. A live deployment is
   *  stopped, one that never started is cancelled, and a finished one is a
   *  record being cleared — three different consequences, three dialogs. */
  const teardownKind = (run: Deployment) =>
    !isLiveRun(run) ? "remove" : run.status === "pending" ? "cancel" : "stop";

  const renderExpanded = (run: Deployment) => (
    <>
      {(run.node_count ?? 1) > 1 && (
        <ExperimentalBanner
          className="m-4"
          title={MULTI_NODE_TITLE}
          reason={MULTI_NODE_REASON}
          items={MULTI_NODE_UNPROVEN}
        />
      )}
      <dl className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1 border-b border-line bg-bg px-4 py-3 text-[13px]">
        <dt className="text-muted">{t("runs.recipe")}</dt>
        <dd className="truncate font-mono">{run.recipe_id}</dd>
        {run.engine && (
          <>
            <dt className="text-muted">{t("runs.engine")}</dt>
            <dd className="truncate font-mono">{run.engine}{run.variant ? `/${run.variant}` : ""}</dd>
          </>
        )}
        {run.image_ref && (
          <>
            <dt className="text-muted">{t("runs.image")}</dt>
            <dd className="truncate font-mono">{run.image_ref}</dd>
          </>
        )}
        <dt className="text-muted">{t("runs.model")}</dt>
        <dd className="truncate font-mono">{run.model || t("runs.modelFromCommand")}</dd>
        {run.container_name && (
          <>
            <dt className="text-muted">{t("runs.container")}</dt>
            <dd className="truncate font-mono">{run.container_name}</dd>
          </>
        )}
      </dl>
      <RankList
        ranks={rankDetail(run.id)?.ranks ?? run.ranks}
        orphans={rankDetail(run.id)?.orphans ?? run.orphans}
        className="border-b border-line bg-bg px-4 py-3"
      />
      <EngineMetricsPanel
        window={metricsFor(run.id)}
        loading={metricsLoading}
        className="border-b border-line bg-bg px-4 py-3"
      />
      <div className="flex items-center gap-2 bg-bg px-4 py-2 text-[13px] text-muted">
        {streaming[run.id] ? (
          <span className="flex items-center gap-1.5">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-good" />
            {t("runs.streaming")}
          </span>
        ) : (
          <span>{t("runs.streamStopped")}</span>
        )}
        <button onClick={() => toggle(run.id)} className="ml-auto text-blue2 hover:underline">
          {t("runs.hide")}
        </button>
      </div>
      <div
        ref={(el) => { logRef.current[run.id] = el; }}
        onScroll={() => handleLogScroll(run.id)}
        className="max-h-[60vh] min-h-[240px] overflow-auto whitespace-pre-wrap bg-bg p-4 font-mono text-[13px] text-text"
      >
        {(logs[run.id] || [t("runs.noLogs")]).map((line, i) => (
          <div key={i} className="leading-relaxed text-muted last:text-text">{line}</div>
        ))}
      </div>
      <div className="border-t border-line p-4">
        <EventStreamViewer
          events={events.filter((e) => e.resource === run.id)}
          resource={run.id}
          onClear={() => clearEvents(run.id)}
        />
      </div>
    </>
  );

  const renderList = (runs: Deployment[], empty: React.ReactNode) => {
    if (runs.length === 0) return empty;
    return (
      <div className="space-y-2">
        {runs.map((run) => (
          <RunRow
            key={run.id}
            run={run}
            benchmark={latestBenchmark.get(run.id)}
            expanded={expandedId === run.id}
            onToggle={() => toggle(run.id)}
            onTeardown={() => setTeardownTarget(run)}
            onBenchmark={() => setBenchmarkTarget(run)}
          >
            {renderExpanded(run)}
          </RunRow>
        ))}
      </div>
    );
  };

  const tabs = [
    { id: "live", label: t("runs.tabLive"), count: live.length },
    { id: "finished", label: t("runs.tabFinished"), count: finished.length },
    ...(benchmarkingEnabled
      ? [{ id: "benchmarks", label: t("runs.tabBenchmarks"), count: benchmarks?.length }]
      : []),
  ];

  const nothingAtAll = !loading && !error && live.length === 0 && finished.length === 0;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t("nav.runs")}
        title={t("runs.heading")}
        description={t("runs.subtitle")}
      />

      <Tabs
        label={t("runs.heading")}
        value={tab}
        onChange={(id) => setChosenTab(id as RunTab)}
        tabs={tabs}
      />

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
        renderList(
          live,
          <EmptyState icon={Terminal} hint={t("runs.noneLiveHint")}>
            {t("runs.noneLive")}
          </EmptyState>,
        )
      ) : (
        renderList(
          finished,
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
