/** OCI Registry page — browse, install, update recipe collections from OCI registries. */

import { useState, useEffect, useCallback } from "react";
import { useI18n } from "@/lib/i18n";
import {
  Package, Download, RefreshCw,
  Plus, AlertCircle, CheckCircle2, Loader2, Clock, XCircle,
  Check, Box, Network, Cpu, Trash2,
} from "lucide-react";
import {
  fetchOciRegistries,
  fetchOciCollections,
  fetchOciMeta,
  fetchOciAutoUpdateSettings,
  updateOciAutoUpdateSettings,
  installOciCollection,
  checkOciUpdates,
  applyOciUpdates,
  addOciRegistry,
  updateOciRegistry,
  removeOciRegistry,
  testOciRegistry,
  runOciAutoUpdate,
  fetchOciCollectionRecipes,
  fetchOciRegistryVersions,
  installOciRecipe,
  updateOciRecipe,
  uninstallOciRecipe,
} from "@/lib/api";
import type { OciRegistry, OciRegistryUpdate, OciCollection, OciCollectionRecipe, OciUpdateCheck } from "@/lib/types";
import { useQuery } from "@/hooks/useQuery";
import {
  AlertModal,
  Button,
  ConfirmModal,
  EmptyState,
  Field,
  IconButton,
  Input,
  Spinner,
  Tabs,
  Toggle,
} from "@/ui";
import SlideDrawer from "@/components/SlideDrawer";
import RegistryCard from "@/components/RegistryCard";
import EditRegistryDialog from "@/components/EditRegistryDialog";
import CollectionCard from "@/components/CollectionCard";

type Tab = "browse" | "installed" | "settings";

export default function OciRegistryPage() {
  const { t } = useI18n();
  const [activeTab, setActiveTab] = useState<Tab>("browse");

  const [alertModal, setAlertModal] = useState<{ title: string; message: string; open: boolean } | null>(null);
  const [drawerCollection, setDrawerCollection] = useState<OciCollection | null>(null);
  const [drawerRecipes, setDrawerRecipes] = useState<OciCollectionRecipe[]>([]);
  const [drawerRecipesLoading, setDrawerRecipesLoading] = useState(false);
  const [recipeActions, setRecipeActions] = useState<Record<string, "installing" | "updating" | "done">>({});
  const [addingRegistry, setAddingRegistry] = useState(false);
  const [editingRegistry, setEditingRegistry] = useState<OciRegistry | null>(null);
  const [newRegName, setNewRegName] = useState("");
  const [newRegUrl, setNewRegUrl] = useState("");
  const [autoUpdating, setAutoUpdating] = useState(false);
  const [registryVersions, setRegistryVersions] = useState<Record<string, string[]>>({});
  /** The recipe the operator asked to uninstall, held until they confirm.
   *  Two of the three uninstall paths asked nothing at all: a click on a bin
   *  icon removed the recipe and reported it afterwards. */
  const [uninstallTarget, setUninstallTarget] = useState<string | null>(null);
  /** The registry the operator asked to forget, same reason. */
  const [removeTarget, setRemoveTarget] = useState<OciRegistry | null>(null);
  /** The auto-update schedule as it is being typed. It used to PUT the whole
   *  settings object on every keystroke, so typing a cron expression saved
   *  every prefix of it — including the invalid ones. */
  const [scheduleDraft, setScheduleDraft] = useState<string | null>(null);
  // Fetch versions for a registry
  const fetchVersionsForRegistry = useCallback(async (regName: string) => {
    try {
      const result = await fetchOciRegistryVersions(regName);
      setRegistryVersions(prev => ({ ...prev, [regName]: result.versions }));
    } catch {
      setRegistryVersions(prev => ({ ...prev, [regName]: [] }));
    }
  }, []);

  // Memoized fetchers to prevent infinite refetch loops
  const fetchCollections = useCallback((signal?: AbortSignal) => fetchOciCollections(undefined, undefined, signal), []);
  const fetchUpdates = useCallback((signal?: AbortSignal) => checkOciUpdates(undefined, undefined, signal), []);

  // Data queries
  const { data: registries, loading: regsLoading, refetch: refetchRegs } = useQuery(fetchOciRegistries);
  const { data: collections, loading: colsLoading, refetch: refetchCols } = useQuery(fetchCollections);
  const { data: ociMeta, loading: metaLoading, refetch: refetchMeta } = useQuery(fetchOciMeta);
  const { data: autoSettings, loading: autoLoading, refetch: refetchAuto } = useQuery(fetchOciAutoUpdateSettings);
  const { data: updates, loading: updatesLoading, refetch: refetchUpdates } = useQuery(fetchUpdates);


  // Fetch versions for each registry when they change
  useEffect(() => {
    registries?.forEach(reg => fetchVersionsForRegistry(reg.name));
  }, [registries, fetchVersionsForRegistry]);

  // Derived state
  const installedNames = new Set(ociMeta?.map(m => m.collection) || []);
  const updateMap = new Map<string, OciUpdateCheck>();
  updates?.forEach(u => updateMap.set(u.collection, u));

  // ── Registry actions ────────────────────────────────────────────────────

  const handleToggleRegistry = async (reg: OciRegistry) => {
    try {
      await updateOciRegistry(reg.name, { enabled: !reg.enabled });
      refetchRegs();
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to update registry", open: true });
    }
  };

  const handleTestRegistry = async (reg: OciRegistry) => {
    try {
      const result = await testOciRegistry(reg.name);
      if (!result.ok) {
        setAlertModal({ title: "Connection Failed", message: `Registry ${reg.name} is not reachable`, open: true });
      }
      refetchRegs();
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Test failed", open: true });
    }
  };

  const handleSaveRegistry = async (name: string, update: OciRegistryUpdate) => {
    await updateOciRegistry(name, update);
    refetchRegs();
    // A changed URL is worth re-validating immediately rather than waiting
    // for the operator to notice it is still marked unreachable; a failed
    // test here is not itself an error, just fresh status.
    if (update.url !== undefined) {
      try {
        await testOciRegistry(name);
      } catch {
        // best-effort re-check; refetch below shows whatever state landed
      }
      refetchRegs();
    }
  };

  const handleRemoveRegistry = async (reg: OciRegistry) => {
    try {
      await removeOciRegistry(reg.name);
      refetchRegs();
      refetchCols();
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to remove registry", open: true });
    }
  };

  const handleAddRegistry = async () => {
    if (!newRegName.trim() || !newRegUrl.trim()) return;
    try {
      await addOciRegistry({
        name: newRegName.trim(),
        url: newRegUrl.trim(),
        enabled: true,
        default: false,
        auth_type: "none",
      });
      setNewRegName("");
      setNewRegUrl("");
      setAddingRegistry(false);
      refetchRegs();
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to add registry", open: true });
    }
  };

  // ── Collection actions ──────────────────────────────────────────────────

  const handleInstall = async (col: OciCollection) => {
    try {
      await installOciCollection(col.name, col.version, col.registry);
      setAlertModal({ title: "Success", message: `Installed ${col.name}:${col.version}`, open: true });
      refetchMeta();
      refetchCols();
    } catch (e) {
      setAlertModal({ title: "Install Failed", message: e instanceof Error ? e.message : "Unknown error", open: true });
    }
  };

  // ── Fetch collection recipes ────────────────────────────────────────────

  const fetchCollectionRecipes = useCallback(async (col: OciCollection) => {
    setDrawerRecipesLoading(true);
    setDrawerRecipes([]);
    try {
      const recipes = await fetchOciCollectionRecipes(col.name, col.version, col.registry);
      setDrawerRecipes(recipes);
    } catch {
      setDrawerRecipes([]);
    } finally {
      setDrawerRecipesLoading(false);
    }
  }, []);

  const handleOpenDrawer = useCallback((col: OciCollection) => {
    setDrawerCollection(col);
    fetchCollectionRecipes(col);
  }, [fetchCollectionRecipes]);

  // ── Individual recipe actions ───────────────────────────────────────────

  const handleInstallRecipe = async (recipeName: string, collection: OciCollection) => {
    setRecipeActions(prev => ({ ...prev, [recipeName]: "installing" }));
    try {
      await installOciRecipe({
        collection: collection.name,
        recipe: recipeName,
        version: collection.version,
        registry: collection.registry,
      });
      setRecipeActions(prev => ({ ...prev, [recipeName]: "done" }));
      setTimeout(() => {
        setRecipeActions(prev => {
          const next = { ...prev };
          delete next[recipeName];
          return next;
        });
      }, 2000);
    } catch (e) {
      setAlertModal({ title: "Install Failed", message: e instanceof Error ? e.message : "Unknown error", open: true });
      setRecipeActions(prev => {
        const next = { ...prev };
        delete next[recipeName];
        return next;
      });
    }
  };

  const handleUpdateRecipe = async (recipeName: string, collection: OciCollection) => {
    setRecipeActions(prev => ({ ...prev, [recipeName]: "updating" }));
    try {
      await updateOciRecipe(recipeName, {
        collection: collection.name,
        version: collection.version,
        registry: collection.registry,
      });
      setRecipeActions(prev => ({ ...prev, [recipeName]: "done" }));
      setTimeout(() => {
        setRecipeActions(prev => {
          const next = { ...prev };
          delete next[recipeName];
          return next;
        });
      }, 2000);
    } catch (e) {
      setAlertModal({ title: "Update Failed", message: e instanceof Error ? e.message : "Unknown error", open: true });
      setRecipeActions(prev => {
        const next = { ...prev };
        delete next[recipeName];
        return next;
      });
    }
  };

  const handleUninstallRecipe = async (recipeName: string) => {
    try {
      await uninstallOciRecipe(recipeName);
      setAlertModal({ title: t("oci.success"), message: t("oci.uninstalled", { name: recipeName }), open: true });
      refetchMeta();
    } catch (e) {
      setAlertModal({ title: t("oci.uninstallFailed"), message: e instanceof Error ? e.message : t("oci.unknownError"), open: true });
    }
  };

  // ── Update actions ──────────────────────────────────────────────────────

  const handleCheckUpdates = async () => {
    await refetchUpdates();
  };

  const handleApplyUpdates = async () => {
    const pending = updates?.filter(u => !u.local_changes) || [];
    if (pending.length === 0) return;

    const params: { collection: string; target_version: string; registry: string }[] = pending.map(u => ({
      collection: u.collection,
      target_version: u.latest_version,
      registry: "",
    }));

    try {
      const results = await applyOciUpdates(params);
      const success = results.filter(r => r.success).length;
      const failed = results.filter(r => !r.success).length;
      setAlertModal({
        title: success > 0 ? "Updates Applied" : "Update Failed",
        message: `${success} succeeded, ${failed} failed`,
        open: true,
      });
      refetchMeta();
      refetchUpdates();
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Update failed", open: true });
    }
  };

  // ── Auto-update actions ─────────────────────────────────────────────────

  const handleToggleAutoUpdate = async (enabled: boolean) => {
    try {
      await updateOciAutoUpdateSettings({ enabled });
      refetchAuto();
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to update settings", open: true });
    }
  };

  /** Saved when the field loses focus, not on every keystroke: the old handler
   *  PUT the settings on each character, so a cron expression was saved once
   *  per prefix and every invalid one along the way was briefly the schedule. */
  const saveSchedule = async (current: string) => {
    if (scheduleDraft === null || scheduleDraft === current) {
      setScheduleDraft(null);
      return;
    }
    try {
      await updateOciAutoUpdateSettings({ schedule: scheduleDraft });
      setScheduleDraft(null);
      refetchAuto();
    } catch (e) {
      setAlertModal({
        title: "Error",
        message: e instanceof Error ? e.message : "Failed to update settings",
        open: true,
      });
    }
  };

  const handleRunAutoUpdate = async () => {
    setAutoUpdating(true);
    try {
      const result = await runOciAutoUpdate();
      if (result.skipped) {
        setAlertModal({ title: "Auto-update", message: result.reason || "Skipped", open: true });
      } else if (result.success) {
        setAlertModal({
          title: "Auto-update Complete",
          message: `${result.updated || 0} recipe(s) updated`,
          open: true,
        });
      } else {
        setAlertModal({ title: "Auto-update Failed", message: result.error || "Unknown error", open: true });
      }
      refetchAuto();
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Auto-update failed", open: true });
    } finally {
      setAutoUpdating(false);
    }
  };

  // ── Render ──────────────────────────────────────────────────────────────

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Package size={24} className="text-blue2" />
            OCI Recipe Registry
          </h1>
          <p className="text-text-muted text-sm">
            Browse, install, and update recipe collections from OCI registries
          </p>
        </div>
      </div>

      {/* Tabs */}
      <Tabs
        label={t("nav.oci")}
        value={activeTab}
        onChange={(id) => setActiveTab(id as Tab)}
        tabs={[
          { id: "browse", label: "Browse" },
          {
            id: "installed",
            label: "Installed",
            count: ociMeta?.length || undefined,
          },
          { id: "settings", label: "Settings" },
        ]}
      />

      {/* Browse Tab */}
      {activeTab === "browse" && (
        <div className="space-y-6">
          {/* Collections Grid */}
          {colsLoading ? (
            <div className="flex items-center justify-center py-16">
              <Spinner size="lg" label={t("common.loading")} />
            </div>
          ) : collections && collections.length > 0 ? (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
              {collections.map(col => (
                <CollectionCard
                  key={`${col.name}-${col.version}`}
                  collection={col}
                  installed={installedNames.has(col.name)}
                  onView={() => handleOpenDrawer(col)}
                  onInstall={() => handleInstall(col)}
                />
              ))}
            </div>
          ) : (
            <div className="text-center py-16 text-text-muted">
              <Package size={48} className="mx-auto mb-4 opacity-30" />
              <p className="text-base font-medium">{t("oci.noCollections")}</p>
              <p className="text-sm mt-2">{t("oci.noCollectionsHint")}</p>
            </div>
          )}
        </div>
      )}

      {/* Installed Tab */}
      {activeTab === "installed" && (
        <div className="space-y-6">
          {/* Update Section */}
          {updates && updates.length > 0 && (
            <div className="p-6 rounded-md bg-surface border border-border">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <RefreshCw size={16} className="text-warning" />
                  <h3 className="font-semibold">{t("oci.availableUpdates")}</h3>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={handleCheckUpdates}
                    disabled={updatesLoading}
                    className="px-3 py-1.5 rounded-md text-sm border border-border hover:bg-surface-hover transition-colors"
                  >
                    {updatesLoading ? <Loader2 size={14} className="animate-spin" /> : "Check"}
                  </button>
                  <button
                    onClick={handleApplyUpdates}
                    disabled={updates.some(u => u.local_changes)}
                    className="px-3 py-1.5 rounded-sm text-sm bg-warning text-warning-foreground hover:bg-warning/90 transition-colors disabled:opacity-50"
                  >
                    Apply All
                  </button>
                </div>
              </div>
              <div className="space-y-3">
                {updates.map(u => (
                  <div key={u.collection} className="flex items-center justify-between p-4 rounded-md bg-surface-pressed border border-border">
                    <div className="flex items-center gap-3">
                      {u.local_changes ? (
                        <AlertCircle size={16} className="text-warning" />
                      ) : (
                        <CheckCircle2 size={16} className="text-success" />
                      )}
                      <div>
                        <span className="font-mono font-semibold">{u.collection}</span>
                        <span className="text-text-muted text-sm ml-2">
                          {u.current_version} → {u.latest_version}
                        </span>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 text-sm">
                      {u.added_recipes.length > 0 && (
                        <span className="text-success">+{u.added_recipes.length}</span>
                      )}
                      {u.modified_recipes.length > 0 && (
                        <span className="text-warning">~{u.modified_recipes.length}</span>
                      )}
                      {u.local_changes && (
                        <span className="text-warning text-xs">{t("oci.localChanges")}</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Installed Collections */}
          {metaLoading ? (
            <div className="flex items-center justify-center py-16">
              <Spinner size="lg" label={t("common.loading")} />
            </div>
          ) : ociMeta && ociMeta.length > 0 ? (
            <div className="space-y-3">
              {ociMeta.map(meta => (
                <div
                  key={meta.name}
                  className="flex items-center justify-between p-3 rounded-md border border-border bg-surface"
                >
                  <div className="flex items-center gap-3">
                    <Download size={16} className="text-blue2" />
                    <div>
                      <span className="font-mono font-semibold">{meta.name}</span>
                      <span className="text-text-muted text-sm ml-2">
                        {meta.collection}@{meta.version}
                      </span>
                      <span className="text-text-muted text-sm ml-1">
                        ({meta.source})
                      </span>
                    </div>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="text-sm text-text-muted">
                      {new Date(meta.installed_at).toLocaleDateString()}
                    </span>
                    {meta.local_changes && (
                      <span className="text-warning text-xs">{t("oci.modified")}</span>
                    )}
                    <IconButton
                      size="sm"
                      icon={Trash2}
                      label={t("oci.uninstallRecipe")}
                      onClick={() => setUninstallTarget(meta.name)}
                      className="border-transparent text-muted hover:text-bad hover:border-line"
                    />
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState icon={Download} hint={t("oci.noInstalledHint")}>
              {t("oci.noInstalled")}
            </EmptyState>
          )}
        </div>
      )}

      {/* Settings Tab */}
      {activeTab === "settings" && (
        <div className="space-y-8">
          {/* Registries */}
          <div className="p-6 rounded-md bg-surface border border-border">
            <div className="flex items-center justify-between mb-5">
              <div className="flex items-center gap-2">
                <Package size={16} className="text-blue2" />
                <h3 className="font-semibold">{t("oci.registries")}</h3>
              </div>
              <Button size="sm" variant="primary" icon={Plus} onClick={() => setAddingRegistry(true)}>
                {t("oci.addRegistryButton")}
              </Button>
            </div>
            {regsLoading ? (
              <div className="flex items-center justify-center py-12">
                <Spinner size="lg" label={t("common.loading")} />
              </div>
            ) : registries && registries.length > 0 ? (
              <div className="space-y-3">
                {registries.map(reg => (
                  <RegistryCard
                    key={reg.name}
                    reg={reg}
                    versions={registryVersions[reg.name] || []}
                    onToggle={() => handleToggleRegistry(reg)}
                    onTest={() => handleTestRegistry(reg)}
                    onRemove={() => setRemoveTarget(reg)}
                    onEdit={() => setEditingRegistry(reg)}
                  />
                ))}
              </div>
            ) : (
              <EmptyState icon={Package}>{t("oci.noRegistries")}</EmptyState>
            )}

            {/* Add Registry Form */}
            {addingRegistry && (
              <div className="mt-6 p-5 rounded-md border border-border bg-surface-pressed">
                <h3 className="font-semibold mb-4">{t("oci.addRegistry")}</h3>
                <div className="space-y-4">
                  <div>
                    <label className="block text-sm text-text-muted mb-1">{t("oci.name")}</label>
                    <input
                      type="text"
                      value={newRegName}
                      onChange={e => setNewRegName(e.target.value)}
                      placeholder={t("oci.namePlaceholder")}
                      className="w-full px-3 py-2 rounded-md border border-border bg-surface text-sm"
                    />
                  </div>
                  <div>
                    <label className="block text-sm text-text-muted mb-1">{t("oci.url")}</label>
                    <input
                      type="text"
                      value={newRegUrl}
                      onChange={e => setNewRegUrl(e.target.value)}
                      placeholder={t("oci.urlPlaceholder")}
                      className="w-full px-3 py-2 rounded-md border border-border bg-surface text-sm"
                    />
                  </div>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="primary"
                      onClick={handleAddRegistry}
                      disabled={!newRegName.trim() || !newRegUrl.trim()}
                    >
                      Add
                    </Button>
                    <Button
                      size="sm"
                      onClick={() => { setAddingRegistry(false); setNewRegName(""); setNewRegUrl(""); }}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Auto-Update */}
          <div className="p-6 rounded-md bg-surface border border-border">
            <div className="flex items-center gap-2 mb-5">
              <Clock size={16} className="text-blue2" />
              <h3 className="font-semibold">{t("oci.autoUpdate")}</h3>
            </div>
            {autoLoading ? (
              <div className="flex items-center justify-center py-12">
                <Spinner size="lg" label={t("common.loading")} />
              </div>
            ) : autoSettings && (
              <div className="space-y-5">
                <div className="flex items-center justify-between">
                  <div className="space-y-1">
                    <span className="font-medium">{t("oci.enableAutoUpdate")}</span>
                    <p className="text-sm text-text-muted">
                      Check for updates on a schedule
                    </p>
                  </div>
                  <Toggle
                    on={autoSettings.enabled}
                    onChange={handleToggleAutoUpdate}
                    label={t("oci.enableAutoUpdate")}
                  />
                </div>

                <div className="flex items-end gap-4">
                  <Field label={t("oci.schedule")} className="flex-1">
                    {(control) => (
                      <Input
                        {...control}
                        mono
                        type="text"
                        value={scheduleDraft ?? autoSettings.schedule}
                        onChange={(e) => setScheduleDraft(e.target.value)}
                        onBlur={() => saveSchedule(autoSettings.schedule)}
                      />
                    )}
                  </Field>
                  <Button variant="primary" loading={autoUpdating} onClick={handleRunAutoUpdate}>
                    Run Now
                  </Button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Collection Drawer */}
      <SlideDrawer
        open={!!drawerCollection}
        onClose={() => setDrawerCollection(null)}
        header={
          <div className="space-y-2">
            <div className="flex items-center gap-3">
              <Package size={20} className="text-blue2" />
              <span className="text-xl font-mono font-bold">{drawerCollection?.name}</span>
              <span className="text-sm text-text-muted font-mono">v{drawerCollection?.version}</span>
            </div>
            <p className="text-sm text-text-muted">{drawerCollection?.description}</p>
          </div>
        }
        actions={
          <button onClick={() => setDrawerCollection(null)} className="p-1.5 rounded hover:bg-surface-pressed">
            <XCircle size={18} />
          </button>
        }
      >
        <div className="space-y-6 py-4">
          {/* Recipes List */}
          <div className="px-4">
            {drawerRecipesLoading ? (
              <div className="flex items-center gap-2 py-4 text-text-muted">
                <Loader2 size={16} className="animate-spin" />
                <span className="text-sm">{t("oci.loadingRecipes")}</span>
              </div>
            ) : drawerRecipes.length > 0 ? (
              drawerRecipes.map((recipe, idx) => {
                const action = recipeActions[recipe.name];
                const isInstalling = action === "installing";
                const isUpdating = action === "updating";
                const isDone = action === "done";
                const isInstalled = ociMeta?.some(m => m.name === recipe.name) || false;

                return (
                  <div
                    key={idx}
                    className="p-4 rounded-md border border-border bg-surface hover:bg-surface-hover transition-colors mb-2"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-mono font-semibold text-sm">{recipe.name}</span>
                          <span className="text-xs text-text-muted font-mono">v{recipe.recipe_version}</span>
                          {isDone && (
                            <CheckCircle2 size={14} className="text-success" />
                          )}
                        </div>
                        <p className="text-sm text-text-muted mt-1">{recipe.description}</p>
                        <div className="flex items-center gap-2 mt-2 text-xs flex-wrap">
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-text-muted">
                            <Box size={12} />{recipe.container || "N/A"}
                          </span>
                          {(recipe.solo_only || (!recipe.solo_only && !recipe.cluster_only)) && (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-primary/20 text-blue2">
                              <Cpu size={11} />Solo
                            </span>
                          )}
                          {(recipe.cluster_only || (!recipe.solo_only && !recipe.cluster_only)) && (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-warning/20 text-warning">
                              <Network size={11} />Cluster
                            </span>
                          )}
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        {isInstalling ? (
                          <Spinner label={t("common.loading")} />
                        ) : isUpdating ? (
                          <Spinner className="text-warn" label={t("common.loading")} />
                        ) : isDone ? (
                          <Check size={16} className="text-success" />
                        ) : (
                          <>
                            {isInstalled ? (
                              <>
                                <Button
                                  size="sm"
                                  icon={RefreshCw}
                                  title={t("oci.updateRecipe")}
                                  onClick={() => handleUpdateRecipe(recipe.name, drawerCollection!)}
                                >
                                  {t("oci.update")}
                                </Button>
                                <Button
                                  size="sm"
                                  variant="danger"
                                  icon={Trash2}
                                  title={t("oci.uninstallRecipe")}
                                  onClick={() => setUninstallTarget(recipe.name)}
                                >
                                  {t("oci.uninstall")}
                                </Button>
                              </>
                            ) : (
                              <Button
                                size="sm"
                                variant="primary"
                                icon={Download}
                                title={t("oci.installRecipe")}
                                onClick={() => handleInstallRecipe(recipe.name, drawerCollection!)}
                              >
                                {t("oci.install")}
                              </Button>
                            )}
                          </>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })
            ) : (
              <div className="py-4 text-center text-text-muted text-sm">
                No recipes found for this collection
              </div>
            )}
          </div>

          {/* Install Collection Button - kept for bulk install */}
          <div className="flex justify-end pt-2 pr-4 border-t border-border">
            <Button
              variant="primary"
              icon={Download}
              title={t("oci.installAll")}
              onClick={() => drawerCollection && handleInstall(drawerCollection)}
            >
              Install All Recipes
            </Button>
          </div>
        </div>
      </SlideDrawer>

      {/* Edit Registry Dialog */}
      <EditRegistryDialog
        reg={editingRegistry}
        onClose={() => setEditingRegistry(null)}
        onSave={handleSaveRegistry}
      />

      {uninstallTarget && (
        <ConfirmModal
          open
          onClose={() => setUninstallTarget(null)}
          onConfirm={async () => {
            const name = uninstallTarget;
            setUninstallTarget(null);
            await handleUninstallRecipe(name);
          }}
          title={t("oci.uninstallRecipe")}
          message={t("oci.uninstallConfirm", { name: uninstallTarget })}
          confirmLabel={t("common.uninstall")}
          confirmVariant="danger"
        />
      )}

      {removeTarget && (
        <ConfirmModal
          open
          onClose={() => setRemoveTarget(null)}
          onConfirm={async () => {
            const reg = removeTarget;
            setRemoveTarget(null);
            await handleRemoveRegistry(reg);
          }}
          title={t("oci.removeRegistry")}
          message={t("oci.removeRegistryConfirm", { name: removeTarget.name })}
          confirmLabel={t("common.delete")}
          confirmVariant="danger"
        />
      )}

      {/* Alert Modal */}
      {alertModal && alertModal.open && (
        <AlertModal
          open={alertModal.open}
          title={alertModal.title}
          message={alertModal.message}
          onClose={() => setAlertModal(null)}
        />
      )}
    </div>
  );
}
