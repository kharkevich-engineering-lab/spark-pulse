/** One run, on a page of its own: `/jobs/:id`.
 *
 * It was a panel beside the Runs table — and before that a card that expanded
 * in place. Both put a second scrolling column inside a scrolling page: the
 * log, the ranks, the engine's metrics and the event timeline stacked into one
 * tall box, with the list it was opened from squeezed beside it. So a run has
 * an address, a back link, and three tabs, none of them long:
 *
 * - **Overview** — how it is doing (the latest benchmark, how long it has
 *   served), what it is made of, where its ranks are, the engine's metrics.
 * - **Logs** — the stream, in a terminal-sized box: a log is the one thing
 *   that is allowed a scroll of its own.
 * - **Events** — the stored timeline, with the live frames merged onto it.
 *
 * The tab is `?tab=`, so a link can open the logs directly. Everything here is
 * read for this one run only: its ranks' live state, its metrics window and
 * its history cost a request each, and nobody pays for runs nobody opened.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Flame, Terminal } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { useConfig } from "@/lib/config";
import {
  connectLogStream,
  fetchBenchmarks,
  fetchDeployment,
  fetchDeploymentEvents,
  fetchEngineMetrics,
  stopDeployment,
} from "@/lib/api";
import { eventFromFrame, latestPullProgress, mergeEvents } from "@/lib/operations";
import type { DeploymentEvent } from "@/lib/operations";
import { cn, formatSize, servesChat } from "@/lib/utils";
import { useQuery } from "@/hooks/useQuery";
import { useDeployments, isLiveRun } from "@/hooks/useDeployments";
import {
  AlertModal,
  Button,
  ConfirmModal,
  DetailHeader,
  EmptyState,
  ErrorLine,
  Section,
  Spinner,
  StatusBadge,
  Tabs,
  isSettling,
} from "@/ui";
import EventStreamViewer from "@/components/EventStreamViewer";
import RankList, { hasRanksToShow } from "@/components/RankList";
import EngineMetricsPanel from "@/components/EngineMetrics";
import ServesChip from "@/components/ServesChip";
import BenchmarkLauncher from "@/components/BenchmarkLauncher";
import { Chip, CurlExample, Endpoint, endpointUrl, metric, placement } from "@/components/RunRow";
import { runTiming, teardownIcon, teardownKind, teardownLabel, throughputOf } from "@/components/RunsTable";
import type { BenchmarkResult, Deployment, EngineMetricsWindow } from "@/lib/types";

/** How often a running run's per-rank state is re-read, in ms. Matches the
 *  list's own poll so the two never drift by more than one interval. */
const RANK_POLL_MS = 10000;

/** How often the engine's metrics window is re-read, in ms. The backend
 *  sampler's own cadence: reading faster returns the same window twice. */
const METRICS_POLL_MS = 5000;

/** How many stored events are read. One page, newest first. */
const EVENT_HISTORY_LIMIT = 200;
/** How many of the latest events Overview shows beside the details. */
const RECENT_EVENTS = 12;

function clockOf(iso: string): string {
  const d = new Date(iso);
  return Number.isFinite(d.getTime())
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
    : "";
}

/** How many log lines are kept in the browser. */
const LOG_LINES = 500;

const TABS = ["overview", "logs", "events"] as const;
type DetailTab = (typeof TABS)[number];

function Figure({ label, value, busy }: { label: string; value: string; busy?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-[10.5px] font-semibold uppercase tracking-[0.08em] text-muted">{label}</dt>
      <dd className={busy ? "animate-pulse font-mono text-[15px] tabular-nums text-blue2" : "font-mono text-[15px] tabular-nums"}>
        {value}
      </dd>
    </div>
  );
}

export default function RunDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const { t } = useI18n();
  const { config } = useConfig();
  const benchmarkingEnabled = config?.benchmarking_enabled ?? false;

  const [params, setParams] = useSearchParams();
  const asked = params.get("tab");
  const tab: DetailTab = (TABS as readonly string[]).includes(asked ?? "") ? (asked as DetailTab) : "overview";
  const setTab = (next: string) =>
    setParams(
      (prev) => {
        const out = new URLSearchParams(prev);
        if (next === "overview") out.delete("tab");
        else out.set("tab", next);
        return out;
      },
      { replace: true },
    );

  const { deployments, loading, error, refetch, events, clearEvents } = useDeployments();
  const run = deployments?.find((d) => d.id === id) ?? null;
  const pull = run?.status === "pulling" ? latestPullProgress(events).get(id) : undefined;

  // ── The latest benchmark, which is what the figures say ──────────────────
  const readBenchmarks = useCallback(
    (signal?: AbortSignal): Promise<BenchmarkResult[]> =>
      benchmarkingEnabled ? fetchBenchmarks(signal) : Promise.resolve([]),
    [benchmarkingEnabled],
  );
  const { data: benchmarks, refetch: refetchBenchmarks } = useQuery(readBenchmarks);
  const benchmark = useMemo(() => {
    let latest: BenchmarkResult | undefined;
    for (const bench of benchmarks ?? []) {
      if (bench.deployment_id !== id) continue;
      if (!latest || new Date(bench.started_at) > new Date(latest.started_at)) latest = bench;
    }
    return latest;
  }, [benchmarks, id]);
  const measuring = benchmark?.status === "running";
  useEffect(() => {
    if (!measuring) return;
    const timer = setInterval(() => refetchBenchmarks(), 3000);
    return () => clearInterval(timer);
  }, [measuring, refetchBenchmarks]);

  // ── The ranks' live state, only while it is running ──────────────────────
  // A pending run has no containers yet and a stopped one has none by design;
  // inspecting either would paint every rank red for saying what the badge
  // already says. A failed read leaves the list's ranks standing.
  const running = run?.status === "running";
  const [detail, setDetail] = useState<Deployment | null>(null);
  const wantedRef = useRef<string | null>(null);
  wantedRef.current = running ? id : null;
  const loadDetail = useCallback((which: string) => {
    if (wantedRef.current !== which) return;
    fetchDeployment(which)
      .then((dep) => {
        if (wantedRef.current === which) setDetail(dep);
      })
      .catch(() => {});
  }, []);
  useEffect(() => {
    setDetail(null);
    if (!running) return;
    loadDetail(id);
    const timer = setInterval(() => loadDetail(id), RANK_POLL_MS);
    return () => clearInterval(timer);
  }, [running, id, loadDetail]);
  const ranks = detail && detail.id === id ? detail : null;

  // ── The engine's metrics window ──────────────────────────────────────────
  // Asked for whatever the state: a stopped run answers "there is no engine
  // to ask", which is worth showing. A missed poll keeps the last window.
  const [metrics, setMetrics] = useState<EngineMetricsWindow | null>(null);
  const [metricsLoading, setMetricsLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    setMetrics(null);
    setMetricsLoading(true);
    const load = () =>
      fetchEngineMetrics(id)
        .then((w) => {
          if (!alive) return;
          // A window is only ever this run's: one for another run is dropped.
          setMetrics(w && w.deployment_id === id ? w : null);
          setMetricsLoading(false);
        })
        .catch(() => {
          if (alive) setMetricsLoading(false);
        });
    load();
    const timer = setInterval(load, METRICS_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [id]);

  // ── The stored timeline, read once; live frames land on top of it ────────
  const [history, setHistory] = useState<{ events: DeploymentEvent[]; total: number } | null>(null);
  const [historyLoading, setHistoryLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    setHistory(null);
    setHistoryLoading(true);
    fetchDeploymentEvents(id, { limit: EVENT_HISTORY_LIMIT })
      .then((page) => {
        if (!alive) return;
        setHistory({ events: page.events.map(eventFromFrame), total: page.total });
        setHistoryLoading(false);
      })
      .catch(() => {
        // The live frames alone, which is what the panel had before the store.
        if (alive) setHistoryLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [id]);
  const timeline = useMemo(() => {
    const merged = mergeEvents(
      events.filter((e) => e.resource === id),
      history?.events ?? [],
    );
    return { events: merged, total: Math.max(history?.total ?? 0, merged.length) };
  }, [events, history, id]);

  // ── The log stream, for as long as the page is open ──────────────────────
  const [logs, setLogs] = useState<string[]>([]);
  const [streaming, setStreaming] = useState(true);
  const logRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);
  useEffect(() => {
    setLogs([]);
    setStreaming(true);
    pinnedRef.current = true;
    const stop = connectLogStream(id, (event, data) => {
      if (event === "log") {
        setLogs((prev) => [...prev.slice(-(LOG_LINES - 1)), (data as { text: string }).text]);
      } else if (event === "status") {
        refetch();
        loadDetail(id);
      } else if (event === "end") {
        // The run is no longer serving; the connection has closed itself.
        setStreaming(false);
      }
    });
    return () => stop?.();
  }, [id, refetch, loadDetail]);
  // Follow the tail only while the reader is at the bottom.
  useEffect(() => {
    const el = logRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [logs, tab]);
  const onLogScroll = () => {
    const el = logRef.current;
    if (el) pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  // ── Actions ───────────────────────────────────────────────────────────────
  const [confirming, setConfirming] = useState(false);
  const [benchmarkOpen, setBenchmarkOpen] = useState(false);
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);

  const teardown = async (target: Deployment) => {
    const kind = teardownKind(target);
    try {
      await stopDeployment(target.id);
      // A removed record has no page to stay on.
      if (kind === "remove") navigate("/jobs");
      else refetch();
    } catch (e) {
      setAlert({ title: t("common.error"), message: e instanceof Error ? e.message : t("runs.stopFailed") });
    }
  };

  if (!run) {
    return (
      <div className="space-y-4">
        <DetailHeader backTo="/jobs" backLabel={t("nav.runs")} title={id} />
        {loading || (!deployments && !error) ? (
          <div className="flex justify-center py-20">
            <Spinner size="lg" label={t("common.loading")} />
          </div>
        ) : error ? (
          <ErrorLine>{error}</ErrorLine>
        ) : (
          <EmptyState
            icon={Terminal}
            hint={t("runs.notFoundHint")}
            action={
              <Link to="/jobs" className="text-blue2 hover:underline">
                {t("runs.seeAll")}
              </Link>
            }
          >
            {t("runs.notFound")}
          </EmptyState>
        )}
      </div>
    );
  }

  const live = isLiveRun(run);
  const settling = isSettling(run.sync, run.phase);
  const url = endpointUrl(run, window.location.hostname);
  const latency = metric(benchmark, "ttft_ms", "latency_ms");
  const label = teardownLabel(run, t);
  const kind = teardownKind(run);
  const TeardownIcon = teardownIcon(run);

  return (
    <div data-testid={`run-detail-${run.id}`}>
      <DetailHeader
        backTo="/jobs"
        backLabel={t("nav.runs")}
        title={run.name}
        badge={
          <StatusBadge
            status={run.status}
            sync={run.sync}
            syncReason={run.sync_reason}
            syncIntent={run.sync_intent}
            phase={run.phase}
            phaseVerified={run.phase_verified}
            phaseReason={run.phase_reason}
            progress={pull?.percent}
          />
        }
        subtitle={
          <div className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-1.5">
              {run.engine && (
                <Chip title={run.image_ref || undefined}>
                  {run.engine}
                  {run.variant ? `/${run.variant}` : ""}
                </Chip>
              )}
              {run.model && <Chip title={run.model}>{run.model}</Chip>}
              <ServesChip serves={run.serves} />
              <Chip>{placement(run, t("runs.thisNode"))}</Chip>
            </div>
            {url && live && (
              <div className="flex flex-wrap items-center gap-1.5">
                <Endpoint url={url} />
                <CurlExample url={url} model={run.served_model || run.model || ""} />
              </div>
            )}
          </div>
        }
        actions={
          <>
            {live && servesChat(run.serves) && (
              <Button
                icon={Flame}
                onClick={() => setBenchmarkOpen(true)}
                disabled={run.status !== "running" || measuring}
                loading={measuring}
              >
                {t("runs.benchmark")}
              </Button>
            )}
            <Button
              variant="danger"
              icon={TeardownIcon}
              disabled={settling}
              title={settling ? t("runs.settling") : label}
              onClick={() => setConfirming(true)}
            >
              {label}
            </Button>
          </>
        }
      />

      {pull && (
        <div className="mb-4" data-testid={`pull-${run.id}`}>
          <p className="text-[13px] text-muted">
            {t("runs.pullingImage", {
              percent: Math.floor(pull.percent),
              done: formatSize(pull.bytesDone),
              total: pull.bytesTotal ? formatSize(pull.bytesTotal) : "?",
            })}
          </p>
          <div className="mt-1 h-1.5 max-w-[420px] overflow-hidden rounded-full bg-bg2">
            <div
              role="progressbar"
              aria-label={t("runs.pullProgressLabel", { name: run.name })}
              aria-valuenow={Math.floor(pull.percent)}
              className="h-full bg-blue-cta transition-all"
              style={{ width: `${pull.percent}%` }}
            />
          </div>
        </div>
      )}

      <Tabs
        variant="sections"
        label={run.name}
        value={tab}
        onChange={setTab}
        className="mb-4"
        tabs={[
          { id: "overview", label: t("runs.tabOverview") },
          { id: "logs", label: t("runs.logs") },
          { id: "events", label: t("runs.tabEvents"), count: timeline.total },
        ]}
      />

      {tab === "overview" && (
        // The figures and details on the left, the run's latest events beside
        // them: what it is and what it just did, read without changing tab.
        <div className="grid grid-cols-1 items-start gap-3 min-[1200px]:grid-cols-[minmax(0,1fr)_400px]">
        <div className="min-w-0 space-y-3">
          <ErrorLine>{run.error_message}</ErrorLine>
          <Section>
            <dl className="grid grid-cols-3 gap-4 border-b border-line pb-3">
              <Figure
                label={t("runs.throughput")}
                value={throughputOf(benchmark, t)}
                busy={measuring}
              />
              <Figure label={t("runs.firstToken")} value={latency != null ? `${latency.toFixed(1)} ms` : "—"} />
              <Figure label={live ? t("runs.serving") : t("runs.ended")} value={runTiming(run, t)} />
            </dl>
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 pt-3 text-[13px] min-[900px]:grid-cols-[auto_minmax(0,1fr)_auto_minmax(0,1fr)] min-[900px]:gap-x-6">
              <dt className="text-muted">{t("runs.recipe")}</dt>
              <dd className="truncate font-mono">{run.recipe_id}</dd>
              {run.engine && (
                <>
                  <dt className="text-muted">{t("runs.engine")}</dt>
                  <dd className="truncate font-mono">
                    {run.engine}
                    {run.variant ? `/${run.variant}` : ""}
                  </dd>
                </>
              )}
              {run.image_ref && (
                <>
                  <dt className="text-muted">{t("runs.image")}</dt>
                  <dd className="truncate font-mono" title={run.image_ref}>
                    {run.image_ref}
                  </dd>
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
              {run.port ? (
                <>
                  <dt className="text-muted">{t("overview.colPort")}</dt>
                  <dd className="font-mono">{run.port}</dd>
                </>
              ) : null}
            </dl>
          </Section>
          {hasRanksToShow(ranks?.ranks ?? run.ranks, ranks?.orphans ?? run.orphans) && (
            <Section>
              <RankList ranks={ranks?.ranks ?? run.ranks} orphans={ranks?.orphans ?? run.orphans} />
            </Section>
          )}
          <Section>
            <EngineMetricsPanel window={metrics} loading={metricsLoading} />
          </Section>
        </div>
        <Section
          flush
          title={t("runs.recentEvents")}
          actions={
            timeline.events.length > RECENT_EVENTS ? (
              <Button size="sm" variant="ghost" onClick={() => setTab("events")}>
                {t("runs.allEvents")}
              </Button>
            ) : undefined
          }
          data-testid="run-recent-events"
        >
          {timeline.events.length === 0 ? (
            <p className="px-3 py-3 text-[12.5px] text-muted">{historyLoading ? t("common.loading") : t("runs.noEvents")}</p>
          ) : (
            <ol className="divide-y divide-line">
              {timeline.events.slice(0, RECENT_EVENTS).map((e) => (
                <li key={e.event_id} className="flex gap-3 px-3 py-1.5 text-[12.5px]">
                  <span className="w-11 shrink-0 font-mono tabular-nums text-muted">{clockOf(e.timestamp)}</span>
                  <span
                    title={e.message}
                    className={cn(
                      "min-w-0 truncate",
                      e.severity === "error" ? "text-bad" : e.severity === "warning" ? "text-warn" : "text-muted",
                    )}
                  >
                    {e.message}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </Section>
        </div>
      )}

      {tab === "logs" && (
        <Section
          flush
          title={
            streaming ? (
              <span className="inline-flex items-center gap-1.5">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-good" />
                {t("runs.streaming")}
              </span>
            ) : (
              t("runs.streamStopped")
            )
          }
        >
          <div
            ref={logRef}
            onScroll={onLogScroll}
            data-testid="run-logs"
            className="h-[60vh] min-h-[240px] overflow-auto whitespace-pre-wrap bg-bg p-4 font-mono text-[13px]"
          >
            {(logs.length ? logs : [t("runs.noLogs")]).map((line, i) => (
              <div key={i} className="leading-relaxed text-muted last:text-text">
                {line}
              </div>
            ))}
          </div>
        </Section>
      )}

      {tab === "events" && (
        <Section>
          <EventStreamViewer
            events={timeline.events}
            total={timeline.total}
            loading={historyLoading}
            resource={run.id}
            onClear={() => {
              // The panel is cleared, not the store: reopening the page reads
              // the timeline back. Nothing here can destroy a run's history.
              clearEvents(run.id);
              setHistory({ events: [], total: 0 });
            }}
          />
        </Section>
      )}

      <ConfirmModal
        open={confirming}
        onClose={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          void teardown(run);
        }}
        title={t(`runs.${kind}Title`)}
        message={t(`runs.${kind}Body`, { name: run.name })}
        confirmLabel={t(`runs.${kind}`)}
        confirmVariant="danger"
      />
      <AlertModal open={alert !== null} onClose={() => setAlert(null)} title={alert?.title ?? ""} message={alert?.message ?? ""} />
      {benchmarkOpen && (
        <BenchmarkLauncher
          run={run}
          enabled={benchmarkingEnabled}
          tool={config?.benchmarking_tool}
          onClose={() => setBenchmarkOpen(false)}
          onStarted={() => {
            setBenchmarkOpen(false);
            refetchBenchmarks();
          }}
        />
      )}
    </div>
  );
}
