import { useState, useEffect, useMemo } from "react";
import { fetchRecipes, fetchRecipe, fetchDeployments, createDeployment, fetchSettings, fetchRecipeCustomization, saveRecipeCustomization, deleteRecipeCustomization } from "@/lib/api";
import type { RecipeDetail, RecipeCustomization, RecipeSummary } from "@/lib/types";
import { useQuery } from "@/hooks/useQuery";
import { AlertModal, ConfirmModal } from "@/components/Modal";
import { Loader2, AlertCircle, ChevronDown } from "lucide-react";
import RecipeCard from "@/components/RecipeCard";
import RecipeModal from "@/components/RecipeModal";
import { setRefresh } from "@/lib/refresh";

export default function RecipesPage() {
  const { data: recipes, loading, error, refetch } = useQuery(fetchRecipes);
  const { data: deployments } = useQuery(fetchDeployments);
  const { data: settings } = useQuery(fetchSettings);
  const [selected, setSelected] = useState<{ recipe: RecipeDetail; customization: RecipeCustomization } | null>(null);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);
  const [showUnavailable, setShowUnavailable] = useState(false);

  // Confirmation modal state for reset
  const [resetConfirm, setResetConfirm] = useState<{ recipeId: string; recipeName: string } | null>(null);

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

  useEffect(() => { setRefresh(refetch); }, [refetch]);

  const handleSelect = async (recipe: RecipeSummary) => {
    try {
      const [detail, customization] = await Promise.all([
        fetchRecipe(recipe.id),
        fetchRecipeCustomization(recipe.id),
      ]);
      setSelected({ recipe: detail, customization });
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to load recipe" });
    }
  };

  const handleDeploy = async (name: string, params: Record<string, unknown>) => {
    if (!selected) return;
    try {
      await createDeployment({ recipe_id: selected.recipe.id, name, params });
      setSelected(null);
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to deploy" });
    }
  };

  const handleSaveCustomization = async (fields: Partial<RecipeCustomization>) => {
    if (!selected) return;
    try {
      await saveRecipeCustomization(selected.recipe.id, fields);
      // Refresh recipe list and selection
      await refetch();
      const [detail, customization] = await Promise.all([
        fetchRecipe(selected.recipe.id),
        fetchRecipeCustomization(selected.recipe.id),
      ]);
      setSelected({ recipe: detail, customization });
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to save customization" });
    }
  };

  const handleReset = async (recipeId: string) => {
    try {
      await deleteRecipeCustomization(recipeId);
      await refetch();
      if (selected?.recipe.id === recipeId) {
        // Reopen with just the original recipe
        const detail = await fetchRecipe(recipeId);
        setSelected({ recipe: detail, customization: {} });
      }
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to reset customization" });
    } finally {
      setResetConfirm(null);
    }
  };

  const openResetConfirm = (recipe: RecipeSummary) => {
    setResetConfirm({ recipeId: recipe.id, recipeName: recipe.name });
  };

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
            {available.map((r) => (
              <RecipeCard
                key={r.id}
                r={r}
                isRunning={runningIds.has(r.id)}
                clusterBlocked={false}
                duplicateNames={duplicateNames}
                onSelect={() => handleSelect(r)}
                onReset={() => openResetConfirm(r)}
              />
            ))}
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
                  {unavailable.map((r) => (
                    <RecipeCard
                      key={r.id}
                      r={r}
                      isRunning={false}
                      clusterBlocked={true}
                      duplicateNames={duplicateNames}
                      onSelect={() => {}}
                      onReset={() => openResetConfirm(r)}
                    />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {recipes && recipes.length === 0 && !loading && !error && (
        <div className="text-center py-20 text-text-muted"><p>No recipes found.</p><p className="text-sm mt-1">Check spark-vllm-docker path in Settings.</p></div>
      )}

      {selected && (
        <RecipeModal
          recipe={selected.recipe}
          customization={selected.customization}
          isRunning={runningIds.has(selected.recipe.id)}
          clusterEnabled={clusterEnabled}
          onClose={() => setSelected(null)}
          onError={(msg) => setAlertModal({ title: "Error", message: msg })}
          onDeploy={handleDeploy}
          onSaveCustomization={handleSaveCustomization}
          onReset={() => selected && openResetConfirm(selected.recipe)}
        />
      )}

      {alertModal && (
        <AlertModal open={!!alertModal} onClose={() => setAlertModal(null)} title={alertModal.title} message={alertModal.message} />
      )}

      {resetConfirm && (
        <ConfirmModal
          open={!!resetConfirm}
          onClose={() => setResetConfirm(null)}
          onConfirm={() => resetConfirm && handleReset(resetConfirm.recipeId)}
          title="Reset Customization"
          message={`Reset "${resetConfirm.recipeName}" to its original recipe? Any customizations you made will be lost.`}
          confirmLabel="Reset"
          confirmVariant="danger"
        />
      )}
    </div>
  );
}
