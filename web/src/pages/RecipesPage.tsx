import { useState, useEffect, useMemo } from "react";
import { fetchRecipes, fetchRecipe, fetchDeployments, createDeployment, fetchSettings, fetchRecipeCustomization, saveRecipeCustomization, deleteRecipeCustomization, fetchMods, fetchMod } from "@/lib/api";
import type { RecipeDetail, RecipeCustomization, RecipeSummary, ModSummary, ModDetail } from "@/lib/types";
import { useQuery } from "@/hooks/useQuery";
import { AlertModal, ConfirmModal } from "@/components/Modal";
import { Loader2, AlertCircle, ChevronDown, ChevronRight, X, Copy, Check, Wrench, Zap, FileCode2, FileText, FileCode } from "lucide-react";
import RecipeCard from "@/components/RecipeCard";
import RecipeDrawer from "@/components/RecipeDrawer";
import { setRefresh } from "@/lib/refresh";
import { useRef } from "react";

type Tab = "recipes" | "mods";

// ── File-kind badge colours ──────────────────────────────────────────────────

const KIND_STYLE: Record<string, string> = {
  patch: "bg-warning/15 text-warning border-warning/30",
  template: "bg-primary/15 text-primary border-primary/30",
  python: "bg-success/15 text-success border-success/30",
  script: "bg-tag-bg text-text-muted border-border",
  yaml: "bg-tag-bg text-text-muted border-border",
  file: "bg-tag-bg text-text-muted border-border",
};

const KIND_ICON: Record<string, React.ReactNode> = {
  patch: <FileCode2 size={12} />,
  template: <FileText size={12} />,
  python: <FileCode size={12} />,
  script: <FileCode size={12} />,
};

function FileBadge({ name, kind }: { name: string; kind: string }) {
  const cls = KIND_STYLE[kind] ?? KIND_STYLE.file;
  const icon = KIND_ICON[kind];
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-xs font-mono ${cls}`}>
      {icon}
      {name}
    </span>
  );
}

// ── Mod detail drawer ────────────────────────────────────────────────────────

function ModDrawer({ modId, onClose }: { modId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<ModDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const drawerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    drawerRef.current?.focus();
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchMod(modId)
      .then(setDetail)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [modId]);

  const copyScript = async () => {
    if (!detail?.script) return;
    await navigator.clipboard.writeText(detail.script);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end" onClick={onClose}>
      <div
        ref={drawerRef}
        tabIndex={-1}
        className="h-full w-full max-w-2xl bg-surface border-l border-border shadow-xl flex flex-col overflow-hidden outline-none"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-border shrink-0">
          <div className="flex items-center gap-2">
            <Wrench size={20} className="text-primary" />
            <span className="font-mono font-semibold text-lg">{modId}</span>
          </div>
          <button onClick={onClose} className="p-1.5 rounded hover:bg-surface-hover transition-colors">
            <X size={18} />
          </button>
        </div>

        {loading && (
          <div className="flex-1 flex items-center justify-center">
            <Loader2 className="animate-spin text-primary" size={32} />
          </div>
        )}
        {error && (
          <div className="flex-1 flex items-center justify-center px-6">
            <div className="flex items-center gap-3 text-danger">
              <AlertCircle size={20} />
              <span>{error}</span>
            </div>
          </div>
        )}

        {detail && !loading && (
          <div className="flex-1 overflow-y-auto p-6 space-y-5">
            {detail.description && (
              <div className="p-4 rounded-xl bg-bg border border-border">
                <p className="text-sm text-text-muted leading-relaxed">{detail.description}</p>
              </div>
            )}

            {detail.files.length > 0 && (
              <div>
                <p className="text-sm font-medium mb-2 text-text-muted uppercase tracking-wide text-xs">Assets</p>
                <div className="flex flex-wrap gap-2">
                  {detail.files.map((f) => (
                    <FileBadge key={f.name} name={f.name} kind={f.kind} />
                  ))}
                </div>
              </div>
            )}

            <div>
              <div className="flex items-center justify-between mb-2">
                <p className="text-sm font-medium">run.sh</p>
                <button
                  onClick={copyScript}
                  className="flex items-center gap-1.5 text-xs text-text-muted hover:text-text transition-colors"
                >
                  {copied ? <Check size={14} className="text-success" /> : <Copy size={14} />}
                  {copied ? "Copied" : "Copy"}
                </button>
              </div>
              <div className="rounded-xl bg-bg border border-border overflow-hidden">
                <pre className="p-4 text-xs font-mono overflow-x-auto leading-relaxed whitespace-pre">
                  {detail.script || "(empty)"}
                </pre>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function RecipesPage() {
  const { data: recipes, loading: recipesLoading, error: recipesError, refetch } = useQuery(fetchRecipes);
  const { data: deployments } = useQuery(fetchDeployments);
  const { data: settings } = useQuery(fetchSettings);
  const { data: mods, loading: modsLoading, error: modsError } = useQuery(fetchMods);
  const [tab, setTab] = useState<Tab>("recipes");
  const [selected, setSelected] = useState<{ recipe: RecipeDetail; customization: RecipeCustomization } | null>(null);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);
  const [showUnavailable, setShowUnavailable] = useState(false);
  const [activeModId, setActiveModId] = useState<string | null>(null);

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

  const isAnyLoading = recipesLoading || modsLoading;
  const isError = recipesError || modsError;
  const combinedError = [recipesError, modsError].filter(Boolean).join("; ");

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">Recipes & Mods</h2>
        <p className="text-text-muted mt-1">Browse deployment recipes and available modifications</p>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-2 border-b border-border">
        <button
          onClick={() => setTab("recipes")}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            tab === "recipes"
              ? "border-primary text-primary"
              : "border-transparent text-text-muted hover:text-text"
          }`}
        >
          <Zap size={14} className="inline mr-1.5" />
          Recipes ({recipes?.length ?? 0})
        </button>
        <button
          onClick={() => setTab("mods")}
          className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
            tab === "mods"
              ? "border-primary text-primary"
              : "border-transparent text-text-muted hover:text-text"
          }`}
        >
          <Wrench size={14} className="inline mr-1.5" />
          Mods ({mods?.length ?? 0})
        </button>
      </div>

      {/* Loading / error overlay */}
      {isAnyLoading && (
        <div className="flex justify-center py-20">
          <Loader2 className="animate-spin text-primary" size={32} />
        </div>
      )}
      {isError && !isAnyLoading && (
        <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3">
          <AlertCircle size={20} />
          <span>{combinedError}</span>
        </div>
      )}

      {/* ── Recipes tab ─────────────────────────────────────────────────── */}
      {tab === "recipes" && !isAnyLoading && (
        <div className="space-y-6">
          {recipes && recipes.length > 0 && (
            <>
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
            </>
          )}

          {recipes && recipes.length === 0 && (
            <div className="text-center py-20 text-text-muted">
              <p>No recipes found.</p>
              <p className="text-sm mt-1">Check spark-vllm-docker path in Settings.</p>
            </div>
          )}
        </div>
      )}

      {/* ── Mods tab ────────────────────────────────────────────────────── */}
      {tab === "mods" && !isAnyLoading && (
        <div className="space-y-4">
          {mods && mods.length === 0 && (
            <div className="py-20 text-center text-text-muted">
              <Wrench size={48} className="mx-auto mb-4 opacity-30" />
              <p className="text-lg font-medium">No mods found</p>
              <p className="text-sm mt-1 opacity-70">
                Make sure spark_vllm_path is configured correctly in Settings.
              </p>
            </div>
          )}

          {mods && mods.length > 0 && (
            <div className="grid gap-3 sm:grid-cols-1 lg:grid-cols-2">
              {mods.map((mod) => (
                <ModCard key={mod.id} mod={mod} onClick={() => setActiveModId(mod.id)} />
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── Modals / Drawers ────────────────────────────────────────────── */}
      {selected && (
        <RecipeDrawer
          recipe={selected.recipe}
          customization={selected.customization}
          isRunning={runningIds.has(selected.recipe.id)}
          clusterEnabled={clusterEnabled}
          onClose={() => setSelected(null)}
          onError={(msg) => setAlertModal({ title: "Error", message: msg })}
          onDeploy={handleDeploy}
          onSaveCustomization={handleSaveCustomization}
          onReset={() => {
            if (selected) {
              return handleReset(selected.recipe.id);
            }
          }}
        />
      )}

      {activeModId && (
        <ModDrawer modId={activeModId} onClose={() => setActiveModId(null)} />
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

// ── Mod card ─────────────────────────────────────────────────────────────────

function ModCard({ mod, onClick }: { mod: ModSummary; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="w-full text-left p-5 rounded-xl bg-surface border border-border hover:border-primary/50 hover:bg-surface-hover transition-all group"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <Wrench size={16} className="text-primary shrink-0" />
            <span className="font-mono font-semibold text-sm">{mod.id}</span>
            {mod.has_patches && (
              <span className="px-1.5 py-0.5 rounded text-xs bg-warning/15 text-warning border border-warning/30 font-mono">
                patches
              </span>
            )}
          </div>
          {mod.description ? (
            <p className="text-sm text-text-muted leading-snug">{mod.description}</p>
          ) : (
            <p className="text-sm text-text-muted italic opacity-50">No description</p>
          )}
        </div>
        <ChevronRight size={16} className="text-text-muted group-hover:text-primary shrink-0 mt-0.5 transition-colors" />
      </div>

      {mod.files.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mt-3">
          {mod.files.map((f) => (
            <FileBadge key={f.name} name={f.name} kind={f.kind} />
          ))}
        </div>
      )}
    </button>
  );
}
