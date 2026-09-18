import { useState } from "react";
import { useI18n } from "@/lib/i18n";
import {
  fetchBenchmarks,
  fetchLatestByRecipe,
  runBenchmark,
  compareRuns,
  deleteBenchmark,
} from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import {
  AlertModal,
  Button,
  ConfirmModal,
  EmptyState,
  ErrorLine,
  Field,
  IconButton,
  Input,
  Modal,
  Spinner,
  StatusBadge,
  Tabs,
} from "@/ui";
import {
  Flame, TrendingUp,
  TrendingDown,
  X, Play, BarChart3, Table as TableIcon, Trash2,
} from "lucide-react";
import type { BenchmarkResult } from "@/lib/types";

type Tab = "history" | "summary";

export default function BenchmarkingPage() {
  const { t } = useI18n();
  const [activeTab, setActiveTab] = useState<Tab>("history");
  const { data: benchmarks, loading, error, refetch } = useQuery(fetchBenchmarks);
  const { data: latestByRecipe, loading: latestLoading } = useQuery(fetchLatestByRecipe);
  const [showRunModal, setShowRunModal] = useState(false);
  const [runTarget, setRunTarget] = useState("");
  const [runBaseline, setRunBaseline] = useState("");
  const [runRecipeId, setRunRecipeId] = useState("");
  const [runParams, setRunParams] = useState<Record<string, unknown>>({
    benchmarks: ["throughput", "latency"],
    context_length: 4096,
  });
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);
  const [isRunning, setIsRunning] = useState(false);

  // Comparison state
  const [selectedRunIds, setSelectedRunIds] = useState<string[]>([]);
  const [comparisonResult, setComparisonResult] = useState<{
    runs: Record<string, BenchmarkResult>;
    comparison: Record<string, any>;
    run_ids: string[];
  } | null>(null);
  const [showComparison, setShowComparison] = useState(false);

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

  const handleRun = async () => {
    if (!runTarget) return;
    setIsRunning(true);
    try {
      await runBenchmark({
        deployment_id: runTarget,
        baseline_id: runBaseline || undefined,
        recipe_id: runRecipeId,
        recipe_name: runRecipeId,
        params: runParams,
      });
      setShowRunModal(false);
      setRunTarget("");
      setRunBaseline("");
      setRunRecipeId("");
      refetch();
    } catch (e) {
      setAlertModal({
        title: "Error",
        message: e instanceof Error ? e.message : t("benchmarking.runFailed"),
      });
    } finally {
      setIsRunning(false);
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
      const result = await compareRuns(selectedRunIds);
      setComparisonResult(result);
      setShowComparison(true);
    } catch {
      setAlertModal({ title: t("common.error"), message: t("benchmarking.compareFailed") });
    }
  };

  const formatNumber = (n: number, decimals = 1) => n.toFixed(decimals);

  const renderDiff = (pct: number) => {
    if (pct === 0) return <span className="text-text-muted">—</span>;
    return (
      <span className={`inline-flex items-center gap-0.5 ${pct > 0 ? "text-success" : "text-danger"}`}>
        {pct > 0 ? <TrendingUp size={14} /> : <TrendingDown size={14} />}
        {formatNumber(Math.abs(pct))}%
      </span>
    );
  };

  // ── Render helpers ─────────────────────────────────────────────────────────

  const renderHistoryTab = () => (
    <div className="space-y-3">
      {selectedRunIds.length >= 2 && (
        <div className="flex items-center justify-between gap-3 p-3 rounded-md border border-line">
          <p className="text-[14px] font-medium">{selectedRunIds.length} run(s) selected</p>
          <div className="flex items-center gap-2">
            <Button size="sm" variant="primary" icon={BarChart3} onClick={runComparison}>
              Compare Selected
            </Button>
            <Button size="sm" onClick={() => setSelectedRunIds([])}>
              {t("common.clear")}
            </Button>
          </div>
        </div>
      )}

      <ErrorLine>{error}</ErrorLine>
      {benchmarks && benchmarks.length > 0 && (
        <div className="space-y-2">
          {benchmarks.map((bench) => {
            const isSelected = selectedRunIds.includes(bench.benchmark_id);
            return (
              <div key={bench.benchmark_id} className="rounded-md bg-surface border border-line overflow-hidden">
                <div className="flex items-center gap-3 p-4">
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={() => toggleSelectRun(bench.benchmark_id)}
                    className="accent-[var(--blue)] cursor-pointer"
                  />
                  <Flame size={16} className="text-blue2 shrink-0" />
                  <div className="flex-1 min-w-0">
                    <p className="font-medium truncate">
                      {bench.recipe_name || bench.recipe_id || bench.benchmark_id.slice(0, 8)}
                    </p>
                    <p className="text-xs text-text-muted">
                      {new Date(bench.started_at).toLocaleDateString()} {new Date(bench.started_at).toLocaleTimeString()}
                      {bench.recipe_id && <span className="ml-2 text-text-muted/60">recipe: {bench.recipe_id}</span>}
                    </p>
                  </div>
                  <StatusBadge status={bench.status} />
                  {bench.baseline_id && (
                    <span className="text-[13px] px-2 py-0.5 rounded-full border border-line text-muted font-medium shrink-0">
                      vs baseline
                    </span>
                  )}
                  <IconButton
                    size="sm"
                    icon={Trash2}
                    label={t("benchmarking.delete")}
                    onClick={() => setDeleteTarget(bench)}
                    className="border-transparent text-muted hover:text-bad hover:border-line"
                  />
                </div>
              </div>
            );
          })}
        </div>
      )}
      {benchmarks && benchmarks.length === 0 && !loading && !error && (
        <EmptyState icon={Flame} hint={t("benchmarking.emptyHint")}>
          {t("benchmarking.empty")}
        </EmptyState>
      )}
    </div>
  );

  const renderSummaryTab = () => (
    <div className="space-y-3">
      {latestLoading && (
        <div className="flex justify-center py-20">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      )}
      {latestByRecipe && Object.keys(latestByRecipe).length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border">
                <th className="text-left py-3 px-4 font-medium text-text-muted">{t("benchmarking.colModel")}</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">{t("benchmarking.colThroughput")}</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">{t("benchmarking.colLatency")}</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">{t("benchmarking.colDecode")}</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">{t("benchmarking.colGpuMemory")}</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">{t("benchmarking.colGpuUtil")}</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">{t("benchmarking.colPrefill")}</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">{t("benchmarking.colBenchmarked")}</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">{t("benchmarking.colStatus")}</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(latestByRecipe).map(([recipeId, bench]) => {
                const results = bench.results as Record<string, unknown> | null;
                return (
                  <tr key={recipeId} className="border-b border-border hover:bg-surface-hover transition-colors">
                    <td className="py-3 px-4 font-medium truncate max-w-[200px]">
                      {bench.recipe_name || recipeId}
                    </td>
                    <td className="py-3 px-4 font-mono">
                      {results?.throughput != null ? formatNumber(results.throughput as number) : "—"}
                    </td>
                    <td className="py-3 px-4 font-mono">
                      {results?.latency_ms != null ? formatNumber(results.latency_ms as number) : "—"}
                    </td>
                    <td className="py-3 px-4 font-mono">
                      {results?.decode_latency_ms != null ? formatNumber(results.decode_latency_ms as number) : "—"}
                    </td>
                    <td className="py-3 px-4 font-mono">
                      {results?.gpu_memory_gb != null ? formatNumber(results.gpu_memory_gb as number) : "—"}
                    </td>
                    <td className="py-3 px-4 font-mono">
                      {results?.gpu_utilization != null ? `${formatNumber(results.gpu_utilization as number, 0)}%` : "—"}
                    </td>
                    <td className="py-3 px-4 font-mono">
                      {results?.prefill_speed != null ? formatNumber(results.prefill_speed as number) : "—"}
                    </td>
                    <td className="py-3 px-4 text-text-muted">
                      {new Date(bench.started_at).toLocaleDateString()}
                    </td>
                    <td className="py-3 px-4">
                      <StatusBadge status={bench.status} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {latestByRecipe && Object.keys(latestByRecipe).length === 0 && !latestLoading && (
        <EmptyState icon={TableIcon} hint={t("benchmarking.noDataHint")}>
          {t("benchmarking.noData")}
        </EmptyState>
      )}
    </div>
  );

  // ── Main render ────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold">{t("benchmarking.title")}</h2>
          <p className="text-text-muted mt-1">{t("benchmarking.subtitle")}</p>
        </div>
        <Button variant="primary" icon={Play} onClick={() => setShowRunModal(true)}>
          {t("benchmarking.run")}
        </Button>
      </div>

      {/* Tabs */}
      <Tabs
        label={t("benchmarking.title")}
        value={showComparison ? "comparison" : activeTab}
        // The comparison pill is a view rather than a list: it lights up once
        // two runs have been compared, and clicking it puts the comparison
        // away again. That is what it always did; only the shape has changed.
        onChange={(id) => {
          setShowComparison(false);
          setActiveTab(id === "summary" ? "summary" : "history");
        }}
        tabs={[
          { id: "history", label: t("benchmarking.history"), count: benchmarks?.length },
          {
            id: "summary",
            label: t("benchmarking.summary"),
            count: latestByRecipe ? Object.keys(latestByRecipe).length : undefined,
          },
          { id: "comparison", label: t("benchmarking.comparison") },
        ]}
      />

      {/* Tab content */}
      {loading && (
        <div className="flex justify-center py-20">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      )}
      {activeTab === "history" && !showComparison && renderHistoryTab()}
      {activeTab === "summary" && renderSummaryTab()}

      {/* Comparison view */}
      {showComparison && comparisonResult && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-[17px] font-semibold flex items-center gap-2">
              <BarChart3 size={20} className="text-blue2" />
              Run Comparison
            </h3>
            <IconButton
              size="sm"
              icon={X}
              label={t("common.close")}
              onClick={() => { setShowComparison(false); setComparisonResult(null); }}
              className="border-transparent text-muted hover:text-text hover:border-line"
            />
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left py-3 px-4 font-medium text-text-muted">{t("benchmarking.colMetric")}</th>
                  {comparisonResult.run_ids.map((rid) => (
                    <th key={rid} className="text-left py-3 px-4 font-medium text-text-muted min-w-[150px]">
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
                    <tr key={metric} className="border-b border-border">
                      <td className="py-3 px-4 font-medium">{metric.replace(/_/g, " ")}</td>
                      {Object.entries(values).map(([rid, v]: [string, any]) => {
                        const diffs = Object.entries(differences).filter(([k]) => k.startsWith(rid));
                        return (
                          <td key={rid} className="py-3 px-4 font-mono">
                            <div>{formatNumber(v.value, 2)}</div>
                            {diffs.map(([diffKey, diff]: [string, any]) => (
                              <div key={diffKey} className="text-xs text-text-muted">
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
      )}

      {/* Run Benchmark Modal */}
      {showRunModal && (
        <Modal
          open
          onClose={() => setShowRunModal(false)}
          title="Run Benchmark"
          icon={<Flame size={20} className="text-blue2" />}
          actions={
            <>
              <Button size="sm" onClick={() => setShowRunModal(false)}>
                {t("common.cancel")}
              </Button>
              <Button
                size="sm"
                variant="primary"
                loading={isRunning}
                disabled={!runTarget}
                onClick={handleRun}
              >
                {isRunning ? "Running..." : "Run"}
              </Button>
            </>
          }
        >
          <div className="space-y-4">
            <Field label={t("benchmarking.target")}>
              {(control) => (
                <Input
                  {...control}
                  mono
                  type="text"
                  value={runTarget}
                  onChange={(e) => setRunTarget(e.target.value)}
                  placeholder={t("benchmarking.targetPlaceholder")}
                />
              )}
            </Field>
            <Field label={t("benchmarking.recipeId")}>
              {(control) => (
                <Input
                  {...control}
                  mono
                  type="text"
                  value={runRecipeId}
                  onChange={(e) => setRunRecipeId(e.target.value)}
                  placeholder={t("benchmarking.recipeIdPlaceholder")}
                />
              )}
            </Field>
            <Field label={t("benchmarking.baseline")}>
              {(control) => (
                <Input
                  {...control}
                  mono
                  type="text"
                  value={runBaseline}
                  onChange={(e) => setRunBaseline(e.target.value)}
                  placeholder={t("benchmarking.baselinePlaceholder")}
                />
              )}
            </Field>
            <div>
              <p className="block text-[13px] font-medium mb-1.5">{t("benchmarking.types")}</p>
              <div className="flex flex-wrap gap-2">
                {["throughput", "latency", "gpu_memory", "gpu_utilization", "prefill_speed"].map((type) => (
                  <label key={type} className="flex items-center gap-1.5 text-[14px] cursor-pointer">
                    <input
                      type="checkbox"
                      className="accent-[var(--blue)]"
                      checked={((runParams.benchmarks as string[]) || []).includes(type)}
                      onChange={(e) => {
                        const current = (runParams.benchmarks as string[]) || [];
                        const next = e.target.checked
                          ? [...current, type]
                          : current.filter((t: string) => t !== type);
                        setRunParams({ ...runParams, benchmarks: next });
                      }}
                    />
                    <span className="text-muted">{type.replace(/_/g, " ")}</span>
                  </label>
                ))}
              </div>
            </div>
            <Field label={t("benchmarking.contextLength")}>
              {(control) => (
                <Input
                  {...control}
                  mono
                  type="number"
                  className="w-32"
                  value={Number(runParams.context_length) || 4096}
                  onChange={(e) =>
                    setRunParams({ ...runParams, context_length: parseInt(e.target.value) || 4096 })
                  }
                />
              )}
            </Field>
          </div>
        </Modal>
      )}

      {/* Delete confirmation */}
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

      {/* Alert modal */}
      {alertModal && (
        <AlertModal open={!!alertModal} onClose={() => setAlertModal(null)} title={alertModal.title} message={alertModal.message} />
      )}
    </div>
  );
}
