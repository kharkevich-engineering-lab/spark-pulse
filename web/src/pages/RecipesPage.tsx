import { useState, useEffect, useMemo } from "react";
import { fetchRecipes, fetchRecipe, fetchDeployments, createDeployment, fetchSettings, fetchRecipeCustomization, saveRecipeCustomization, deleteRecipeCustomization, fetchMods, fetchMod, listCustomRecipes, saveCustomRecipe, deleteCustomRecipe, listCustomMods, getCustomModFiles, saveCustomModFiles, deleteCustomMod, syncSymlinks } from "@/lib/api";
import type { RecipeDetail, RecipeCustomization, RecipeSummary, ModSummary, ModDetail, CustomRecipeInfo, CustomModInfo, ModFileMap } from "@/lib/types";
import { useQuery } from "@/hooks/useQuery";
import { AlertModal, ConfirmModal } from "@/components/Modal";
import { Loader2, AlertCircle, ChevronDown, X, Copy, Check, Wrench, Zap, FileCode2, FileText, FileCode, Plus } from "lucide-react";
import RecipeCard from "@/components/RecipeCard";
import RecipeDrawer from "@/components/RecipeDrawer";
import SlideDrawer from "@/components/SlideDrawer";
import BaseCard from "@/components/BaseCard";
import CustomRecipeDrawer from "@/components/CustomRecipeDrawer";
import CustomModDrawer from "@/components/CustomModDrawer";
import NewRecipeModal from "@/components/NewRecipeModal";
import NewModModal from "@/components/NewModModal";
import type { DeployOptionsValue } from "@/components/DeployOptions";
import { setRefresh } from "@/lib/refresh";

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
    <SlideDrawer
      open={!!modId}
      onClose={onClose}
      header={
        <div>
          <div className="flex items-center gap-2">
            <Wrench size={18} className="text-primary" />
            <span className="text-xl font-mono font-bold truncate">{modId}</span>
          </div>
        </div>
      }
      actions={
        <button type="button" onClick={onClose} className="p-1.5 rounded-lg hover:bg-surface-hover transition-colors">
          <X size={18} />
        </button>
      }
    >
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
        <div className="p-6 space-y-5">
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
    </SlideDrawer>
  );
}

export default function RecipesPage() {
  const { data: recipes, loading: recipesLoading, error: recipesError, refetch } = useQuery(fetchRecipes);
  const { data: deployments } = useQuery(fetchDeployments);
  const { data: settings } = useQuery(fetchSettings);
  const { data: mods, loading: modsLoading, error: modsError } = useQuery(fetchMods);
  const [showCustom, setShowCustom] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [tab, setTab] = useState<"recipes" | "mods">("recipes");
  const [customRecipes, setCustomRecipes] = useState<CustomRecipeInfo[]>([]);
  const [customMods, setCustomMods] = useState<CustomModInfo[]>([]);
  const [customLoading, setCustomLoading] = useState(false);
  const [selected, setSelected] = useState<{ recipe: RecipeDetail; customization: RecipeCustomization } | null>(null);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);
  const [showUnavailable, setShowUnavailable] = useState(false);
  const [activeModId, setActiveModId] = useState<string | null>(null);

  // Custom recipe modal state
  const [selectedRecipe, setSelectedRecipe] = useState<CustomRecipeInfo | null>(null);
  const [showRecipeModal, setShowRecipeModal] = useState(false);
  // Custom mod modal state
  const [selectedMod, setSelectedMod] = useState<CustomModInfo | null>(null);
  const [showModModal, setShowModModal] = useState(false);
  const [modFiles, setModFiles] = useState<ModFileMap>({});

  // Confirmation modal state for reset
  const [resetConfirm, setResetConfirm] = useState<{ recipeId: string; recipeName: string } | null>(null);

  // New recipe/mod modal state
  const [showNewRecipe, setShowNewRecipe] = useState(false);
  const [showNewMod, setShowNewMod] = useState(false);

  const clusterEnabled = settings?.cluster_enabled ?? false;

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

  const loadCustomData = async () => {
    setCustomLoading(true);
    try {
      const [r, m] = await Promise.all([listCustomRecipes(), listCustomMods()]);
      setCustomRecipes(r);
      setCustomMods(m);
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to load custom data" });
    } finally {
      setCustomLoading(false);
    }
  };

  const handleToggleCustom = async () => {
    if (toggling) return;
    setToggling(true);
    const next = !showCustom;
    try {
      if (next) {
        // Switching to custom: create symlinks
        try { await syncSymlinks("create"); } catch { /* best-effort */ }
        await loadCustomData();
      } else {
        // Switching to system: remove symlinks
        try { await syncSymlinks("remove"); } catch { /* best-effort */ }
      }
      setShowCustom(next);
    } finally {
      setToggling(false);
    }
  };

  const handleOpenCustomRecipe = async (recipe: CustomRecipeInfo) => {
    setSelectedRecipe(recipe);
    setShowRecipeModal(true);
  };

  const handleSaveCustomRecipe = async (id: string, content: string) => {
    await saveCustomRecipe(id, content);
    await loadCustomData();
  };

  const handleDeleteCustomRecipe = async (_id: string) => {
    await deleteCustomRecipe(_id);
    await loadCustomData();
  };

  const handleOpenCustomMod = async (mod: CustomModInfo) => {
    setSelectedMod(mod);
    try {
      const { files } = await getCustomModFiles(mod.id);
      setModFiles(files);
      setShowModModal(true);
    } catch {
      setAlertModal({ title: "Error", message: "Failed to load mod" });
    }
  };

  const handleSaveCustomMod = async (_id: string, fileMap: ModFileMap) => {
    await saveCustomModFiles(_id, fileMap);
    await loadCustomData();
  };

  const handleDeleteCustomMod = async (_id: string) => {
    await deleteCustomMod(_id);
    await loadCustomData();
  };

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

  const handleDeploy = async (name: string, params: Record<string, unknown>, options?: DeployOptionsValue) => {
    if (!selected) return;
    try {
      await createDeployment({
        recipe_id: selected.recipe.id,
        name,
        params,
        engine: options?.engine,
        model: options?.model,
        extra_args: options?.extra_args?.length ? options.extra_args : undefined,
      });
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

  const isAnyLoading = recipesLoading || modsLoading || customLoading;
  const isError = recipesError || modsError;
  const combinedError = [recipesError, modsError].filter(Boolean).join("; ");

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">Recipes & Mods</h2>
        <p className="text-text-muted mt-1">
          {showCustom ? "Browse your custom recipes and mods" : "Browse deployment recipes and available modifications"}
        </p>
      </div>

      {/* Tabs + Toggle */}
      <div className="flex items-center justify-between border-b border-border">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setTab("recipes")}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              tab === "recipes"
                ? "border-primary text-primary"
                : "border-transparent text-text-muted hover:text-text"
            }`}
          >
            <Zap size={14} className="inline mr-1.5" />
            Recipes ({showCustom ? customRecipes.length : (recipes?.length ?? 0)})
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
            Mods ({showCustom ? customMods.length : (mods?.length ?? 0)})
          </button>
        </div>
        {/* Toggle + label */}
        <div className="flex items-center gap-2">
          <button
            onClick={handleToggleCustom}
            disabled={customLoading || toggling}
            className={`relative w-11 h-6 rounded-full transition-colors ${showCustom ? "bg-primary" : "bg-border"}`}
            aria-label="Toggle custom mode"
          >
            <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${showCustom ? "translate-x-5" : ""}`} />
          </button>
          <span className={`text-sm font-medium ${showCustom ? "text-primary" : "text-text-muted"}`}>
            Custom mode
          </span>
        </div>
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
          {showCustom ? (
            <>
              {customRecipes.length > 0 && (
                <>
                  <div className="flex items-center justify-between">
                    <p className="text-sm text-text-muted">
                      {customRecipes.length} custom recipe{customRecipes.length > 1 ? "s" : ""}
                    </p>
                    <button
                      onClick={() => setShowNewRecipe(true)}
                      className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-white text-sm font-medium flex items-center gap-1.5 transition-colors"
                    >
                      <Plus size={14} />
                      New Recipe
                    </button>
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                    {customRecipes.map((r) => (
                      <BaseCard
                        key={r.id}
                        icon={<FileText size={16} className="shrink-0 text-primary" />}
                        title={r.name}
                        subtitle={r.filename}
                        onClick={() => handleOpenCustomRecipe(r)}
                      />
                    ))}
                  </div>
                </>
              )}
              {customRecipes.length === 0 && (
                <div className="text-center py-20 text-text-muted">
                  <FileText size={48} className="mx-auto mb-4 opacity-30" />
                  <p className="text-lg font-medium">No custom recipes</p>
                  <p className="text-sm mt-1">Upload a YAML file or create a new recipe.</p>
                </div>
              )}
            </>
          ) : (
            <>
              {recipes && recipes.length > 0 && (
                <>
                  <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                    {available.map((r) => (
                      <RecipeCard
                        key={r.id}
                        r={r}
                        isRunning={runningIds.has(r.id)}
                        clusterBlocked={false}
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
            </>
          )}
        </div>
      )}

      {/* ── Mods tab ────────────────────────────────────────────────────── */}
      {tab === "mods" && !isAnyLoading && (
        <div className="space-y-4">
          {showCustom ? (
            <>
              {customMods.length === 0 && (
                <div className="py-20 text-center text-text-muted">
                  <Wrench size={48} className="mx-auto mb-4 opacity-30" />
                  <p className="text-lg font-medium">No custom mods</p>
                  <p className="text-sm mt-1 opacity-70">Create a new mod to get started.</p>
                </div>
              )}

              {customMods.length > 0 && (
                <>
                  <div className="flex items-center justify-between">
                    <p className="text-sm text-text-muted">
                      {customMods.length} custom mod{customMods.length > 1 ? "s" : ""}
                    </p>
                    <button
                      onClick={() => setShowNewMod(true)}
                      className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-white text-sm font-medium flex items-center gap-1.5 transition-colors"
                    >
                      <Plus size={14} />
                      New Mod
                    </button>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-1 lg:grid-cols-2">
                    {customMods.map((m) => (
                      <BaseCard
                        key={m.id}
                        icon={<Wrench size={16} className="shrink-0 text-primary" />}
                        title={m.name}
                        description={m.description}
                        badges={m.has_run_sh ? (
                          <span className="flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-success/15 text-success">
                            <span className="w-1.5 h-1.5 rounded-full bg-success" />run.sh
                          </span>
                        ) : undefined}
                        onClick={() => handleOpenCustomMod(m)}
                      />
                    ))}
                  </div>
                </>
              )}
            </>
          ) : (
            <>
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
            </>
          )}
        </div>
      )}

      {/* ── Drawers ─────────────────────────────────────────────────────── */}
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

      {/* Custom recipe modal */}
      {showRecipeModal && selectedRecipe && (
        <CustomRecipeDrawer
          open={showRecipeModal}
          recipe={selectedRecipe}
          onClose={() => { setShowRecipeModal(false); setSelectedRecipe(null); }}
          onSave={handleSaveCustomRecipe}
          onDelete={handleDeleteCustomRecipe}
          onError={(msg) => setAlertModal({ title: "Error", message: msg })}
        />
      )}

      {/* Custom mod modal */}
      {showModModal && selectedMod && (
        <CustomModDrawer
          open={showModModal}
          mod={selectedMod}
          files={modFiles}
          onClose={() => { setShowModModal(false); setSelectedMod(null); }}
          onSave={handleSaveCustomMod}
          onDelete={handleDeleteCustomMod}
          onError={(msg) => setAlertModal({ title: "Error", message: msg })}
        />
      )}

      {/* New recipe modal */}
      {showNewRecipe && (
        <NewRecipeModal
          open={showNewRecipe}
          onClose={() => setShowNewRecipe(false)}
          onSave={async (_id, _name, _content) => {
            await loadCustomData();
            setShowNewRecipe(false);
          }}
          onError={(msg) => setAlertModal({ title: "Error", message: msg })}
        />
      )}

      {/* New mod modal */}
      {showNewMod && (
        <NewModModal
          open={showNewMod}
          onClose={() => setShowNewMod(false)}
          onSave={async (_id, _name) => {
            await loadCustomData();
            setShowNewMod(false);
          }}
          onError={(msg) => setAlertModal({ title: "Error", message: msg })}
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

// ── Mod card ─────────────────────────────────────────────────────────────────

function ModCard({ mod, onClick }: { mod: ModSummary; onClick: () => void }) {
  const badges = (
    <>
      {mod.has_patches && (
        <span className="px-1.5 py-0.5 rounded text-xs bg-warning/15 text-warning border border-warning/30 font-mono">
          patches
        </span>
      )}
      {mod.files.map((f) => (
        <FileBadge key={f.name} name={f.name} kind={f.kind} />
      ))}
    </>
  );

  return (
    <BaseCard
      icon={<Wrench size={16} className="shrink-0 text-primary" />}
      title={mod.id}
      description={mod.description}
      badges={badges}
      onClick={onClick}
    />
  );
}
