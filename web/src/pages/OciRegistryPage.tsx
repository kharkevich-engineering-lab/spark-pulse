/** OCI Registry page — browse, install, update recipe collections from OCI registries. */

import { useState, useEffect, useCallback } from "react";
import {
  Package, Settings as SettingsIcon, Download, RefreshCw,
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
import type { OciRegistry, OciCollection, OciCollectionRecipe, OciUpdateCheck } from "@/lib/types";
import { useQuery } from "@/hooks/useQuery";
import { AlertModal } from "@/components/Modal";
import SlideDrawer from "@/components/SlideDrawer";
import RegistryCard from "@/components/RegistryCard";
import CollectionCard from "@/components/CollectionCard";
import { setRefresh } from "@/lib/refresh";

type Tab = "browse" | "installed" | "settings";

export default function OciRegistryPage() {
  const [activeTab, setActiveTab] = useState<Tab>("browse");

  const [alertModal, setAlertModal] = useState<{ title: string; message: string; open: boolean } | null>(null);
  const [drawerCollection, setDrawerCollection] = useState<OciCollection | null>(null);
  const [drawerRecipes, setDrawerRecipes] = useState<OciCollectionRecipe[]>([]);
  const [drawerRecipesLoading, setDrawerRecipesLoading] = useState(false);
  const [recipeActions, setRecipeActions] = useState<Record<string, "installing" | "updating" | "done">>({});
  const [addingRegistry, setAddingRegistry] = useState(false);
  const [newRegName, setNewRegName] = useState("");
  const [newRegUrl, setNewRegUrl] = useState("");
  const [autoUpdating, setAutoUpdating] = useState(false);
  const [registryVersions, setRegistryVersions] = useState<Record<string, string[]>>({});
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
  const fetchCollections = useCallback(() => fetchOciCollections(), []);
  const fetchUpdates = useCallback(() => checkOciUpdates(), []);

  // Data queries
  const { data: registries, loading: regsLoading, refetch: refetchRegs } = useQuery(fetchOciRegistries);
  const { data: collections, loading: colsLoading, refetch: refetchCols } = useQuery(fetchCollections);
  const { data: ociMeta, loading: metaLoading, refetch: refetchMeta } = useQuery(fetchOciMeta);
  const { data: autoSettings, loading: autoLoading, refetch: refetchAuto } = useQuery(fetchOciAutoUpdateSettings);
  const { data: updates, loading: updatesLoading, refetch: refetchUpdates } = useQuery(fetchUpdates);

  useEffect(() => { setRefresh(refetchCols); }, [refetchCols]);

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
      setAlertModal({ title: "Success", message: `Uninstalled ${recipeName}`, open: true });
      refetchMeta();
    } catch (e) {
      setAlertModal({ title: "Uninstall Failed", message: e instanceof Error ? e.message : "Unknown error", open: true });
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
            <Package size={24} className="text-primary" />
            OCI Recipe Registry
          </h1>
          <p className="text-text-muted text-sm">
            Browse, install, and update recipe collections from OCI registries
          </p>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-border pb-0">
        {([
          { key: "browse" as Tab, label: "Browse", icon: Package },
          { key: "installed" as Tab, label: "Installed", icon: Download },
          { key: "settings" as Tab, label: "Settings", icon: SettingsIcon },
        ]).map(tab => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`flex items-center gap-2 px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              activeTab === tab.key
                ? "border-primary text-primary"
                : "border-transparent text-text-muted hover:text-foreground"
            }`}
          >
            <tab.icon size={16} />
            {tab.label}
            {tab.key === "installed" && (ociMeta?.length || 0) > 0 && (
              <span className="ml-1 px-1.5 py-0.5 rounded-full text-xs bg-primary/15 text-primary">
                {ociMeta?.length}
              </span>
            )}
            {tab.key === "installed" && updates?.some(u => !u.local_changes) && (
              <span className="ml-1 px-1.5 py-0.5 rounded-full text-xs bg-success/15 text-success">
                {updates.filter(u => !u.local_changes).length}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Browse Tab */}
      {activeTab === "browse" && (
        <div className="space-y-6">
          {/* Collections Grid */}
          {colsLoading ? (
            <div className="flex items-center justify-center py-16">
              <Loader2 size={24} className="animate-spin text-primary" />
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
              <p className="text-base font-medium">No collections found</p>
              <p className="text-sm mt-2">Configure registries in Settings to browse collections</p>
            </div>
          )}
        </div>
      )}

      {/* Installed Tab */}
      {activeTab === "installed" && (
        <div className="space-y-6">
          {/* Update Section */}
          {updates && updates.length > 0 && (
            <div className="p-6 rounded-xl bg-surface border border-border">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <RefreshCw size={16} className="text-warning" />
                  <h3 className="font-semibold">Available Updates</h3>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={handleCheckUpdates}
                    disabled={updatesLoading}
                    className="px-3 py-1.5 rounded-lg text-sm border border-border hover:bg-surface-hover transition-colors"
                  >
                    {updatesLoading ? <Loader2 size={14} className="animate-spin" /> : "Check"}
                  </button>
                  <button
                    onClick={handleApplyUpdates}
                    disabled={updates.some(u => u.local_changes)}
                    className="px-3 py-1.5 rounded-lg text-sm bg-warning text-warning-foreground hover:bg-warning/90 transition-colors disabled:opacity-50"
                  >
                    Apply All
                  </button>
                </div>
              </div>
              <div className="space-y-3">
                {updates.map(u => (
                  <div key={u.collection} className="flex items-center justify-between p-4 rounded-lg bg-surface-pressed border border-border">
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
                        <span className="text-warning text-xs">Local changes</span>
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
              <Loader2 size={24} className="animate-spin text-primary" />
            </div>
          ) : ociMeta && ociMeta.length > 0 ? (
            <div className="space-y-3">
              {ociMeta.map(meta => (
                <div
                  key={meta.name}
                  className="flex items-center justify-between p-3 rounded-lg border border-border bg-surface"
                >
                  <div className="flex items-center gap-3">
                    <Download size={16} className="text-primary" />
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
                      <span className="text-warning text-xs">Modified</span>
                    )}
                    <button
                      onClick={() => handleUninstallRecipe(meta.name)}
                      className="p-1.5 rounded-lg hover:bg-danger/10 text-text-muted hover:text-danger transition-colors"
                      title="Uninstall this recipe"
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-center py-16 text-text-muted">
              <Download size={48} className="mx-auto mb-4 opacity-30" />
              <p className="text-base font-medium">No OCI recipes installed</p>
              <p className="text-sm mt-2">Browse collections to install recipes</p>
            </div>
          )}
        </div>
      )}

      {/* Settings Tab */}
      {activeTab === "settings" && (
        <div className="space-y-8">
          {/* Registries */}
          <div className="p-6 rounded-xl bg-surface border border-border">
            <div className="flex items-center justify-between mb-5">
              <div className="flex items-center gap-2">
                <Package size={16} className="text-primary" />
                <h3 className="font-semibold">Registries</h3>
              </div>
              <button
                onClick={() => setAddingRegistry(true)}
                className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-sm bg-primary text-white hover:bg-primary/90 transition-colors"
              >
                <Plus size={14} />
                Add Registry
              </button>
            </div>
            {regsLoading ? (
              <div className="flex items-center justify-center py-12">
                <Loader2 size={20} className="animate-spin text-primary" />
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
                    onRemove={() => handleRemoveRegistry(reg)}
                    onVersionChange={(version) => {
                      console.log(`Registry ${reg.name} version changed to ${version}`);
                    }}
                  />
                ))}
              </div>
            ) : (
              <div className="text-center py-12 text-text-muted">
                <Package size={32} className="mx-auto mb-3 opacity-30" />
                <p className="text-sm font-medium">No registries configured</p>
              </div>
            )}

            {/* Add Registry Form */}
            {addingRegistry && (
              <div className="mt-6 p-5 rounded-lg border border-border bg-surface-pressed">
                <h3 className="font-semibold mb-4">Add New Registry</h3>
                <div className="space-y-4">
                  <div>
                    <label className="block text-sm text-text-muted mb-1">Name</label>
                    <input
                      type="text"
                      value={newRegName}
                      onChange={e => setNewRegName(e.target.value)}
                      placeholder="my-registry"
                      className="w-full px-3 py-2 rounded-lg border border-border bg-surface text-sm"
                    />
                  </div>
                  <div>
                    <label className="block text-sm text-text-muted mb-1">URL</label>
                    <input
                      type="text"
                      value={newRegUrl}
                      onChange={e => setNewRegUrl(e.target.value)}
                      placeholder="ghcr.io/owner/recipe-repo"
                      className="w-full px-3 py-2 rounded-lg border border-border bg-surface text-sm"
                    />
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={handleAddRegistry}
                      disabled={!newRegName.trim() || !newRegUrl.trim()}
                      className="px-4 py-2 rounded-lg text-sm bg-primary text-white hover:bg-primary/90 disabled:opacity-50"
                    >
                      Add
                    </button>
                    <button
                      onClick={() => { setAddingRegistry(false); setNewRegName(""); setNewRegUrl(""); }}
                      className="px-4 py-2 rounded-lg text-sm border border-border hover:bg-surface-hover"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Auto-Update */}
          <div className="p-6 rounded-xl bg-surface border border-border">
            <div className="flex items-center gap-2 mb-5">
              <Clock size={16} className="text-primary" />
              <h3 className="font-semibold">Auto-Update</h3>
            </div>
            {autoLoading ? (
              <div className="flex items-center justify-center py-12">
                <Loader2 size={20} className="animate-spin text-primary" />
              </div>
            ) : autoSettings && (
              <div className="space-y-5">
                <div className="flex items-center justify-between">
                  <div className="space-y-1">
                    <span className="font-medium">Enable Auto-Update</span>
                    <p className="text-sm text-text-muted">
                      Check for updates on a schedule
                    </p>
                  </div>
                  <button
                    onClick={() => handleToggleAutoUpdate(!autoSettings.enabled)}
                    className={`relative w-12 h-6 rounded-full transition-colors ${
                      autoSettings.enabled ? "bg-success" : "bg-text-muted/30"
                    }`}
                  >
                    <span
                      className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
                        autoSettings.enabled ? "translate-x-6" : ""
                      }`}
                    />
                  </button>
                </div>

                <div className="flex items-center gap-4">
                  <div className="flex-1">
                    <label className="block text-sm text-text-muted mb-2">Schedule (cron)</label>
                    <input
                      type="text"
                      value={autoSettings.schedule}
                      onChange={e => updateOciAutoUpdateSettings({ schedule: e.target.value })}
                      className="w-full px-3 py-2 rounded-lg border border-border bg-surface text-sm font-mono"
                    />
                  </div>
                  <button
                    onClick={handleRunAutoUpdate}
                    disabled={autoUpdating}
                    className="px-4 py-2 rounded-lg text-sm bg-primary text-white hover:bg-primary/90 disabled:opacity-50 mt-6"
                  >
                    {autoUpdating ? (
                      <Loader2 size={16} className="animate-spin inline" />
                    ) : (
                      "Run Now"
                    )}
                  </button>
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
              <Package size={20} className="text-primary" />
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
                <span className="text-sm">Loading recipes...</span>
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
                    className="p-4 rounded-lg border border-border bg-surface hover:bg-surface-hover transition-colors mb-2"
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
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-primary/20 text-primary">
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
                          <Loader2 size={16} className="animate-spin text-primary" />
                        ) : isUpdating ? (
                          <Loader2 size={16} className="animate-spin text-warning" />
                        ) : isDone ? (
                          <Check size={16} className="text-success" />
                        ) : (
                          <>
                            {isInstalled ? (
                              <>
                                <button
                                  onClick={() => handleUpdateRecipe(recipe.name, drawerCollection!)}
                                  className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs bg-warning text-warning-foreground hover:bg-warning/90 transition-colors font-medium"
                                  title="Update this recipe"
                                >
                                  <RefreshCw size={12} />
                                  Update
                                </button>
                                <button
                                  onClick={() => handleUninstallRecipe(recipe.name)}
                                  className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs border border-border hover:bg-danger/10 hover:text-danger hover:border-danger transition-colors"
                                  title="Uninstall this recipe"
                                >
                                  <Trash2 size={12} />
                                  Uninstall
                                </button>
                              </>
                            ) : (
                              <button
                                onClick={() => handleInstallRecipe(recipe.name, drawerCollection!)}
                                className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs bg-primary text-white hover:bg-primary/90 transition-colors font-medium"
                                title="Install this recipe"
                              >
                                <Download size={12} />
                                Install
                              </button>
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
            <button
              onClick={() => drawerCollection && handleInstall(drawerCollection)}
              className="flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-white hover:bg-primary/90 transition-colors font-medium text-sm"
              title="Install all recipes in this collection"
            >
              <Download size={16} />
              Install All Recipes
            </button>
          </div>
        </div>
      </SlideDrawer>

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
