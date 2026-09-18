/** The benchmark history, as a tab of Runs.
 *
 * This was a page of its own with a nav entry of its own, and the first thing
 * it asked for was a deployment id typed by hand — which is to say it asked
 * the operator to carry an identifier across from the list of runs it was
 * sitting next to. The launcher moved onto the run itself, so what is left
 * here is the record: what has been measured, what the latest numbers per
 * recipe are, and how two runs compare.
 *
 * Two sub-views, not three. "Comparison" was a tab that showed nothing at all
 * until two runs had been ticked and a button pressed — an empty destination
 * whose emptiness meant "you have not done something elsewhere yet". The
 * comparison now renders where the selection was made, under the runs it is
 * comparing.
 */

import { useState } from "react";
import { useI18n } from "@/lib/i18n";
import { fetchLatestByRecipe, compareRuns, deleteBenchmark } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import {
  AlertModal,
  Button,
  ConfirmModal,
  EmptyState,
  ErrorLine,
  IconButton,
  Spinner,
  StatusBadge,
  Tabs,
} from "@/ui";
import {
  BarChart3,
  Flame,
  Table as TableIcon,
  Trash2,
  TrendingDown,
  TrendingUp,
  X,
} from "lucide-react";
import type { BenchmarkResult } from "@/lib/types";

type View = "history" | "summary";

export interface BenchmarksPanelProps {
  /** The history, read once by the page so the run rows can use it too. */
  benchmarks: BenchmarkResult[] | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

const formatNumber = (n: number, decimals = 1) => n.toFixed(decimals);

/** The columns of the summary, in order, and where each reads its number. */
const SUMMARY_COLUMNS: { key: string; label: string; decimals?: number; suffix?: string }[] = [
  { key: "throughput", label: "benchmarking.colThroughput" },
  { key: "latency_ms", label: "benchmarking.colLatency" },
  { key: "decode_latency_ms", label: "benchmarking.colDecode" },
  { key: "gpu_memory_gb", label: "benchmarking.colGpuMemory" },
  { key: "gpu_utilization", label: "benchmarking.colGpuUtil", decimals: 0, suffix: "%" },
  { key: "prefill_speed", label: "benchmarking.colPrefill" },
];

function cell(results: Record<string, unknown> | null, column: (typeof SUMMARY_COLUMNS)[number]) {
  const value = results?.[column.key];
  if (typeof value !== "number") return "—";
  return `${formatNumber(value, column.decimals ?? 1)}${column.suffix ?? ""}`;
}

export default function BenchmarksPanel({
  benchmarks,
  loading,
  error,
  refetch,
}: BenchmarksPanelProps) {
  const { t, plural } = useI18n();
  const [view, setView] = useState<View>("history");
  const { data: latestByRecipe, loading: latestLoading } = useQuery(fetchLatestByRecipe);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);

  const [selectedRunIds, setSelectedRunIds] = useState<string[]>([]);
  const [comparisonResult, setComparisonResult] = useState<{
    runs: Record<string, BenchmarkResult>;
    comparison: Record<string, any>;
    run_ids: string[];
  } | null>(null);

  /** The run the operator asked to delete, held until they confirm. Kept as
   *  the record rather than the id so the dialog can name it. */
  const [deleteTarget, setDeleteTarget] = useState<BenchmarkResult | null>(null);

  /** What the list calls a run — the same fallback chain the row itself uses,
   *  so the dialog names it the way the operator just read it. */
  const runLabel = (bench: BenchmarkResult) =>
    bench.recipe_name || bench.recipe_id || bench.benchmark_id.slice(0, 8);

  const handleDelete = async () => {
    if (!deleteTarget) return;
    const id = deleteTarget.benchmark_id;
    try {
      await deleteBenchmark(id);
      setDeleteTarget(null);
      // A deleted run cannot stay in a comparison selection: the compare call
      // would 404 on an id that is no longer there.
      setSelectedRunIds((prev) => prev.filter((i) => i !== id));
      refetch();
    } catch (e) {
      // The dialog closes and the reason takes its place — a 409 ("still
      // running") is the one an operator most needs to read, and leaving the
      // confirm up behind an alert hides it.
      setDeleteTarget(null);
      setAlertModal({
        title: t("common.error"),
        message: e instanceof Error ? e.message : t("benchmarking.deleteFailed"),
      });
    }
  };

  const toggleSelectRun = (id: string) => {
    setSelectedRunIds((prev) =>
      prev.includes(id) ? prev.filter((i) => i !== id) : [...prev, id],
    );
  };

  const runComparison = async () => {
    if (selectedRunIds.length < 2) return;
    try {
      setComparisonResult(await compareRuns(selectedRunIds));
    } catch {
      setAlertModal({ title: t("common.error"), message: t("benchmarking.compareFailed") });
    }
  };

  const renderDiff = (pct: number) => {
    if (pct === 0) return <span className="text-muted">—</span>;
    return (
      <span className={`inline-flex items-center gap-0.5 ${pct > 0 ? "text-good" : "text-bad"}`}>
        {pct > 0 ? <TrendingUp size={14} /> : <TrendingDown size={14} />}
        {formatNumber(Math.abs(pct))}%
      </span>
    );
  };

  // ── History ────────────────────────────────────────────────────────────────

  const renderComparison = () =>
    comparisonResult && (
      <div className="space-y-3 rounded-md border border-line p-4" data-testid="comparison">
        <div className="flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-[17px] font-semibold tracking-[-0.02em]">
            <BarChart3 size={20} className="text-blue2" />
            {t("benchmarking.comparison")}
          </h3>
          <IconButton
            size="sm"
            icon={X}
            label={t("common.close")}
            onClick={() => setComparisonResult(null)}
            className="border-transparent text-muted hover:border-line hover:text-text"
          />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-[14px]">
            <thead>
              <tr className="border-b border-line">
                <th className="px-4 py-3 text-left font-medium text-muted">
                  {t("benchmarking.colMetric")}
                </th>
                {comparisonResult.run_ids.map((rid) => (
                  <th
                    key={rid}
                    className="min-w-[150px] px-4 py-3 text-left font-medium text-muted"
                  >
                    {comparisonResult.runs[rid]?.recipe_name || rid.slice(0, 8)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {Object.entries(comparisonResult.comparison).map(([metric, comp]: [string, any]) => {
                const values = comp.values;
                const differences = comp.differences || {};
                return (
                  <tr key={metric} className="border-b border-line">
                    <td className="px-4 py-3 font-medium">{metric.replace(/_/g, " ")}</td>
                    {Object.entries(values).map(([rid, v]: [string, any]) => {
                      const diffs = Object.entries(differences).filter(([k]) => k.startsWith(rid));
                      return (
                        <td key={rid} className="px-4 py-3 font-mono">
                          <div>{formatNumber(v.value, 2)}</div>
                          {diffs.map(([diffKey, diff]: [string, any]) => (
                            <div key={diffKey} className="text-[13px] text-muted">
                              {renderDiff(diff.difference_pct)} vs {diffKey.replace(rid + "_vs_", "")}
                            </div>
                          ))}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    );

  const renderHistory = () => (
    <div className="space-y-3">
      {selectedRunIds.length >= 2 && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-line p-3">
          <p className="text-[14px] font-medium">
            {plural("runs.selected", selectedRunIds.length)}
          </p>
          <div className="flex items-center gap-2">
            <Button size="sm" variant="primary" icon={BarChart3} onClick={runComparison}>
              {t("runs.compare")}
            </Button>
            <Button size="sm" onClick={() => setSelectedRunIds([])}>
              {t("common.clear")}
            </Button>
          </div>
        </div>
      )}

      {renderComparison()}

      <ErrorLine>{error}</ErrorLine>
      {benchmarks && benchmarks.length > 0 && (
        <div className="space-y-2">
          {benchmarks.map((bench) => (
            <div
              key={bench.benchmark_id}
              className="flex flex-wrap items-center gap-3 rounded-md border border-line bg-surface p-4"
            >
              <input
                type="checkbox"
                aria-label={runLabel(bench)}
                checked={selectedRunIds.includes(bench.benchmark_id)}
                onChange={() => toggleSelectRun(bench.benchmark_id)}
                className="cursor-pointer accent-[var(--blue)]"
              />
              <Flame size={16} className="shrink-0 text-blue2" />
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium">{runLabel(bench)}</p>
                <p className="text-[13px] text-muted">
                  {new Date(bench.started_at).toLocaleDateString()}{" "}
                  {new Date(bench.started_at).toLocaleTimeString()}
                  {bench.recipe_id && <span className="ml-2">recipe: {bench.recipe_id}</span>}
                </p>
              </div>
              <StatusBadge status={bench.status} />
              {bench.baseline_id && (
                <span className="shrink-0 rounded-full border border-line px-2 py-0.5 text-[13px] font-medium text-muted">
                  {t("runs.vsBaseline")}
                </span>
              )}
              <IconButton
                size="sm"
                icon={Trash2}
                label={t("benchmarking.delete")}
                onClick={() => setDeleteTarget(bench)}
                className="border-transparent text-muted hover:border-line hover:text-bad"
              />
            </div>
          ))}
        </div>
      )}
      {benchmarks && benchmarks.length === 0 && !loading && !error && (
        <EmptyState icon={Flame} hint={t("benchmarking.emptyHint")}>
          {t("benchmarking.empty")}
        </EmptyState>
      )}
    </div>
  );

  // ── Summary ────────────────────────────────────────────────────────────────

  const renderSummary = () => (
    <div className="space-y-3">
      {latestLoading && (
        <div className="flex justify-center py-20">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      )}
      {latestByRecipe && Object.keys(latestByRecipe).length > 0 && (
        <>
          {/* Nine columns do not survive 390px however they are scrolled: the
              model name goes off screen with the numbers, so the reader is
              swiping a table with no row labels. Under 900 each recipe is a
              card with its own numbers named. */}
          <div className="space-y-2 min-[900px]:hidden" data-testid="summary-cards">
            {Object.entries(latestByRecipe).map(([recipeId, bench]) => {
              const results = bench.results as Record<string, unknown> | null;
              return (
                <div key={recipeId} className="rounded-md border border-line bg-surface p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="truncate font-medium">{bench.recipe_name || recipeId}</p>
                    <StatusBadge status={bench.status} />
                  </div>
                  <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-[13px]">
                    {SUMMARY_COLUMNS.map((column) => (
                      <div key={column.key} className="flex justify-between gap-2">
                        <dt className="text-muted">{t(column.label)}</dt>
                        <dd className="font-mono tabular-nums">{cell(results, column)}</dd>
                      </div>
                    ))}
                  </dl>
                  <p className="mt-2 text-[13px] text-muted">
                    {t("benchmarking.colBenchmarked")}{" "}
                    {new Date(bench.started_at).toLocaleDateString()}
                  </p>
                </div>
              );
            })}
          </div>

          <div className="hidden overflow-x-auto min-[900px]:block" data-testid="summary-table">
            <table className="w-full text-[14px]">
              <thead>
                <tr className="border-b border-line">
                  <th className="px-4 py-3 text-left font-medium text-muted">
                    {t("benchmarking.colModel")}
                  </th>
                  {SUMMARY_COLUMNS.map((column) => (
                    <th key={column.key} className="px-4 py-3 text-left font-medium text-muted">
                      {t(column.label)}
                    </th>
                  ))}
                  <th className="px-4 py-3 text-left font-medium text-muted">
                    {t("benchmarking.colBenchmarked")}
                  </th>
                  <th className="px-4 py-3 text-left font-medium text-muted">
                    {t("benchmarking.colStatus")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(latestByRecipe).map(([recipeId, bench]) => {
                  const results = bench.results as Record<string, unknown> | null;
                  return (
                    <tr key={recipeId} className="border-b border-line">
                      <td className="max-w-[200px] truncate px-4 py-3 font-medium">
                        {bench.recipe_name || recipeId}
                      </td>
                      {SUMMARY_COLUMNS.map((column) => (
                        <td key={column.key} className="px-4 py-3 font-mono tabular-nums">
                          {cell(results, column)}
                        </td>
                      ))}
                      <td className="px-4 py-3 text-muted">
                        {new Date(bench.started_at).toLocaleDateString()}
                      </td>
                      <td className="px-4 py-3">
                        <StatusBadge status={bench.status} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
      {latestByRecipe && Object.keys(latestByRecipe).length === 0 && !latestLoading && (
        <EmptyState icon={TableIcon} hint={t("benchmarking.noDataHint")}>
          {t("benchmarking.noData")}
        </EmptyState>
      )}
    </div>
  );

  return (
    <div className="space-y-4">
      <Tabs
        label={t("benchmarking.title")}
        value={view}
        onChange={(id) => setView(id === "summary" ? "summary" : "history")}
        tabs={[
          { id: "history", label: t("benchmarking.history"), count: benchmarks?.length },
          {
            id: "summary",
            label: t("benchmarking.summary"),
            count: latestByRecipe ? Object.keys(latestByRecipe).length : undefined,
          },
        ]}
      />

      {loading && (
        <div className="flex justify-center py-20">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      )}
      {view === "history" ? renderHistory() : renderSummary()}

      {deleteTarget && (
        <ConfirmModal
          open={!!deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onConfirm={handleDelete}
          title={t("benchmarking.deleteTitle")}
          message={t("benchmarking.deleteMessage", { name: runLabel(deleteTarget) })}
          confirmLabel={t("common.delete")}
          confirmVariant="danger"
        />
      )}

      {alertModal && (
        <AlertModal
          open={!!alertModal}
          onClose={() => setAlertModal(null)}
          title={alertModal.title}
          message={alertModal.message}
        />
      )}
    </div>
  );
}
