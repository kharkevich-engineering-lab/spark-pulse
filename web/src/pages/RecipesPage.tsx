import { useState, useEffect, useMemo } from "react";
import { useI18n } from "@/lib/i18n";
import { fetchRecipes, fetchRecipe, fetchDeployments, createDeployment, scheduleDeploy, fetchSettings, fetchRecipeCustomization, saveRecipeCustomization, deleteRecipeCustomization, fetchMods, fetchMod, listCustomRecipes, saveCustomRecipe, deleteCustomRecipe, listCustomMods, getCustomModFiles, saveCustomModFiles, deleteCustomMod, ApiError } from "@/lib/api";
import type { RecipeDetail, RecipeCustomization, RecipeSummary, ModSummary, ModDetail, CustomRecipeInfo, CustomModInfo, ModFileMap, PreflightReport } from "@/lib/types";
import { useQuery } from "@/hooks/useQuery";
import { AlertModal, ConfirmModal } from "@/components/Modal";
import PreflightPanel from "@/components/PreflightPanel";
import { Loader2, AlertCircle, ChevronDown, X, Copy, Check, Wrench, Zap, FileCode2, FileText, FileCode, Plus, Download } from "lucide-react";
import RecipeCard from "@/components/RecipeCard";
import RecipeDrawer from "@/components/RecipeDrawer";
import SlideDrawer from "@/components/SlideDrawer";
import BaseCard from "@/components/BaseCard";
import CustomRecipeDrawer from "@/components/CustomRecipeDrawer";
import CustomModDrawer from "@/components/CustomModDrawer";
import NewRecipeModal from "@/components/NewRecipeModal";
import NewModModal from "@/components/NewModModal";
import type { DeployOptionsValue } from "@/components/DeployOptions";

/** What a blocked create is waiting on: the deploy the operator asked for,
 *  and the pre-flight report that stopped it — kept together so "Deploy
 *  anyway" can re-issue the exact same create with `skip_preflight`. */
interface BlockedDeploy {
  /** The recipe the create was for. Held here rather than read back off
   *  `selected`, because the drawer closes itself the moment `onDeploy`
   *  resolves — and a gated create resolves, it does not throw. Reading
   *  `selected` left "Deploy anyway" with nothing to deploy. */
  recipeId: string;
  name: string;
  params: Record<string, unknown>;
  options?: DeployOptionsValue;
  preflight: PreflightReport;
}

/** A deploy the model has to arrive for first: what the operator asked for,
 *  plus the model that is not here yet. Kept together so accepting the offer
 *  re-issues the very same deploy rather than an approximation of it. */
interface MissingModelDeploy {
  recipeId: string;
  name: string;
  params: Record<string, unknown>;
  options?: DeployOptionsValue;
  model: string;
}

/** The missing model from a 400's `detail.missing_model`, or null when the
 *  create failed for one of the many other reasons. */
function missingModel(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const detail = (payload as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return null;
  const missing = (detail as { missing_model?: unknown }).missing_model;
  if (!missing || typeof missing !== "object") return null;
  const model = (missing as { model?: unknown }).model;
  return typeof model === "string" && model ? model : null;
}

/** The pre-flight report from a 409's `detail.preflight`, or null when the
 *  failure was something else entirely (not every create failure is a gate). */
function blockingPreflight(payload: unknown): PreflightReport | null {
  if (!payload || typeof payload !== "object") return null;
  const detail = (payload as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return null;
  const preflight = (detail as { preflight?: unknown }).preflight;
  return preflight && typeof preflight === "object" ? (preflight as PreflightReport) : null;
}

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
  const { t } = useI18n();
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
              <p className="text-sm font-medium mb-2 text-text-muted uppercase tracking-wide text-xs">{t("recipes.assets")}</p>
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
  const { t, plural } = useI18n();
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
  const [blockedDeploy, setBlockedDeploy] = useState<BlockedDeploy | null>(null);
  const [missingModelDeploy, setMissingModelDeploy] = useState<MissingModelDeploy | null>(null);
  const [scheduling, setScheduling] = useState(false);
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


  const loadCustomData = async () => {
    setCustomLoading(true);
    try {
      const [r, m] = await Promise.all([listCustomRecipes(), listCustomMods()]);
      setCustomRecipes(r);
      setCustomMods(m);
    } catch (e) {
      setAlertModal({ title: t("common.error"), message: e instanceof Error ? e.message : "Failed to load custom data" });
    } finally {
      setCustomLoading(false);
    }
  };

  const handleToggleCustom = async () => {
    if (toggling) return;
    setToggling(true);
    const next = !showCustom;
    try {
      // Custom recipes and mods are read straight from ~/.config/spark-pulse,
      // so showing them is a load — there is nothing to sync into a checkout.
      if (next) await loadCustomData();
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
      setAlertModal({ title: t("common.error"), message: "Failed to load mod" });
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
      setAlertModal({ title: t("common.error"), message: e instanceof Error ? e.message : "Failed to load recipe" });
    }
  };

  const handleDeploy = async (name: string, params: Record<string, unknown>, options?: DeployOptionsValue) => {
    if (!selected) return;
    try {
      await createDeployment({
        recipe_id: selected.recipe.id,
        name,
        params,
        nodes: options?.nodes?.length ? options.nodes : undefined,
        engine: options?.engine,
        model: options?.model,
        extra_args: options?.extra_args?.length ? options.extra_args : undefined,
      });
      setSelected(null);
    } catch (e) {
      // A 409 from the pre-flight gate is not "an error" in the usual sense:
      // it is the same report the preview already showed, now with a stop
      // sign. Show the operator the checks and let them override rather than
      // reducing it to a one-line alert.
      const preflight = e instanceof ApiError && e.status === 409 ? blockingPreflight(e.payload) : null;
      if (preflight) {
        setBlockedDeploy({ recipeId: selected.recipe.id, name, params, options, preflight });
        return;
      }
      // "The model is not here" is the one deploy failure with an obvious
      // next step, and telling the operator to go and download it themselves
      // — on another page, matching the id by eye — is a worse answer than
      // offering to do it.
      const model = e instanceof ApiError && e.status === 400 ? missingModel(e.payload) : null;
      if (model) {
        setMissingModelDeploy({ recipeId: selected.recipe.id, name, params, options, model });
        return;
      }
      setAlertModal({ title: t("common.error"), message: e instanceof Error ? e.message : t("recipes.deployFailed") });
    }
  };

  const handleDownloadAndDeploy = async () => {
    if (!missingModelDeploy) return;
    const { recipeId, name, params, options, model } = missingModelDeploy;
    setScheduling(true);
    try {
      await scheduleDeploy({
        recipe_id: recipeId,
        name,
        params,
        nodes: options?.nodes?.length ? options.nodes : undefined,
        engine: options?.engine,
        model: options?.model || model,
        extra_args: options?.extra_args?.length ? options.extra_args : undefined,
      });
      setMissingModelDeploy(null);
      setSelected(null);
      setAlertModal({
        title: t("recipes.downloadStarted"),
        message: t("recipes.downloadStartedBody", { model, name }),
      });
    } catch (e) {
      setMissingModelDeploy(null);
      setAlertModal({ title: t("common.error"), message: e instanceof Error ? e.message : t("recipes.downloadFailed") });
    } finally {
      setScheduling(false);
    }
  };

  const handleDeployAnyway = async () => {
    if (!blockedDeploy) return;
    const { recipeId, name, params, options } = blockedDeploy;
    try {
      await createDeployment({
        recipe_id: recipeId,
        name,
        params,
        nodes: options?.nodes?.length ? options.nodes : undefined,
        engine: options?.engine,
        model: options?.model,
        extra_args: options?.extra_args?.length ? options.extra_args : undefined,
        skip_preflight: true,
      });
      setBlockedDeploy(null);
      setSelected(null);
    } catch (e) {
      setBlockedDeploy(null);
      setAlertModal({ title: t("common.error"), message: e instanceof Error ? e.message : t("recipes.deployFailed") });
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
      setAlertModal({ title: t("common.error"), message: e instanceof Error ? e.message : "Failed to save customization" });
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
      setAlertModal({ title: t("common.error"), message: e instanceof Error ? e.message : "Failed to reset customization" });
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
        <h2 className="text-2xl font-bold">{t("recipes.title")}</h2>
        <p className="text-text-muted mt-1">
          {showCustom ? t("recipes.subtitleCustom") : t("recipes.subtitleBundled")}
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
            {t("recipes.tabRecipes")} ({showCustom ? customRecipes.length : (recipes?.length ?? 0)})
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
            {t("recipes.tabMods")} ({showCustom ? customMods.length : (mods?.length ?? 0)})
          </button>
        </div>
        {/* Toggle + label */}
        <div className="flex items-center gap-2">
          <button
            onClick={handleToggleCustom}
            disabled={customLoading || toggling}
            className={`relative w-11 h-6 rounded-full transition-colors ${showCustom ? "bg-primary" : "bg-border"}`}
            aria-label={t("recipes.toggleCustom")}
          >
            <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${showCustom ? "translate-x-5" : ""}`} />
          </button>
          <span className={`text-sm font-medium ${showCustom ? "text-primary" : "text-text-muted"}`}>
            {t("recipes.customMode")}
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
              {/* The create button sits outside the "we already have some"
                  branch. It used to be inside it, so the state everybody
                  starts in — no custom recipes — offered no way to make the
                  first one: an empty page saying "create a new recipe to get
                  started" beside no button that would. */}
              <div className="flex items-center justify-between">
                <p className="text-sm text-text-muted">
                  {customRecipes.length === 0
                    ? t("recipes.noCustomRecipesYet")
                    : plural("recipes.customRecipeCount", customRecipes.length)}
                </p>
                <button
                  onClick={() => setShowNewRecipe(true)}
                  className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-white text-sm font-medium flex items-center gap-1.5 transition-colors"
                >
                  <Plus size={14} />
                  {t("recipes.newRecipe")}
                </button>
              </div>
              {customRecipes.length > 0 ? (
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
              ) : (
                <div className="text-center py-20 text-text-muted">
                  <FileText size={48} className="mx-auto mb-4 opacity-30" />
                  <p className="text-lg font-medium">{t("recipes.noCustomRecipes")}</p>
                  <p className="text-sm mt-1">
                    {t("recipes.noCustomRecipesHint")}
                  </p>
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
                  <p>{t("recipes.noRecipes")}</p>
                  <p className="text-sm mt-1">{t("recipes.noRecipesHint")}</p>
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
              {/* Same as the recipes tab: the button that makes the first one
                  cannot live inside the branch that needs one to exist. */}
              <div className="flex items-center justify-between">
                <p className="text-sm text-text-muted">
                  {customMods.length === 0
                    ? t("recipes.noCustomModsYet")
                    : plural("recipes.customModCount", customMods.length)}
                </p>
                <button
                  onClick={() => setShowNewMod(true)}
                  className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-white text-sm font-medium flex items-center gap-1.5 transition-colors"
                >
                  <Plus size={14} />
                  {t("recipes.newMod")}
                </button>
              </div>
              {customMods.length === 0 && (
                <div className="py-20 text-center text-text-muted">
                  <Wrench size={48} className="mx-auto mb-4 opacity-30" />
                  <p className="text-lg font-medium">{t("recipes.noCustomMods")}</p>
                  <p className="text-sm mt-1 opacity-70">
                    A mod is a <code className="font-mono">run.sh</code> that runs inside the
                    container before the engine starts.
                  </p>
                </div>
              )}

              {customMods.length > 0 && (
                <>
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
                  <p className="text-lg font-medium">{t("recipes.noMods")}</p>
                  <p className="text-sm mt-1 opacity-70">
                    {t("recipes.noModsHint")}
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
          onError={(msg) => setAlertModal({ title: t("common.error"), message: msg })}
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
          onError={(msg) => setAlertModal({ title: t("common.error"), message: msg })}
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
          onError={(msg) => setAlertModal({ title: t("common.error"), message: msg })}
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
          onError={(msg) => setAlertModal({ title: t("common.error"), message: msg })}
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
          onError={(msg) => setAlertModal({ title: t("common.error"), message: msg })}
        />
      )}

      {alertModal && (
        <AlertModal open={!!alertModal} onClose={() => setAlertModal(null)} title={alertModal.title} message={alertModal.message} />
      )}

      {blockedDeploy && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center p-4" role="dialog" aria-modal="true">
          <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => setBlockedDeploy(null)} />
          <div
            className="relative w-full max-w-lg rounded-xl bg-surface border border-border shadow-2xl p-5 space-y-4"
            data-testid="preflight-block-modal"
          >
            <div className="flex items-start justify-between gap-3">
              <h3 className="text-lg font-bold">{t("recipes.preflightBlocked")}</h3>
              <button
                type="button"
                onClick={() => setBlockedDeploy(null)}
                className="p-1 rounded-lg hover:bg-surface-hover transition-colors"
                title={t("common.close")}
              >
                <X size={18} />
              </button>
            </div>
            <PreflightPanel report={blockedDeploy.preflight} />
            <div className="flex items-center justify-end gap-3">
              <button
                type="button"
                onClick={() => setBlockedDeploy(null)}
                className="px-4 py-2 rounded-lg border border-border hover:border-border-hover transition-colors"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleDeployAnyway}
                className="px-4 py-2 rounded-lg bg-danger hover:bg-danger/80 text-white font-medium transition-colors"
              >
                {t("recipes.deployAnyway")}
              </button>
            </div>
          </div>
        </div>
      )}

      {missingModelDeploy && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center p-4" role="dialog" aria-modal="true">
          <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={() => setMissingModelDeploy(null)} />
          <div
            className="relative w-full max-w-lg rounded-xl bg-surface border border-border shadow-2xl p-5 space-y-4"
            data-testid="missing-model-modal"
          >
            <div className="flex items-start justify-between gap-3">
              <h3 className="text-lg font-bold">{t("recipes.missingModelTitle")}</h3>
              <button
                type="button"
                onClick={() => setMissingModelDeploy(null)}
                className="p-1 rounded-lg hover:bg-surface-hover transition-colors"
                title={t("common.close")}
              >
                <X size={18} />
              </button>
            </div>
            <div className="space-y-3 text-sm">
              <p className="text-text-secondary">
                <span className="font-mono text-text break-all">{missingModelDeploy.model}</span> is not in the
                local catalogue, so <span className="font-medium text-text">{missingModelDeploy.name}</span> cannot start.
              </p>
              <p className="text-text-secondary">
                Downloading it can take a while. You do not have to wait here — the deployment is recorded and
                starts on its own when the model lands, and you can cancel it from the Models page at any point.
              </p>
            </div>
            <div className="flex items-center justify-end gap-3">
              <button
                type="button"
                onClick={() => setMissingModelDeploy(null)}
                className="px-4 py-2 rounded-lg border border-border hover:border-border-hover transition-colors"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleDownloadAndDeploy}
                disabled={scheduling}
                className="px-4 py-2 rounded-lg bg-primary hover:bg-primary/80 text-white font-medium transition-colors disabled:opacity-50 inline-flex items-center gap-2"
              >
                {scheduling ? <Loader2 size={16} className="animate-spin" /> : <Download size={16} />}
                {t("recipes.downloadAndDeploy")}
              </button>
            </div>
          </div>
        </div>
      )}

      {resetConfirm && (
        <ConfirmModal
          open={!!resetConfirm}
          onClose={() => setResetConfirm(null)}
          onConfirm={() => resetConfirm && handleReset(resetConfirm.recipeId)}
          title={t("recipes.resetTitle")}
          message={t("recipes.resetBody", { name: resetConfirm.recipeName })}
          confirmLabel={t("recipes.reset")}
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
