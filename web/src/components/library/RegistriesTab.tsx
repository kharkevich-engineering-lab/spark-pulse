/** Recipe collections: what each registry offers, and the registries themselves.
 *
 * One view, not two sub-tabs. Browse and Installed used to answer the same
 * question twice and disagree — Browse offered Install on recipes Installed
 * listed — so a collection now opens a single view that says, per recipe, what
 * state it is in and offers the one thing to do about it (`CollectionView`).
 * The collections are one table, each row opening that view; the row says how
 * many of its recipes are installed, which is what tells the one in use from
 * the others at a glance.
 *
 * Uninstalling is not here. It is on the Recipes page, beside every other way
 * a recipe leaves, and a recipe a collection stopped shipping is only marked
 * here for that reason.
 *
 * The registries are a second table below, because a registry you cannot reach
 * is a collection you cannot see and the fix belongs beside the symptom. Each
 * row's actions are words — Test, Disable, Edit, Remove — not four icons
 * nobody could tell apart. The schedule it is read on is configuration, and
 * lives in Settings.
 */

import { useState, useEffect, useCallback, useMemo } from "react";
import { useI18n } from "@/lib/i18n";
import { Package, XCircle } from "lucide-react";
import {
  fetchOciCollections,
  fetchOciMeta,
  addOciRegistry,
  updateOciRegistry,
  removeOciRegistry,
  testOciRegistry,
  fetchOciRegistryVersions,
} from "@/lib/api";
import type { OciRegistry, OciRegistryUpdate, OciCollection } from "@/lib/types";
import { useQuery, type UseQueryResult } from "@/hooks/useQuery";
import {
  AlertModal,
  Button,
  ConfirmModal,
  EmptyState,
  Field,
  IconButton,
  InfoTip,
  Input,
  Modal,
  NodeState,
  Section,
  Spinner,
  Table,
  Td,
  Th,
  THead,
  Tr,
} from "@/ui";
import SlideDrawer from "@/components/SlideDrawer";
import EditRegistryDialog from "@/components/EditRegistryDialog";
import CollectionView from "@/components/library/CollectionView";

export interface RegistriesTabProps {
  registries: UseQueryResult<OciRegistry[]>;
  /** The page header's Add registry button opens the dialog this tab owns. */
  addOpen?: boolean;
  onAddOpenChange?: (open: boolean) => void;
}

export default function RegistriesTab({
  registries: registriesQuery,
  addOpen = false,
  onAddOpenChange,
}: RegistriesTabProps) {
  const { t, plural } = useI18n();

  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);
  const [openCollection, setOpenCollection] = useState<OciCollection | null>(null);
  const addingRegistry = addOpen;
  const setAddingRegistry = (open: boolean) => onAddOpenChange?.(open);
  const [editingRegistry, setEditingRegistry] = useState<OciRegistry | null>(null);
  const [newRegName, setNewRegName] = useState("");
  const [newRegUrl, setNewRegUrl] = useState("");
  const [registryVersions, setRegistryVersions] = useState<Record<string, string[]>>({});
  /** The registry the operator asked to forget, held until they confirm. */
  const [removeTarget, setRemoveTarget] = useState<OciRegistry | null>(null);

  const fetchVersionsForRegistry = useCallback(async (regName: string) => {
    try {
      const result = await fetchOciRegistryVersions(regName);
      setRegistryVersions((prev) => ({ ...prev, [regName]: result.versions }));
    } catch {
      setRegistryVersions((prev) => ({ ...prev, [regName]: [] }));
    }
  }, []);

  // Memoized so the query does not refetch on every render.
  const fetchCollections = useCallback(
    (signal?: AbortSignal) => fetchOciCollections(undefined, undefined, signal),
    [],
  );

  const { data: registries, loading: regsLoading, refetch: refetchRegs } = registriesQuery;
  const { data: collections, loading: colsLoading, refetch: refetchCols } = useQuery(fetchCollections);
  const { data: ociMeta, refetch: refetchMeta } = useQuery(fetchOciMeta);

  useEffect(() => {
    registries?.forEach((reg) => fetchVersionsForRegistry(reg.name));
  }, [registries, fetchVersionsForRegistry]);

  /** Installed recipes per collection, from the sidecars — the card's count. */
  const installedCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const m of ociMeta ?? []) counts.set(m.collection, (counts.get(m.collection) ?? 0) + 1);
    return counts;
  }, [ociMeta]);

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

  // ── Render ──────────────────────────────────────────────────────────────

  return (
    <div className="space-y-4">
      <Section flush>
        {colsLoading ? (
          <div className="flex items-center justify-center py-16">
            <Spinner size="lg" label={t("common.loading")} />
          </div>
        ) : collections && collections.length > 0 ? (
          <Table data-testid="collections-table">
            <THead>
              <Th>{t("collection.colCollection")}</Th>
              <Th>{t("collection.colVersion")}</Th>
              <Th numeric>{t("collection.colRecipes")}</Th>
              <Th numeric>{t("collection.colInstalled")}</Th>
              <Th>{t("collection.colPublisher")}</Th>
              <Th>{t("collection.colLicense")}</Th>
            </THead>
            <tbody>
              {collections.map((col) => {
                const installed = installedCounts.get(col.name) ?? 0;
                const key = `${col.registry}-${col.name}-${col.version}`;
                return (
                  <Tr
                    key={key}
                    data-testid={`collection-${col.name}`}
                    interactive
                    selected={openCollection?.name === col.name && openCollection?.registry === col.registry}
                    onClick={() => setOpenCollection(col)}
                  >
                    <Td className="max-w-[420px]">
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          setOpenCollection(col);
                        }}
                        className="font-mono font-semibold hover:text-blue2"
                      >
                        {col.name}
                      </button>
                      <p className="truncate text-[12px] text-muted" title={col.description || undefined}>
                        {col.description || t("oci.noDescription")}
                      </p>
                    </Td>
                    <Td mono muted className="whitespace-nowrap">{col.display_version}</Td>
                    <Td numeric>{col.recipe_count}</Td>
                    <Td numeric>
                      {installed > 0 ? (
                        <span data-testid="collection-installed-count" className="text-good">
                          {installed}
                        </span>
                      ) : (
                        <span className="text-muted">0</span>
                      )}
                    </Td>
                    <Td muted className="whitespace-nowrap">{col.vendor || "—"}</Td>
                    <Td muted className="whitespace-nowrap">{col.license || "—"}</Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        ) : (
          <EmptyState icon={Package} hint={t("oci.noCollectionsHint")}>
            {t("oci.noCollections")}
          </EmptyState>
        )}
      </Section>

      {/* The registries themselves: which ones answer, and which do not. */}
      <Section
        title={t("oci.registries")}
        meta={<InfoTip label={t("library.aboutRegistries")}>{t("library.registriesInSettings")}</InfoTip>}
        flush
      >
        {regsLoading ? (
          <div className="flex items-center justify-center py-12">
            <Spinner size="lg" label={t("common.loading")} />
          </div>
        ) : registries && registries.length > 0 ? (
          <Table data-testid="registries-table">
            <THead>
              <Th>{t("registry.colRegistry")}</Th>
              <Th>{t("registry.colStatus")}</Th>
              <Th>{t("registry.versions")}</Th>
              <Th srOnly>{t("models.colActions")}</Th>
            </THead>
            <tbody>
              {registries.map((reg) => {
                const versions = registryVersions[reg.name] || [];
                return (
                  <Tr key={reg.name} data-testid={`registry-${reg.name}`} muted={!reg.enabled}>
                    <Td className="max-w-[460px]">
                      <div className="flex items-center gap-2">
                        <span className="truncate font-mono font-semibold" title={reg.url}>{reg.name}</span>
                        {reg.default && (
                          <span className="rounded-full border border-line px-1.5 text-[11.5px] text-muted">
                            {t("registry.defaultTag")}
                          </span>
                        )}
                      </div>
                      {/* The default registry is named by its own URL; print it once. */}
                      {reg.url !== reg.name && (
                        <p className="truncate font-mono text-[12px] text-muted" title={reg.url}>{reg.url}</p>
                      )}
                    </Td>
                    <Td className="whitespace-nowrap">
                      <NodeState
                        state={reg.connected ? "ok" : reg.enabled ? "bad" : "unknown"}
                        label={
                          reg.connected
                            ? t("registry.connected")
                            : reg.enabled
                              ? t("registry.notConnected")
                              : t("common.disabled")
                        }
                      />
                    </Td>
                    <Td muted className="whitespace-nowrap" title={versions.join(", ") || undefined}>
                      {versions.length > 0 ? plural("registry.versionCount", versions.length) : "—"}
                    </Td>
                    <Td actions>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t("registry.testConnectionFor", { name: reg.name })}
                        onClick={() => handleTestRegistry(reg)}
                      >
                        {t("registry.test")}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t(reg.enabled ? "registry.disableFor" : "registry.enableFor", { name: reg.name })}
                        onClick={() => handleToggleRegistry(reg)}
                      >
                        {reg.enabled ? t("common.disable") : t("common.enable")}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t("registry.editFor", { name: reg.name })}
                        onClick={() => setEditingRegistry(reg)}
                      >
                        {t("registry.edit")}
                      </Button>
                      {!reg.default && (
                        <Button
                          size="sm"
                          variant="ghost"
                          aria-label={t("registry.removeFor", { name: reg.name })}
                          onClick={() => setRemoveTarget(reg)}
                        >
                          {t("library.remove")}
                        </Button>
                      )}
                    </Td>
                  </Tr>
                );
              })}
            </tbody>
          </Table>
        ) : (
          <EmptyState icon={Package}>{t("oci.noRegistries")}</EmptyState>
        )}
      </Section>

      {/* The collection view. Keyed by collection, so opening another starts
          from its own state and filter rather than the last one's. */}
      <SlideDrawer
        open={!!openCollection}
        onClose={() => setOpenCollection(null)}
        header={
          <div className="space-y-1">
            <div className="flex items-center gap-3">
              <Package size={20} className="text-blue2 shrink-0" />
              <span className="text-xl font-mono font-bold break-all">{openCollection?.name}</span>
            </div>
            {openCollection?.description && (
              <p className="text-[13px] text-muted">{openCollection.description}</p>
            )}
          </div>
        }
        actions={
          <IconButton
            size="sm"
            icon={XCircle}
            label={t("common.close")}
            onClick={() => setOpenCollection(null)}
            className="border-transparent text-muted hover:text-text hover:border-line"
          />
        }
      >
        {openCollection && (
          <CollectionView
            key={`${openCollection.registry}/${openCollection.name}`}
            collection={openCollection}
            onChanged={refetchMeta}
          />
        )}
      </SlideDrawer>

      {/* Add a registry: the same three fields, in the dialog every other
          "add a thing" already uses. */}
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
