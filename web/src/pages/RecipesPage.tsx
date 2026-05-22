import { useState, useEffect, useMemo } from "react";
import { fetchRecipes, fetchRecipe, fetchDeployments, createDeployment, fetchSettings } from "@/lib/api";
import type { RecipeDetail } from "@/lib/types";
import { useQuery } from "@/hooks/useQuery";
import { AlertModal } from "@/components/Modal";
import { Rocket, Box, Layers, Terminal, Info, ArrowRight, Loader2, AlertCircle, Network, Cpu, ChevronDown } from "lucide-react";
import { Link } from "react-router-dom";
import { setRefresh } from "@/lib/refresh";

export default function RecipesPage() {
  const { data: recipes, loading, error, refetch } = useQuery(fetchRecipes);
  const { data: deployments } = useQuery(fetchDeployments);
  const { data: settings } = useQuery(fetchSettings);
  const [selected, setSelected] = useState<RecipeDetail | null>(null);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);
  const [showUnavailable, setShowUnavailable] = useState(false);

  const clusterEnabled = settings?.cluster_enabled ?? false;

  const duplicateNames = useMemo(() => {
    if (!recipes) return new Set<string>();
    const counts = new Map<string, number>();
    recipes.forEach((recipe) => counts.set(recipe.name, (counts.get(recipe.name) ?? 0) + 1));
    return new Set<string>([...counts.entries()].filter(([, count]) => count > 1).map(([name]) => name));
  }, [recipes]);

  const runningIds = useMemo(() => {
    if (!deployments) return new Set<string>();
    return new Set(deployments.filter(d => d.status === "running" || d.status === "pending").map(d => d.recipe_id));
  }, [deployments]);

  const { available, unavailable } = useMemo(() => {
    if (!recipes) return { available: [], unavailable: [] };
    const avail = recipes.filter(r => !(r.cluster_only && !clusterEnabled));
    const unavail = recipes.filter(r => r.cluster_only && !clusterEnabled);
    return { available: avail, unavailable: unavail };
  }, [recipes, clusterEnabled]);

  // Register refresh callback with header
  useEffect(() => { setRefresh(refetch); }, [refetch]);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">Recipes</h2>
        <p className="text-text-muted mt-1">Available deployment recipes for your Spark hardware</p>
      </div>

      {loading && <div className="flex justify-center py-20"><Loader2 className="animate-spin text-primary" size={32} /></div>}
      {error && <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3"><AlertCircle size={20} /><span>{error}</span></div>}

      {recipes && recipes.length > 0 && (
        <div className="space-y-6">
          {/* Available recipes */}
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {available.map((r) => <RecipeCard key={r.id} r={r} isRunning={runningIds.has(r.id)} clusterBlocked={false} duplicateNames={duplicateNames} onSelect={() => fetchRecipe(r.id).then(setSelected)} />)}
          </div>

          {/* Unavailable / cluster-only */}
          {unavailable.length > 0 && (
            <div>
              <button
                onClick={() => setShowUnavailable(v => !v)}
                className="flex items-center gap-2 text-sm text-text-muted hover:text-text transition-colors mb-3"
              >
                <ChevronDown size={16} className={`transition-transform ${showUnavailable ? "rotate-180" : ""}`} />
                {showUnavailable ? "Hide" : "Show"} {unavailable.length} unavailable recipe{unavailable.length > 1 ? "s" : ""} (cluster only)
              </button>
              {showUnavailable && (
                <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                  {unavailable.map((r) => <RecipeCard key={r.id} r={r} isRunning={false} clusterBlocked={true} duplicateNames={duplicateNames} onSelect={() => {}} />)}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {recipes && recipes.length === 0 && !loading && !error && (
        <div className="text-center py-20 text-text-muted"><Terminal size={40} className="mx-auto mb-4 opacity-50" /><p>No recipes found.</p><p className="text-sm mt-1">Check spark-vllm-docker path in Settings.</p></div>
      )}

      {selected && <RecipeModal recipe={selected} isRunning={runningIds.has(selected.id)} clusterEnabled={clusterEnabled} onClose={() => setSelected(null)} onError={(msg) => setAlertModal({ title: "Error", message: msg })} />}

      {/* Alert modal */}
      {alertModal && (
        <AlertModal open={!!alertModal} onClose={() => setAlertModal(null)} title={alertModal.title} message={alertModal.message} />
      )}
    </div>
  );
}

import type { RecipeSummary } from "@/lib/types";

function RecipeCard({ r, isRunning, clusterBlocked, duplicateNames, onSelect }: {
  r: RecipeSummary; isRunning: boolean; clusterBlocked: boolean;
  duplicateNames: Set<string>; onSelect: () => void;
}) {
  return (
    <div onClick={() => !clusterBlocked && onSelect()}
      className={`p-5 rounded-xl bg-surface border transition-colors ${clusterBlocked ? "opacity-50 cursor-not-allowed border-border" : isRunning ? "border-success/50 hover:border-success/80 cursor-pointer group" : "border-border hover:border-border-hover cursor-pointer group"}`}>
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="font-semibold text-base group-hover:text-primary transition-colors">{r.name}</h3>
          <p className="text-xs text-text-muted mt-1 font-mono">{duplicateNames.has(r.name) ? r.id : r.model}</p>
        </div>
        {isRunning
          ? <span className="flex items-center gap-1.5 text-xs text-success font-medium shrink-0"><span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />Running</span>
          : clusterBlocked
          ? <span className="text-xs text-text-muted shrink-0">Cluster only</span>
          : <ArrowRight size={16} className="text-text-muted group-hover:text-primary transition-colors shrink-0" />}
      </div>
      <p className="text-sm text-text-muted mb-4 line-clamp-2">{r.description || r.model}</p>
      <div className="flex flex-wrap gap-2">
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-text-muted"><Box size={12} />{r.container}</span>
        {(r.solo_only || (!r.solo_only && !r.cluster_only)) && <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-primary/20 text-primary"><Cpu size={11} />Solo</span>}
        {(r.cluster_only || (!r.solo_only && !r.cluster_only)) && <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-warning/20 text-warning"><Network size={11} />Cluster</span>}
      </div>
      {r.mods.length > 0 && <div className="mt-3 flex items-center gap-1 text-xs text-text-muted"><Layers size={12} /><span>{r.mods.length} mod{r.mods.length > 1 ? "s" : ""}</span></div>}
    </div>
  );
}

function RecipeModal({ recipe, isRunning, clusterEnabled, onClose, onError }: { recipe: RecipeDetail; isRunning: boolean; clusterEnabled: boolean; onClose: () => void; onError: (msg: string) => void }) {
  const [name, _setName] = useState(recipe.name);
  const [params, _setParams] = useState<Record<string, unknown>>({ ...recipe.defaults });
  const [submitting, setSubmitting] = useState(false);

  const clusterBlocked = recipe.cluster_only && !clusterEnabled;

  const launch = async () => {
    setSubmitting(true);
    try {
      await createDeployment({ recipe_id: recipe.id, name, params });
      window.location.href = "/jobs";
    } catch (e) { onError(e instanceof Error ? e.message : "Failed to launch"); }
    finally { setSubmitting(false); }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div className="relative w-full max-w-2xl max-h-[80vh] overflow-auto rounded-xl bg-surface border border-border p-6">
        <div className="flex items-start justify-between mb-6">
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-xl font-bold">{recipe.name}</h3>
              {isRunning && <span className="flex items-center gap-1.5 text-xs text-success font-medium px-2 py-0.5 rounded-full bg-success/15"><span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />Running</span>}
            </div>
            <p className="text-sm text-text-muted mt-1">{recipe.model}</p>
          </div>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-surface-hover">✕</button>
        </div>

        <div className="space-y-4">
          {recipe.description && <div className="flex items-start gap-3 p-3 rounded-lg bg-bg"><Info size={16} className="text-text-muted mt-0.5 shrink-0" /><p className="text-sm text-text-muted">{recipe.description}</p></div>}

          {clusterBlocked && (
            <div className="flex items-start gap-3 p-3 rounded-lg bg-warning/10 border border-warning/30">
              <Network size={16} className="text-warning mt-0.5 shrink-0" />
              <p className="text-sm text-warning">This recipe requires cluster mode. Enable it in <Link to="/settings" onClick={onClose} className="underline font-medium">Settings</Link>.</p>
            </div>
          )}

          <div className="grid grid-cols-2 gap-4">
            <div className="p-3 rounded-lg bg-bg"><p className="text-xs text-text-muted mb-1">Container</p><p className="font-mono text-sm">{recipe.container}</p></div>
            <div className="p-3 rounded-lg bg-bg"><p className="text-xs text-text-muted mb-1">Mode</p>
              <div className="flex gap-1.5 flex-wrap">
                {(recipe.solo_only || (!recipe.solo_only && !recipe.cluster_only)) && <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-primary/20 text-primary"><Cpu size={11} />Solo</span>}
                {(recipe.cluster_only || (!recipe.solo_only && !recipe.cluster_only)) && <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-warning/20 text-warning"><Network size={11} />Cluster</span>}
              </div>
            </div>
          </div>

          {Object.keys(recipe.defaults).length > 0 && (
            <div><p className="text-sm font-medium mb-2">Default Parameters</p>
              <div className="space-y-1">{Object.entries(recipe.defaults).map(([k, v]) => (<div key={k} className="flex justify-between text-sm px-3 py-1.5 rounded bg-bg"><span className="font-mono text-text-muted">{k}</span><span className="font-mono">{String(v)}</span></div>))}</div>
            </div>
          )}

          {recipe.mods.length > 0 && (<div><p className="text-sm font-medium mb-2">Mods</p><div className="flex flex-wrap gap-2">{recipe.mods.map(m => <Link key={m} to="/mods" className="px-2.5 py-1 rounded text-xs bg-tag-bg font-mono hover:bg-primary/15 hover:text-primary transition-colors">{m}</Link>)}</div></div>)}
          <button onClick={launch} disabled={submitting || isRunning || clusterBlocked}
            className="w-full mt-4 py-2.5 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium transition-colors flex items-center justify-center gap-2">
            {submitting ? <Loader2 className="animate-spin" size={16} /> : <Rocket size={16} />}
            {isRunning ? "Already running" : clusterBlocked ? "Cluster mode disabled" : submitting ? "Deploying…" : "Deploy"}
          </button>
        </div>
      </div>
    </div>
  );
}
