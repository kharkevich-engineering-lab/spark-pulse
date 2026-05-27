import { useEffect, useState } from "react";
import {
  fetchBenchmarks,
  fetchLatestByRecipe,
  runBenchmark,
  compareRuns,
} from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import StatusBadge from "@/components/StatusBadge";
import { AlertModal } from "@/components/Modal";
import {
  Flame, Loader2, AlertCircle, TrendingUp,
  TrendingDown,
  X, Play, BarChart3, Table as TableIcon,
} from "lucide-react";
import { setRefresh } from "@/lib/refresh";
import type { BenchmarkResult } from "@/lib/types";

type Tab = "history" | "summary";

export default function BenchmarkingPage() {
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

  useEffect(() => { setRefresh(refetch); }, [refetch]);

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
        message: e instanceof Error ? e.message : "Failed to run benchmark",
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
      setAlertModal({ title: "Error", message: "Failed to compare benchmarks" });
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
        <div className="flex items-center justify-between p-3 rounded-lg bg-primary/5 border border-primary/20">
          <p className="text-sm text-primary font-medium">
            {selectedRunIds.length} run(s) selected
          </p>
          <div className="flex items-center gap-2">
            <button onClick={runComparison} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-primary text-white text-sm font-medium hover:bg-primary-hover transition-colors">
              <BarChart3 size={14} />
              Compare Selected
            </button>
            <button onClick={() => setSelectedRunIds([])} className="px-3 py-1.5 rounded-lg text-sm text-text-muted hover:bg-surface-hover transition-colors">
              Clear
            </button>
          </div>
        </div>
      )}

      {error && <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3"><AlertCircle size={20} /><span>{error}</span></div>}
      {benchmarks && benchmarks.length > 0 && (
        <div className="space-y-2">
          {benchmarks.map((bench) => {
            const isSelected = selectedRunIds.includes(bench.benchmark_id);
            return (
              <div key={bench.benchmark_id} className="rounded-xl bg-surface border border-border overflow-hidden">
                <div className="flex items-center gap-3 p-4">
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={() => toggleSelectRun(bench.benchmark_id)}
                    className="rounded border-border text-primary focus:ring-primary cursor-pointer"
                  />
                  <Flame size={16} className="text-primary shrink-0" />
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
                    <span className="text-xs px-2 py-0.5 rounded-full bg-primary/10 text-primary font-medium shrink-0">
                      vs baseline
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
      {benchmarks && benchmarks.length === 0 && !loading && !error && (
        <div className="text-center py-20 text-text-muted">
          <Flame size={40} className="mx-auto mb-4 opacity-50" />
          <p>No benchmarks run yet.</p>
          <p className="text-sm mt-1">Click "Run Benchmark" to analyze a running deployment.</p>
        </div>
      )}
    </div>
  );

  const renderSummaryTab = () => (
    <div className="space-y-3">
      {latestLoading && <div className="flex justify-center py-20"><Loader2 className="animate-spin text-primary" size={32} /></div>}
      {latestByRecipe && Object.keys(latestByRecipe).length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border">
                <th className="text-left py-3 px-4 font-medium text-text-muted">Model</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">Throughput</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">Latency</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">Decode Latency</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">GPU Memory</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">GPU Util</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">Pre-fill Speed</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">Benchmarked</th>
                <th className="text-left py-3 px-4 font-medium text-text-muted">Status</th>
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
        <div className="text-center py-20 text-text-muted">
          <TableIcon size={40} className="mx-auto mb-4 opacity-50" />
          <p>No benchmark data yet.</p>
          <p className="text-sm mt-1">Run benchmarks to see model comparison here.</p>
        </div>
      )}
    </div>
  );

  // ── Main render ────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold">Benchmarking</h2>
          <p className="text-text-muted mt-1">Model performance analysis and comparison</p>
        </div>
        <button
          onClick={() => setShowRunModal(true)}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-primary hover:bg-primary-hover text-white text-sm font-medium transition-colors"
        >
          <Play size={16} />
          Run Benchmark
        </button>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-border">
        <button
          onClick={() => { setActiveTab("history"); setShowComparison(false); }}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            activeTab === "history" && !showComparison
              ? "border-primary text-primary"
              : "border-transparent text-text-muted hover:text-text"
          }`}
        >
          <span className="flex items-center gap-1.5">
            <Flame size={16} />
            History
            {benchmarks && <span className="text-xs opacity-60">({benchmarks.length})</span>}
          </span>
        </button>
        <button
          onClick={() => { setActiveTab("summary"); setShowComparison(false); }}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            activeTab === "summary" && !showComparison
              ? "border-primary text-primary"
              : "border-transparent text-text-muted hover:text-text"
          }`}
        >
          <span className="flex items-center gap-1.5">
            <TableIcon size={16} />
            Summary
            {latestByRecipe && <span className="text-xs opacity-60">({Object.keys(latestByRecipe).length})</span>}
          </span>
        </button>
        <button
          onClick={() => { setShowComparison(false); setActiveTab("history"); }}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            showComparison
              ? "border-primary text-primary"
              : "border-transparent text-text-muted hover:text-text"
          }`}
        >
          <span className="flex items-center gap-1.5">
            <BarChart3 size={16} />
            Comparison
          </span>
        </button>
      </div>

      {/* Tab content */}
      {loading && <div className="flex justify-center py-20"><Loader2 className="animate-spin text-primary" size={32} /></div>}
      {activeTab === "history" && !showComparison && renderHistoryTab()}
      {activeTab === "summary" && renderSummaryTab()}

      {/* Comparison view */}
      {showComparison && comparisonResult && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-lg font-bold flex items-center gap-2">
              <BarChart3 size={20} className="text-primary" />
              Run Comparison
            </h3>
            <button onClick={() => { setShowComparison(false); setComparisonResult(null); }} className="p-1 rounded hover:bg-surface-hover">
              <X size={18} />
            </button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left py-3 px-4 font-medium text-text-muted">Metric</th>
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
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="rounded-xl bg-surface border border-border w-full max-w-md p-6 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-lg font-bold flex items-center gap-2">
                <Flame size={20} className="text-primary" />
                Run Benchmark
              </h3>
              <button onClick={() => setShowRunModal(false)} className="p-1 rounded hover:bg-surface-hover">
                <X size={18} />
              </button>
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Target Deployment *</label>
              <input type="text" value={runTarget} onChange={(e) => setRunTarget(e.target.value)} placeholder="deployment-id" className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Recipe/Model ID</label>
              <input type="text" value={runRecipeId} onChange={(e) => setRunRecipeId(e.target.value)} placeholder="qwen3.5-397b-int4" className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Baseline (optional)</label>
              <input type="text" value={runBaseline} onChange={(e) => setRunBaseline(e.target.value)} placeholder="benchmark-id" className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" />
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Benchmark Types</label>
              <div className="flex flex-wrap gap-2">
                {["throughput", "latency", "gpu_memory", "gpu_utilization", "prefill_speed"].map((type) => (
                  <label key={type} className="flex items-center gap-1.5 text-sm cursor-pointer">
                    <input type="checkbox" checked={(runParams.benchmarks as string[] || []).includes(type)} onChange={(e) => {
                      const current = (runParams.benchmarks as string[]) || [];
                      const next = e.target.checked ? [...current, type] : current.filter((t: string) => t !== type);
                      setRunParams({ ...runParams, benchmarks: next });
                    }} className="rounded border-border text-primary focus:ring-primary" />
                    <span className="text-text-muted">{type.replace(/_/g, " ")}</span>
                  </label>
                ))}
              </div>
            </div>
            <div>
              <label className="block text-sm font-medium mb-1">Context Length</label>
              <input type="number" value={Number(runParams.context_length) || 4096} onChange={(e) => setRunParams({ ...runParams, context_length: parseInt(e.target.value) || 4096 })} className="w-32 px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" />
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <button onClick={() => setShowRunModal(false)} className="px-4 py-2 rounded-lg border border-border text-sm hover:bg-surface-hover transition-colors">Cancel</button>
              <button onClick={handleRun} disabled={!runTarget || isRunning} className="flex items-center gap-2 px-4 py-2 rounded-lg bg-primary hover:bg-primary-hover text-white text-sm font-medium transition-colors disabled:opacity-50">
                {isRunning && <Loader2 size={16} className="animate-spin" />}
                {isRunning ? "Running..." : "Run"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Alert modal */}
      {alertModal && (
        <AlertModal open={!!alertModal} onClose={() => setAlertModal(null)} title={alertModal.title} message={alertModal.message} />
      )}
    </div>
  );
}
