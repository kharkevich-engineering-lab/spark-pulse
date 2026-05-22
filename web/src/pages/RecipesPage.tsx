import { useState, useEffect, useMemo } from "react";
import { fetchRecipes, fetchRecipe, createDeployment } from "@/lib/api";
import type { RecipeDetail } from "@/lib/types";
import { useQuery } from "@/hooks/useQuery";
import { AlertModal } from "@/components/Modal";
import { Rocket, Box, Layers, Terminal, Info, ArrowRight, Loader2, AlertCircle } from "lucide-react";
import { setRefresh } from "@/lib/refresh";

export default function RecipesPage() {
  const { data: recipes, loading, error, refetch } = useQuery(fetchRecipes);
  const [selected, setSelected] = useState<RecipeDetail | null>(null);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);

  const duplicateNames = useMemo(() => {
    if (!recipes) return new Set<string>();
    const counts = new Map<string, number>();
    recipes.forEach((recipe) => counts.set(recipe.name, (counts.get(recipe.name) ?? 0) + 1));
    return new Set<string>([...counts.entries()].filter(([, count]) => count > 1).map(([name]) => name));
  }, [recipes]);

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
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {recipes.map((r) => (
            <div key={r.id} className="p-5 rounded-xl bg-surface border border-border hover:border-border-hover cursor-pointer group" onClick={() => fetchRecipe(r.id).then(setSelected)}>
              <div className="flex items-start justify-between mb-3">
                <div>
                  <h3 className="font-semibold text-base group-hover:text-primary transition-colors">{r.name}</h3>
                  <p className="text-xs text-text-muted mt-1 font-mono">{duplicateNames.has(r.name) ? r.id : r.model}</p>
                </div>
                <ArrowRight size={16} className="text-text-muted group-hover:text-primary transition-colors" />
              </div>
              <p className="text-sm text-text-muted mb-4 line-clamp-2">{r.description || r.model}</p>
              <div className="flex flex-wrap gap-2">
                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-text-muted"><Box size={12} />{r.container}</span>
                {r.solo_only && <span className="px-2 py-0.5 rounded text-xs bg-primary/20 text-primary">Solo</span>}
                {r.cluster_only && <span className="px-2 py-0.5 rounded text-xs bg-warning/20 text-warning">Cluster</span>}
              </div>
              {r.mods.length > 0 && <div className="mt-3 flex items-center gap-1 text-xs text-text-muted"><Layers size={12} /><span>{r.mods.length} mod{r.mods.length > 1 ? "s" : ""}</span></div>}
            </div>
          ))}
        </div>
      )}

      {recipes && recipes.length === 0 && !loading && !error && (
        <div className="text-center py-20 text-text-muted"><Terminal size={40} className="mx-auto mb-4 opacity-50" /><p>No recipes found.</p><p className="text-sm mt-1">Check spark-vllm-docker path in Settings.</p></div>
      )}

      {selected && <RecipeModal recipe={selected} onClose={() => setSelected(null)} onError={(msg) => setAlertModal({ title: "Error", message: msg })} />}

      {/* Alert modal */}
      {alertModal && (
        <AlertModal open={!!alertModal} onClose={() => setAlertModal(null)} title={alertModal.title} message={alertModal.message} />
      )}
    </div>
  );
}

function RecipeModal({ recipe, onClose, onError }: { recipe: RecipeDetail; onClose: () => void; onError: (msg: string) => void }) {
  const [name, _setName] = useState(recipe.name);
  const [params, _setParams] = useState<Record<string, unknown>>({ ...recipe.defaults });
  const [submitting, setSubmitting] = useState(false);

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
          <div><h3 className="text-xl font-bold">{recipe.name}</h3><p className="text-sm text-text-muted mt-1">{recipe.model}</p></div>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-surface-hover">✕</button>
        </div>

        <div className="space-y-4">
          {recipe.description && <div className="flex items-start gap-3 p-3 rounded-lg bg-bg"><Info size={16} className="text-text-muted mt-0.5 shrink-0" /><p className="text-sm text-text-muted">{recipe.description}</p></div>}
          <div className="grid grid-cols-2 gap-4">
            <div className="p-3 rounded-lg bg-bg"><p className="text-xs text-text-muted mb-1">Container</p><p className="font-mono text-sm">{recipe.container}</p></div>
            <div className="p-3 rounded-lg bg-bg"><p className="text-xs text-text-muted mb-1">Mode</p><p className="text-sm">{recipe.solo_only ? "Solo only" : recipe.cluster_only ? "Cluster only" : "Solo or Cluster"}</p></div>
          </div>

          {Object.keys(recipe.defaults).length > 0 && (
            <div><p className="text-sm font-medium mb-2">Default Parameters</p>
              <div className="space-y-1">{Object.entries(recipe.defaults).map(([k, v]) => (<div key={k} className="flex justify-between text-sm px-3 py-1.5 rounded bg-bg"><span className="font-mono text-text-muted">{k}</span><span className="font-mono">{String(v)}</span></div>))}</div>
            </div>
          )}

          {recipe.mods.length > 0 && (<div><p className="text-sm font-medium mb-2">Mods</p><div className="flex flex-wrap gap-2">{recipe.mods.map(m => <span key={m} className="px-2.5 py-1 rounded text-xs bg-tag-bg font-mono">{m}</span>)}</div></div>)}
          <button onClick={launch} disabled={submitting} className="w-full mt-4 py-2.5 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50 text-white font-medium transition-colors flex items-center justify-center gap-2">
            {submitting ? <Loader2 className="animate-spin" size={16} /> : <Rocket size={16} />}Deploy
          </button>
        </div>
      </div>
    </div>
  );
}
