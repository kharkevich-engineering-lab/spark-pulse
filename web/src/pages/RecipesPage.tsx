import { useState, useEffect, useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useI18n } from "@/lib/i18n";
import { fetchRecipes, fetchRecipe, createDeployment, scheduleDeploy, fetchSettings, fetchRecipeCustomization, saveRecipeCustomization, deleteRecipeCustomization, listCustomRecipes, saveCustomRecipe, deleteCustomRecipe, uninstallOciRecipe, ApiError } from "@/lib/api";
import { tightHere, wontRunHere } from "@/lib/types";
import type { Deployment, RecipeDetail, RecipeCustomization, RecipeSummary, CustomRecipeInfo, PreflightReport } from "@/lib/types";
import { useQuery } from "@/hooks/useQuery";
import { useDeployments } from "@/hooks/useDeployments";
import { AlertModal, Button, ConfirmModal, EmptyRow, ErrorLine, IconButton, Modal, PageHeader, SearchInput, Section, Spinner, Table, Tabs, Td, Th, THead, Toolbar, Tr, matchesQuery } from "@/ui";
import type { AlertTone } from "@/ui";
import PreflightPanel from "@/components/PreflightPanel";
import { ChevronDown, Download, Pencil, Plus, Rocket, Trash2 } from "lucide-react";
import RecipeCard from "@/components/RecipeCard";
import RecipesTable from "@/components/RecipesTable";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { useWaitingDeploys } from "@/hooks/useWaitingDeploys";
import RecipeDrawer from "@/components/RecipeDrawer";
import CustomRecipeDrawer from "@/components/CustomRecipeDrawer";
import NewRecipeModal from "@/components/NewRecipeModal";
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

/** ``oci-`` is the id prefix ``recipe_sources.py`` mints for a recipe an OCI
 *  collection installed — the stem after it is the filename ``uninstall_oci_
 *  recipe`` (and the OCI Registry page) expect, with or without ``.yaml``. */
const OCI_ID_PREFIX = "oci-";

function ociRecipeName(recipeId: string): string {
  return recipeId.startsWith(OCI_ID_PREFIX) ? recipeId.slice(OCI_ID_PREFIX.length) : recipeId;
}

/** Where a recipe came from, as the source filter names it. */
type RecipeSource = "all" | "bundled" | "oci" | "custom";

function inSource(r: RecipeSummary, source: RecipeSource): boolean {
  if (source === "all") return true;
  if (source === "custom") return r.source === "custom" || r.id.startsWith("custom-");
  if (source === "oci") return r.source === "oci" || r.id.startsWith("oci-");
  return r.source === "bundled" || r.id.startsWith("bundled/");
}

export default function RecipesPage() {
  const { t, plural } = useI18n();
  const { data: recipes, loading: recipesLoading, error: recipesError, refetch } = useQuery(fetchRecipes);
  // The same read Runs makes, through the same hook: this page only needs to
  // know which recipes are already serving, and a second fetch of its own is
  // how the badge came to outlive the run it was marking.
  const { deployments } = useDeployments();
  const { data: settings } = useQuery(fetchSettings);
  const [source, setSource] = useState<RecipeSource>("all");
  const [query, setQuery] = useState("");
  const [customRecipes, setCustomRecipes] = useState<CustomRecipeInfo[]>([]);
  const [customLoaded, setCustomLoaded] = useState(false);
  const [customLoading, setCustomLoading] = useState(false);
  const [customError, setCustomError] = useState<string | null>(null);
  const [selected, setSelected] = useState<{ recipe: RecipeDetail; customization: RecipeCustomization } | null>(null);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string; tone?: AlertTone } | null>(null);
  const [blockedDeploy, setBlockedDeploy] = useState<BlockedDeploy | null>(null);
  const [missingModelDeploy, setMissingModelDeploy] = useState<MissingModelDeploy | null>(null);
  const [scheduling, setScheduling] = useState(false);
  const [showUnavailable, setShowUnavailable] = useState(false);

  // Custom recipe editor state
  const [selectedRecipe, setSelectedRecipe] = useState<CustomRecipeInfo | null>(null);
  const [showRecipeModal, setShowRecipeModal] = useState(false);

  // Confirmation modal state for reset
  const [resetConfirm, setResetConfirm] = useState<{ recipeId: string; recipeName: string } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<
    { kind: "recipe" | "oci"; id: string; name: string } | null
  >(null);

  const [showNewRecipe, setShowNewRecipe] = useState(false);

  // What decides whether a `cluster_only` recipe is offered is the cluster the
  // control plane actually has — `/api/settings` derives it from the registry
  // — not the `cluster_enabled` switch, which only forces the recipes on below
  // two nodes. An operator who had enrolled a second Spark used to be told
  // every multi-node recipe was unavailable until they found that toggle.
  const clusterAvailable = settings?.cluster?.available ?? settings?.cluster_enabled ?? false;
  const clusterNodeCount = settings?.cluster?.node_count ?? 1;

  // Deploys waiting on their model download: the card says "queued" from the
  // moment "Download and deploy" is accepted, rather than looking untouched.
  const { waiting, refetch: refetchWaiting } = useWaitingDeploys();
  const queuedIds = useMemo(() => new Set(waiting.map((w) => w.entry.recipe_id)), [waiting]);

  const runningIds = useMemo(() => {
    if (!deployments) return new Set<string>();
    // The run's one phase (#205); `status` for a server older than it.
    const live = (d: Deployment) =>
      d.phase ? d.phase === "running" || d.phase === "queued" : d.status === "running" || d.status === "pending";
    return new Set(deployments.filter(live).map(d => d.recipe_id));
  }, [deployments]);

  // The source filter and the search narrow the list first; then what does
  // not run here goes last and folded away: the cluster-only ones with no
  // cluster, and the ones the backend says have no image for this fleet's
  // platform or too few nodes (#172). Unfolded, each is dimmed and says why.
  const visible = useMemo(
    () =>
      (recipes ?? []).filter(
        (r) =>
          inSource(r, source) &&
          matchesQuery(query, r.name, r.model, r.description, (r.engines ?? []).join(" "), r.engine),
      ),
    [recipes, source, query],
  );
  const { available, unavailable } = useMemo(() => {
    const blocked = (r: RecipeSummary) => (r.cluster_only && !clusterAvailable) || wontRunHere(r);
    return { available: visible.filter(r => !blocked(r)), unavailable: visible.filter(blocked) };
  }, [visible, clusterAvailable]);
  const sourceCounts = useMemo(() => {
    const all = recipes ?? [];
    return {
      all: all.length,
      bundled: all.filter((r) => inSource(r, "bundled")).length,
      oci: all.filter((r) => inSource(r, "oci")).length,
      custom: all.filter((r) => inSource(r, "custom")).length,
    };
  }, [recipes]);
  const anyClusterBlocked = unavailable.some(r => r.cluster_only && !clusterAvailable);


  // Custom recipe files are read straight from ~/.config/spark-pulse, and
  // only once somebody asks to see them: the Custom filter is that ask.
  const loadCustomData = async () => {
    setCustomLoading(true);
    try {
      setCustomRecipes(await listCustomRecipes());
      setCustomError(null);
    } catch (e) {
      setCustomError(e instanceof Error ? e.message : t("recipes.loadCustomFailed"));
    } finally {
      setCustomLoaded(true);
      setCustomLoading(false);
    }
  };

  const chooseSource = (next: string) => {
    setSource(next as RecipeSource);
    if (next === "custom" && !customLoaded) void loadCustomData();
  };

  const handleOpenCustomRecipe = async (recipe: CustomRecipeInfo) => {
    setSelectedRecipe(recipe);
    setShowRecipeModal(true);
  };

  /** A custom recipe is also a recipe: the main list carries it as
   *  `custom-<file>`. Refreshing only the file list left a deleted one on
   *  the recipe list until a reload. */
  const customRecipesChanged = async () => {
    await Promise.all([loadCustomData(), refetch()]);
  };

  const handleSaveCustomRecipe = async (id: string, content: string) => {
    await saveCustomRecipe(id, content);
    await customRecipesChanged();
  };

  const handleDeleteCustomRecipe = async (_id: string) => {
    await deleteCustomRecipe(_id);
    await customRecipesChanged();
  };

  /** Deleting from the list, rather than from inside the editor.
   *
   * The editor drawer has had a delete since custom recipes existed, but it is
   * two clicks and a scroll away from the card, so the list looked as though
   * what it created could not be removed.
   */
  const confirmDelete = (kind: "recipe" | "oci", id: string, name: string) =>
    setDeleteTarget({ kind, id, name });

  /** OCI recipes are not read from `~/.config/spark-pulse/custom-recipes`, so
   *  removing one is not a delete on the custom-recipe list — it never
   *  appears there, and `refetch()` (the bundled/upstream/OCI listing) is
   *  what makes it disappear from the card the operator just clicked. */
  const handleUninstallOciRecipe = async (recipeId: string) => {
    await uninstallOciRecipe(ociRecipeName(recipeId));
    await refetch();
  };

  const runDelete = async () => {
    if (!deleteTarget) return;
    const { kind, id, name } = deleteTarget;
    setDeleteTarget(null);
    try {
      if (kind === "recipe") await handleDeleteCustomRecipe(id);
      else await handleUninstallOciRecipe(id);
    } catch (e) {
      setAlertModal({
        title:
          kind === "oci"
            ? t("recipes.uninstallFailed", { name })
            : t("recipes.deleteFailed", { name }),
        message: e instanceof Error ? e.message : t("common.unknownError"),
      });
    }
  };

  const handleSelect = async (recipe: RecipeSummary) => {
    try {
      const [detail, customization] = await Promise.all([
        fetchRecipe(recipe.id),
        fetchRecipeCustomization(recipe.id),
      ]);
      setSelected({ recipe: detail, customization });
    } catch (e) {
      setAlertModal({ title: t("common.error"), message: e instanceof Error ? e.message : t("recipes.loadRecipeFailed") });
    }
  };

  // `?recipe=<id>` — the command palette opens a recipe's drawer here. Read
  // once the list has loaded, then dropped so closing the drawer stays closed.
  // A console lists recipes as a table; under 1200px they stay cards.
  const wide = useMediaQuery("(min-width: 1200px)");
  const [searchParams, setSearchParams] = useSearchParams();
  const linkedRecipe = searchParams.get("recipe");
  useEffect(() => {
    if (!linkedRecipe || !recipes) return;
    const recipe = recipes.find((r) => r.id === linkedRecipe);
    if (recipe) void handleSelect(recipe);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete("recipe");
      return next;
    }, { replace: true });
  }, [linkedRecipe, recipes]);

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
      void refetchWaiting();
      setAlertModal({
        title: t("recipes.downloadStarted"),
        message: t("recipes.downloadStartedBody", { model, name }),
        tone: "info",
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
      setAlertModal({ title: t("common.error"), message: e instanceof Error ? e.message : t("recipes.saveCustomizationFailed") });
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
      setAlertModal({ title: t("common.error"), message: e instanceof Error ? e.message : t("recipes.resetCustomizationFailed") });
    } finally {
      setResetConfirm(null);
    }
  };

  const openResetConfirm = (recipe: RecipeSummary) => {
    setResetConfirm({ recipeId: recipe.id, recipeName: recipe.name });
  };

  const isError = recipesError;

  const stateOf = (r: RecipeSummary) => ({ running: runningIds.has(r.id), queued: queuedIds.has(r.id), tight: tightHere(r) });
  const unavailableStateOf = (r: RecipeSummary) => ({
    clusterBlocked: r.cluster_only && !clusterAvailable,
    notHere: wontRunHere(r) ? r.fit?.reason : undefined,
  });
  const nothingMatches = <>{t("recipes.noMatch")}</>;

  /** The recipe a custom file deploys as: `custom/<stem>` is `custom-<stem>`. */
  const summaryForFile = (file: CustomRecipeInfo) =>
    recipes?.find((r) => r.id === `custom-${file.id.replace(/^custom\//, "")}`);

  return (
    <div>
      <PageHeader
        eyebrow={t("nav.recipes")}
        title={t("recipes.heading")}
        description={recipes ? plural("recipes.meta", recipes.length) : undefined}
        actions={
          <Button variant="primary" icon={Plus} onClick={() => setShowNewRecipe(true)}>
            {t("recipes.newRecipe")}
          </Button>
        }
      />

      <Toolbar>
        <SearchInput value={query} onChange={setQuery} label={t("recipes.search")} clearLabel={t("common.clear")} />
        <Tabs
          label={t("recipes.sourceLabel")}
          value={source}
          onChange={chooseSource}
          tabs={[
            { id: "all", label: t("recipes.sourceAll"), count: sourceCounts.all },
            { id: "bundled", label: t("recipes.sourceBundled"), count: sourceCounts.bundled },
            { id: "oci", label: t("recipes.sourceCollections"), count: sourceCounts.oci },
            { id: "custom", label: t("recipes.sourceCustom"), count: sourceCounts.custom },
          ]}
        />
      </Toolbar>

      {recipesLoading && (
        <div className="flex justify-center py-20">
          <Spinner size="lg" label={t("common.loading")} />
        </div>
      )}
      {isError && !recipesLoading && <ErrorLine>{recipesError}</ErrorLine>}

      {!recipesLoading && recipes && (
        <div className="space-y-4">
          {recipes.length === 0 ? (
            <Section>
              <div className="py-10 text-center text-muted">
                <p>{t("recipes.noRecipes")}</p>
                <p className="mt-1 text-[13px]">{t("recipes.noRecipesHint")}</p>
              </div>
            </Section>
          ) : (
            <>
              {available.length === 0 && unavailable.length > 0 && (
                <p className="text-[13px] text-muted" data-testid="nothing-runs-here">
                  {t("recipes.nothingRunsHere")}
                </p>
              )}
              {wide ? (
                (available.length > 0 || unavailable.length === 0) && (
                  <Section flush>
                    <RecipesTable
                      recipes={available}
                      stateOf={stateOf}
                      onSelect={(r) => void handleSelect(r)}
                      onReset={openResetConfirm}
                      empty={nothingMatches}
                    />
                  </Section>
                )
              ) : available.length === 0 && unavailable.length === 0 ? (
                <p className="py-8 text-center text-[13px] text-muted">{nothingMatches}</p>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  {available.map((r) => (
                    <RecipeCard
                      key={r.id}
                      r={r}
                      isRunning={runningIds.has(r.id)}
                      isQueued={queuedIds.has(r.id)}
                      clusterBlocked={false}
                      tight={tightHere(r)}
                      onSelect={() => handleSelect(r)}
                      onReset={() => openResetConfirm(r)}
                    />
                  ))}
                </div>
              )}

              {unavailable.length > 0 && (
                <div>
                  <button
                    onClick={() => setShowUnavailable(v => !v)}
                    className="mb-2 flex items-center gap-2 text-[13px] text-muted transition-colors hover:text-text"
                  >
                    <ChevronDown size={15} className={`transition-transform ${showUnavailable ? "rotate-180" : ""}`} />
                    {plural(showUnavailable ? "recipes.hideNotHere" : "recipes.showNotHere", unavailable.length)}
                  </button>
                  {/* Why they are unavailable, with the two things that would
                      change it: a count beats naming a setting. */}
                  {anyClusterBlocked && (
                    <p className="mb-2 text-[13px] text-muted">
                      {t("recipes.clusterHint", { count: clusterNodeCount })}{" "}
                      <Link to="/cluster" className="text-blue2 hover:underline">{t("recipes.clusterHintAddNode")}</Link>
                      {" · "}
                      <Link to="/settings" className="text-blue2 hover:underline">{t("recipes.clusterHintForce")}</Link>
                    </p>
                  )}
                  {showUnavailable && wide && (
                    <Section flush>
                      <RecipesTable
                        testId="recipes-table-unavailable"
                        recipes={unavailable}
                        stateOf={unavailableStateOf}
                        onSelect={(r) => void handleSelect(r)}
                        onReset={openResetConfirm}
                      />
                    </Section>
                  )}
                  {showUnavailable && !wide && (
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                      {unavailable.map((r) => (
                        <RecipeCard
                          key={r.id}
                          r={r}
                          isRunning={false}
                          clusterBlocked={r.cluster_only && !clusterAvailable}
                          notHere={wontRunHere(r) ? r.fit?.reason : undefined}
                          onSelect={() => handleSelect(r)}
                          onReset={() => openResetConfirm(r)}
                        />
                      ))}
                    </div>
                  )}
                </div>
              )}
            </>
          )}

          {/* The operator's own recipe files: the YAML they wrote, editable
              and deletable here. Only under the Custom filter. */}
          {source === "custom" && (
            <Section
              flush
              title={t("recipes.customFiles")}
              meta={customLoaded && !customError ? plural("recipes.customRecipeCount", customRecipes.length) : undefined}
              data-testid="custom-recipe-files"
            >
              {customLoading ? (
                <div className="flex justify-center py-8">
                  <Spinner label={t("common.loading")} />
                </div>
              ) : customError ? (
                <div className="p-4">
                  <ErrorLine>{customError}</ErrorLine>
                </div>
              ) : (
                <Table>
                  <THead>
                    <Th>{t("recipesTable.colRecipe")}</Th>
                    <Th>{t("recipesTable.colFile")}</Th>
                    <Th srOnly>{t("runs.actions")}</Th>
                  </THead>
                  <tbody>
                    {customRecipes.length === 0 && (
                      <EmptyRow colSpan={3}>
                        <p>{t("recipes.noCustomRecipes")}</p>
                        <p className="mt-1">{t("recipes.noCustomRecipesHint")}</p>
                      </EmptyRow>
                    )}
                    {customRecipes.map((file) => {
                      const deployable = summaryForFile(file);
                      return (
                        <Tr key={file.id} interactive onClick={() => handleOpenCustomRecipe(file)}>
                          <Td>
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation();
                                void handleOpenCustomRecipe(file);
                              }}
                              className="text-left font-semibold hover:text-blue2"
                            >
                              {file.name}
                            </button>
                          </Td>
                          <Td mono muted>{file.filename}</Td>
                          <Td actions>
                            <IconButton
                              size="sm"
                              icon={Pencil}
                              label={t("recipes.editRecipe", { name: file.name })}
                              onClick={() => void handleOpenCustomRecipe(file)}
                            />
                            <IconButton
                              size="sm"
                              icon={Trash2}
                              label={t("recipes.deleteRecipe", { name: file.name })}
                              onClick={() => confirmDelete("recipe", file.id, file.name)}
                            />
                            {deployable && (
                              <Button
                                size="sm"
                                icon={Rocket}
                                aria-label={t("recipesTable.deployFor", { name: file.name })}
                                onClick={() => void handleSelect(deployable)}
                              >
                                {t("recipes.deploy")}
                              </Button>
                            )}
                          </Td>
                        </Tr>
                      );
                    })}
                  </tbody>
                </Table>
              )}
            </Section>
          )}
        </div>
      )}

      {/* ── Drawers ─────────────────────────────────────────────────────── */}
      {selected && (
        <RecipeDrawer
          recipe={selected.recipe}
          customization={selected.customization}
          isRunning={runningIds.has(selected.recipe.id)}
          clusterAvailable={clusterAvailable}
          onClose={() => setSelected(null)}
          onError={(msg) => setAlertModal({ title: t("common.error"), message: msg })}
          onDeploy={handleDeploy}
          onSaveCustomization={handleSaveCustomization}
          onReset={() => {
            if (selected) {
              return handleReset(selected.recipe.id);
            }
          }}
          onUninstall={
            selected.recipe.source === "oci"
              ? () => {
                  confirmDelete("oci", selected.recipe.id, selected.recipe.name);
                  setSelected(null);
                }
              : undefined
          }
        />
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

      {/* New recipe modal */}
      {showNewRecipe && (
        <NewRecipeModal
          open={showNewRecipe}
          onClose={() => setShowNewRecipe(false)}
          onSave={async (_id, _name, _content) => {
            await customRecipesChanged();
            setShowNewRecipe(false);
          }}
          onError={(msg) => setAlertModal({ title: t("common.error"), message: msg })}
        />
      )}

      {alertModal && (
        <AlertModal open={!!alertModal} onClose={() => setAlertModal(null)} title={alertModal.title} message={alertModal.message} tone={alertModal.tone} />
      )}

      {blockedDeploy && (
        <div data-testid="preflight-block-modal">
          <Modal
            open
            size="md"
            onClose={() => setBlockedDeploy(null)}
            title={t("recipes.preflightBlocked")}
            actions={
              <>
                <Button size="sm" onClick={() => setBlockedDeploy(null)}>
                  {t("common.cancel")}
                </Button>
                <Button size="sm" variant="danger" onClick={handleDeployAnyway}>
                  {t("recipes.deployAnyway")}
                </Button>
              </>
            }
          >
            <PreflightPanel report={blockedDeploy.preflight} />
          </Modal>
        </div>
      )}

      {missingModelDeploy && (
        <div data-testid="missing-model-modal">
          <Modal
            open
            size="md"
            onClose={() => setMissingModelDeploy(null)}
            title={t("recipes.missingModelTitle")}
            actions={
              <>
                <Button size="sm" onClick={() => setMissingModelDeploy(null)}>
                  {t("common.cancel")}
                </Button>
                <Button
                  size="sm"
                  variant="primary"
                  icon={Download}
                  loading={scheduling}
                  onClick={handleDownloadAndDeploy}
                >
                  {t("recipes.downloadAndDeploy")}
                </Button>
              </>
            }
          >
            <div className="space-y-3 text-[14px]">
              <p className="text-muted">
                {t("recipes.missingModelBody", {
                  model: missingModelDeploy.model,
                  name: missingModelDeploy.name,
                })}
              </p>
              <p className="text-muted">{t("recipes.missingModelWait")}</p>
            </div>
          </Modal>
        </div>
      )}

      {deleteTarget && (
        <ConfirmModal
          open={!!deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onConfirm={runDelete}
          title={
            deleteTarget.kind === "recipe"
              ? t("recipes.deleteRecipeTitle")
              : t("recipes.uninstallRecipeTitle")
          }
          message={
            deleteTarget.kind === "oci"
              ? t("recipes.uninstallBody", { name: deleteTarget.name })
              : t("recipes.deleteBody", { name: deleteTarget.name })
          }
          confirmLabel={deleteTarget.kind === "oci" ? t("common.uninstall") : t("common.delete")}
          confirmVariant="danger"
        />
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
