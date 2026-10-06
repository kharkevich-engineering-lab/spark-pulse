/** Recipe collections: what each registry offers, and the registries themselves.
 *
 * One view, not two sub-tabs. Browse and Installed used to answer the same
 * question twice and disagree — Browse offered Install on recipes Installed
 * listed — so a collection now opens a single view that says, per recipe, what
 * state it is in and offers the one thing to do about it (`CollectionView`).
 * Several collections are a grid of cards, each opening that view; the card
 * says how many of its recipes are installed, which is what tells the one in
 * use from the others at a glance.
 *
 * Uninstalling is not here. It is on the Recipes page, beside every other way
 * a recipe leaves, and a recipe a collection stopped shipping is only marked
 * here for that reason.
 *
 * The registry list stays below, because a registry you cannot reach is a
 * collection you cannot see and the fix belongs beside the symptom. The
 * schedule it is read on is configuration, and lives in Settings.
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
  Input,
  Modal,
  Spinner,
} from "@/ui";
import SlideDrawer from "@/components/SlideDrawer";
import RegistryCard from "@/components/RegistryCard";
import EditRegistryDialog from "@/components/EditRegistryDialog";
import CollectionCard from "@/components/CollectionCard";
import CollectionView from "@/components/library/CollectionView";

export interface RegistriesTabProps {
  registries: UseQueryResult<OciRegistry[]>;
}

export default function RegistriesTab({ registries: registriesQuery }: RegistriesTabProps) {
  const { t } = useI18n();

  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);
  const [openCollection, setOpenCollection] = useState<OciCollection | null>(null);
  const [addingRegistry, setAddingRegistry] = useState(false);
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
    <div className="space-y-6">
      <section className="space-y-5">
        <h2 className="text-[22px] font-bold tracking-[-0.02em]">{t("collection.heading")}</h2>
        {colsLoading ? (
          <div className="flex items-center justify-center py-16">
            <Spinner size="lg" label={t("common.loading")} />
          </div>
        ) : collections && collections.length > 0 ? (
          <div className="grid grid-cols-1 gap-6 min-[600px]:grid-cols-2 min-[1000px]:grid-cols-3">
            {collections.map((col) => (
              <CollectionCard
                key={`${col.registry}-${col.name}-${col.version}`}
                collection={col}
                installedCount={installedCounts.get(col.name) ?? 0}
                onView={() => setOpenCollection(col)}
              />
            ))}
          </div>
        ) : (
          <EmptyState icon={Package} hint={t("oci.noCollectionsHint")}>
            {t("oci.noCollections")}
          </EmptyState>
        )}
      </section>

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
