/** Recipe collections: what a registry offers, and what is installed from it.
 *
 * Two sub-tabs, not three. The third was "Settings" — the registries a control
 * plane reads and the schedule it reads them on — which is configuration and
 * belongs with the rest of it; what is left here is the browsing and the
 * installing, plus the registry list itself, because a registry you cannot
 * reach is a collection you cannot see and the fix belongs beside the symptom.
 */

import { useState, useEffect, useCallback } from "react";
import { useI18n } from "@/lib/i18n";
import { Download, Package, RefreshCw, Trash2, XCircle } from "lucide-react";
import {
  fetchOciCollections,
  fetchOciMeta,
  installOciCollection,
  checkOciUpdates,
  applyOciUpdates,
  addOciRegistry,
  updateOciRegistry,
  removeOciRegistry,
  testOciRegistry,
  fetchOciCollectionRecipes,
  fetchOciRegistryVersions,
  installOciRecipe,
  updateOciRecipe,
  uninstallOciRecipe,
} from "@/lib/api";
import type {
  OciRegistry,
  OciRegistryUpdate,
  OciCollection,
  OciCollectionRecipe,
  OciUpdateCheck,
} from "@/lib/types";
import { useQuery, type UseQueryResult } from "@/hooks/useQuery";
import {
  AlertModal,
  Button,
  ConfirmModal,
  EmptyState,
  Field,
  IconButton,
  Input,
  Modal,
  Spinner,
  Tabs,
} from "@/ui";
import SlideDrawer from "@/components/SlideDrawer";
import RegistryCard from "@/components/RegistryCard";
import EditRegistryDialog from "@/components/EditRegistryDialog";
import CollectionCard from "@/components/CollectionCard";

type SubTab = "browse" | "installed";

export interface RegistriesTabProps {
  registries: UseQueryResult<OciRegistry[]>;
}

export default function RegistriesTab({ registries: registriesQuery }: RegistriesTabProps) {
  const { t } = useI18n();
  const [activeTab, setActiveTab] = useState<SubTab>("browse");

  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);
  const [drawerCollection, setDrawerCollection] = useState<OciCollection | null>(null);
  const [drawerRecipes, setDrawerRecipes] = useState<OciCollectionRecipe[]>([]);
  const [drawerRecipesLoading, setDrawerRecipesLoading] = useState(false);
  const [recipeActions, setRecipeActions] = useState<Record<string, "installing" | "updating" | "done">>({});
  const [addingRegistry, setAddingRegistry] = useState(false);
  const [editingRegistry, setEditingRegistry] = useState<OciRegistry | null>(null);
  const [newRegName, setNewRegName] = useState("");
  const [newRegUrl, setNewRegUrl] = useState("");
  const [registryVersions, setRegistryVersions] = useState<Record<string, string[]>>({});
  /** The recipe the operator asked to install or uninstall, held until they
   *  confirm. Two of the three uninstall paths asked nothing at all: a click
   *  on a bin icon removed the recipe and reported it afterwards. */
  const [installTarget, setInstallTarget] = useState<OciCollection | null>(null);
  const [uninstallTarget, setUninstallTarget] = useState<string | null>(null);
  /** The registry the operator asked to forget, same reason. */
  const [removeTarget, setRemoveTarget] = useState<OciRegistry | null>(null);

  const fetchVersionsForRegistry = useCallback(async (regName: string) => {
    try {
      const result = await fetchOciRegistryVersions(regName);
      setRegistryVersions((prev) => ({ ...prev, [regName]: result.versions }));
    } catch {
      setRegistryVersions((prev) => ({ ...prev, [regName]: [] }));
    }
  }, []);

  // Memoized fetchers to prevent infinite refetch loops
  const fetchCollections = useCallback(
    (signal?: AbortSignal) => fetchOciCollections(undefined, undefined, signal),
    [],
  );
  const fetchUpdates = useCallback(
    (signal?: AbortSignal) => checkOciUpdates(undefined, undefined, signal),
    [],
  );

  const { data: registries, loading: regsLoading, refetch: refetchRegs } = registriesQuery;
  const { data: collections, loading: colsLoading, refetch: refetchCols } = useQuery(fetchCollections);
  const { data: ociMeta, loading: metaLoading, refetch: refetchMeta } = useQuery(fetchOciMeta);
  const { data: updates, loading: updatesLoading, refetch: refetchUpdates } = useQuery(fetchUpdates);

  useEffect(() => {
    registries?.forEach((reg) => fetchVersionsForRegistry(reg.name));
  }, [registries, fetchVersionsForRegistry]);

  const installedNames = new Set(ociMeta?.map((m) => m.collection) || []);

  const failed = (title: string, e: unknown) =>
    setAlert({ title, message: e instanceof Error ? e.message : t("common.unknownError") });

  // ── Registry actions ────────────────────────────────────────────────────

  const handleToggleRegistry = async (reg: OciRegistry) => {
    try {
      await updateOciRegistry(reg.name, { enabled: !reg.enabled });
      refetchRegs();
    } catch (e) {
      failed(t("library.registryUpdateFailed"), e);
    }
  };

  const handleTestRegistry = async (reg: OciRegistry) => {
    try {
      const result = await testOciRegistry(reg.name);
      if (!result.ok) {
        setAlert({
          title: t("library.connectionFailed"),
          message: t("library.registryUnreachable", { name: reg.name }),
        });
      }
      refetchRegs();
    } catch (e) {
      failed(t("library.testFailed"), e);
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
      failed(t("library.registryRemoveFailed"), e);
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
      failed(t("library.registryAddFailed"), e);
    }
  };

  // ── Collection actions ──────────────────────────────────────────────────

  const handleInstall = async (col: OciCollection) => {
    try {
      await installOciCollection(col.name, col.version, col.registry);
      setAlert({
        title: t("oci.success"),
        message: t("library.installedCollection", { name: col.name, version: col.version }),
      });
      refetchMeta();
      refetchCols();
    } catch (e) {
      failed(t("library.installFailed"), e);
    }
  };

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

  const handleOpenDrawer = useCallback(
    (col: OciCollection) => {
      setDrawerCollection(col);
      fetchCollectionRecipes(col);
    },
    [fetchCollectionRecipes],
  );

  // ── Individual recipe actions ───────────────────────────────────────────

  const clearAction = (recipeName: string) =>
    setRecipeActions((prev) => {
      const next = { ...prev };
      delete next[recipeName];
      return next;
    });

  const handleInstallRecipe = async (recipeName: string, collection: OciCollection) => {
    setRecipeActions((prev) => ({ ...prev, [recipeName]: "installing" }));
    try {
      await installOciRecipe({
        collection: collection.name,
        recipe: recipeName,
        version: collection.version,
        registry: collection.registry,
      });
      setRecipeActions((prev) => ({ ...prev, [recipeName]: "done" }));
      refetchMeta();
      setTimeout(() => clearAction(recipeName), 2000);
    } catch (e) {
      failed(t("library.installFailed"), e);
      clearAction(recipeName);
    }
  };

  const handleUpdateRecipe = async (recipeName: string, collection: OciCollection) => {
    setRecipeActions((prev) => ({ ...prev, [recipeName]: "updating" }));
    try {
      await updateOciRecipe(recipeName, {
        collection: collection.name,
        version: collection.version,
        registry: collection.registry,
      });
      setRecipeActions((prev) => ({ ...prev, [recipeName]: "done" }));
      setTimeout(() => clearAction(recipeName), 2000);
    } catch (e) {
      failed(t("library.updateFailed"), e);
      clearAction(recipeName);
    }
  };

  const handleUninstallRecipe = async (recipeName: string) => {
    try {
      await uninstallOciRecipe(recipeName);
      setAlert({ title: t("oci.success"), message: t("oci.uninstalled", { name: recipeName }) });
      refetchMeta();
    } catch (e) {
      failed(t("oci.uninstallFailed"), e);
    }
  };

  // ── Update actions ──────────────────────────────────────────────────────

  const handleApplyUpdates = async () => {
    const pending = updates?.filter((u) => !u.local_changes) || [];
    if (pending.length === 0) return;

    const params = pending.map((u) => ({
      collection: u.collection,
      target_version: u.latest_version,
      registry: "",
    }));

    try {
      const results = await applyOciUpdates(params);
      const succeeded = results.filter((r) => r.success).length;
      setAlert({
        title: succeeded > 0 ? t("library.updatesApplied") : t("library.updateFailed"),
        message: t("library.updateOutcome", {
          succeeded,
          failed: results.length - succeeded,
        }),
      });
      refetchMeta();
      refetchUpdates();
    } catch (e) {
      failed(t("library.updateFailed"), e);
    }
  };

  // ── Render ──────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      <Tabs
        label={t("library.tabRegistries")}
        value={activeTab}
        onChange={(id) => setActiveTab(id as SubTab)}
        tabs={[
          { id: "browse", label: t("library.browse") },
          { id: "installed", label: t("library.installed"), count: ociMeta?.length || undefined },
        ]}
      />

      {activeTab === "browse" && (
        <div className="space-y-6">
          {colsLoading ? (
            <div className="flex items-center justify-center py-16">
              <Spinner size="lg" label={t("common.loading")} />
            </div>
          ) : collections && collections.length > 0 ? (
            <div className="grid grid-cols-1 gap-6 min-[600px]:grid-cols-2 min-[1000px]:grid-cols-3">
              {collections.map((col) => (
                <CollectionCard
                  key={`${col.name}-${col.version}`}
                  collection={col}
                  installed={installedNames.has(col.name)}
                  onView={() => handleOpenDrawer(col)}
                  onInstall={() => setInstallTarget(col)}
                />
              ))}
            </div>
          ) : (
            <EmptyState icon={Package} hint={t("oci.noCollectionsHint")}>
              {t("oci.noCollections")}
            </EmptyState>
          )}

          {/* The registries themselves: which ones answer, and which do not. */}
          <section className="border-t border-line pt-12 mt-10">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div>
                <h2 className="text-[22px] font-bold tracking-[-0.02em]">{t("oci.registries")}</h2>
                <p className="text-[13px] text-muted mt-1">{t("library.registriesInSettings")}</p>
              </div>
              <Button size="sm" onClick={() => setAddingRegistry(true)}>
                {t("oci.addRegistryButton")}
              </Button>
            </div>

            <div className="mt-5">
              {regsLoading ? (
                <div className="flex items-center justify-center py-12">
                  <Spinner size="lg" label={t("common.loading")} />
                </div>
              ) : registries && registries.length > 0 ? (
                <div className="space-y-3">
                  {registries.map((reg) => (
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
            </div>
          </section>
        </div>
      )}

      {activeTab === "installed" && (
        <div className="space-y-6">
          {updates && updates.length > 0 && (
            <section className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-[17px] font-semibold tracking-[-0.02em]">
                  {t("oci.availableUpdates")}
                </h3>
                <div className="flex items-center gap-2">
                  <Button size="sm" icon={RefreshCw} loading={updatesLoading} onClick={() => refetchUpdates()}>
                    {t("library.check")}
                  </Button>
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={handleApplyUpdates}
                    disabled={updates.some((u) => u.local_changes)}
                  >
                    {t("oci.applyAll")}
                  </Button>
                </div>
              </div>
              <div className="space-y-2">
                {updates.map((u) => (
                  <UpdateRow key={u.collection} update={u} />
                ))}
              </div>
            </section>
          )}

          {metaLoading ? (
            <div className="flex items-center justify-center py-16">
              <Spinner size="lg" label={t("common.loading")} />
            </div>
          ) : ociMeta && ociMeta.length > 0 ? (
            <div className="space-y-3">
              {ociMeta.map((meta) => (
                <div
                  key={meta.name}
                  data-testid={`installed-${meta.name}`}
                  className="flex flex-wrap items-center justify-between gap-3 p-3 rounded-md border border-line bg-surface"
                >
                  <div className="min-w-0">
                    <span className="font-mono font-semibold">{meta.name}</span>
                    <span className="text-muted text-[13px] ml-2 font-mono">
                      {meta.collection}@{meta.version}
                    </span>
                    <span className="text-muted text-[13px] ml-1">({meta.source})</span>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="text-[13px] text-muted">
                      {new Date(meta.installed_at).toLocaleDateString()}
                    </span>
                    {meta.local_changes && (
                      <span className="text-warn text-[13px]">{t("oci.modified")}</span>
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

      {/* Collection Drawer */}
      <SlideDrawer
        open={!!drawerCollection}
        onClose={() => setDrawerCollection(null)}
        header={
          <div className="space-y-2">
            <div className="flex items-center gap-3">
              <Package size={20} className="text-blue2" />
              <span className="text-xl font-mono font-bold">{drawerCollection?.name}</span>
              <span className="text-[13px] text-muted font-mono">v{drawerCollection?.version}</span>
            </div>
            <p className="text-[13px] text-muted">{drawerCollection?.description}</p>
          </div>
        }
        actions={
          <IconButton
            size="sm"
            icon={XCircle}
            label={t("common.close")}
            onClick={() => setDrawerCollection(null)}
            className="border-transparent text-muted hover:text-text hover:border-line"
          />
        }
      >
        <div className="space-y-6 py-4">
          <div className="px-4 space-y-2">
            {drawerRecipesLoading ? (
              <p className="flex items-center gap-2 py-4 text-[13px] text-muted">
                <Spinner size="sm" />
                {t("oci.loadingRecipes")}
              </p>
            ) : drawerRecipes.length > 0 ? (
              drawerRecipes.map((recipe) => {
                const action = recipeActions[recipe.name];
                const isInstalled = ociMeta?.some((m) => m.name === recipe.name) || false;
                return (
                  <div
                    key={recipe.name}
                    data-testid={`collection-recipe-${recipe.name}`}
                    className="p-4 rounded-md border border-line bg-surface"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-mono font-semibold text-[14px]">{recipe.name}</span>
                          <span className="text-[13px] text-muted font-mono">
                            v{recipe.recipe_version}
                          </span>
                        </div>
                        <p className="text-[13px] text-muted mt-1">{recipe.description}</p>
                        <p className="text-[13px] text-muted mt-2 font-mono">
                          {recipe.container || t("common.none")}
                          {recipe.cluster_only && ` · ${t("oci.cluster")}`}
                          {recipe.solo_only && ` · ${t("oci.solo")}`}
                        </p>
                      </div>
                      <div className="flex items-center gap-2">
                        {action === "done" ? (
                          <span className="text-[13px] text-good">{t("common.saved")}</span>
                        ) : isInstalled ? (
                          <>
                            <Button
                              size="sm"
                              icon={RefreshCw}
                              loading={action === "updating"}
                              onClick={() => handleUpdateRecipe(recipe.name, drawerCollection!)}
                            >
                              {t("oci.update")}
                            </Button>
                            <Button
                              size="sm"
                              variant="danger"
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
                            loading={action === "installing"}
                            onClick={() => handleInstallRecipe(recipe.name, drawerCollection!)}
                          >
                            {t("oci.install")}
                          </Button>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })
            ) : (
              <p className="py-4 text-center text-muted text-[14px]">
                {t("oci.noRecipesInCollection")}
              </p>
            )}
          </div>

          <div className="flex justify-end pt-4 pr-4 border-t border-line">
            <Button
              variant="primary"
              icon={Download}
              onClick={() => drawerCollection && setInstallTarget(drawerCollection)}
            >
              {t("oci.installAllRecipes")}
            </Button>
          </div>
        </div>
      </SlideDrawer>

      {/* Add a registry. It was an inline form that opened under the list and
          pushed it down the page; it is the same three fields, in the dialog
          every other "add a thing" already uses. */}
      {addingRegistry && (
        <Modal
          open
          onClose={() => setAddingRegistry(false)}
          title={t("oci.addRegistry")}
          size="md"
          actions={
            <>
              <Button size="sm" onClick={() => setAddingRegistry(false)}>
                {t("common.cancel")}
              </Button>
              <Button
                size="sm"
                variant="primary"
                onClick={handleAddRegistry}
                disabled={!newRegName.trim() || !newRegUrl.trim()}
              >
                {t("common.add")}
              </Button>
            </>
          }
        >
          <div className="space-y-4">
            <Field label={t("oci.name")}>
              {(control) => (
                <Input
                  {...control}
                  value={newRegName}
                  onChange={(e) => setNewRegName(e.target.value)}
                  placeholder={t("oci.namePlaceholder")}
                />
              )}
            </Field>
            <Field label={t("oci.url")}>
              {(control) => (
                <Input
                  {...control}
                  mono
                  value={newRegUrl}
                  onChange={(e) => setNewRegUrl(e.target.value)}
                  placeholder={t("oci.urlPlaceholder")}
                />
              )}
            </Field>
          </div>
        </Modal>
      )}

      <EditRegistryDialog
        reg={editingRegistry}
        onClose={() => setEditingRegistry(null)}
        onSave={handleSaveRegistry}
      />

      {installTarget && (
        <ConfirmModal
          open
          onClose={() => setInstallTarget(null)}
          onConfirm={async () => {
            const col = installTarget;
            setInstallTarget(null);
            await handleInstall(col);
          }}
          title={t("library.installTitle")}
          message={t("library.installConfirm", {
            name: installTarget.name,
            version: installTarget.version,
            count: installTarget.recipe_count,
          })}
          confirmLabel={t("oci.install")}
        />
      )}

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

      {alert && (
        <AlertModal open title={alert.title} message={alert.message} onClose={() => setAlert(null)} />
      )}
    </div>
  );
}

/** One collection with a newer version published, and what changes in it. */
function UpdateRow({ update }: { update: OciUpdateCheck }) {
  const { t } = useI18n();
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 p-4 rounded-md bg-surface border border-line">
      <div className="min-w-0">
        <span className="font-mono font-semibold">{update.collection}</span>
        <span className="text-muted text-[13px] ml-2 font-mono">
          {update.current_version} → {update.latest_version}
        </span>
      </div>
      <div className="flex items-center gap-2 text-[13px]">
        {update.added_recipes.length > 0 && (
          <span className="text-good">+{update.added_recipes.length}</span>
        )}
        {update.modified_recipes.length > 0 && (
          <span className="text-warn">~{update.modified_recipes.length}</span>
        )}
        {update.local_changes && <span className="text-warn">{t("oci.localChanges")}</span>}
      </div>
    </div>
  );
}
