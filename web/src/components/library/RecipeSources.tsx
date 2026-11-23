/** Where recipes come from: the package, the operator's own directory, and
 * every OCI registry that publishes collections.
 *
 * One table, one row per source. A registry row folds in what used to be a
 * second table of collections — which collection it carries, at what version,
 * how many recipes it offers and how many of them are installed — because a
 * registry nobody can reach is a collection nobody can see, and the fix
 * belongs beside the symptom. Bundled and custom are read-only rows: nothing
 * to enable, test or remove, only where the files are.
 *
 * Under the table, the schedule on which collections are checked for newer
 * versions. It never applies an update to a recipe edited here.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Package } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import {
  addOciRegistry,
  fetchOciAutoUpdateSettings,
  fetchOciCollections,
  fetchOciMeta,
  fetchOciRegistryVersions,
  removeOciRegistry,
  runOciAutoUpdate,
  testOciRegistry,
  updateOciAutoUpdateSettings,
  updateOciRegistry,
} from "@/lib/api";
import { useQuery, type UseQueryResult } from "@/hooks/useQuery";
import type { OciRegistry, OciRegistryUpdate, RecipeSummary } from "@/lib/types";
import {
  AlertModal,
  Button,
  ConfirmModal,
  EmptyState,
  ErrorLine,
  Field,
  Input,
  Modal,
  NodeState,
  Section,
  Spinner,
  Table,
  Td,
  Th,
  THead,
  Toggle,
  Tr,
} from "@/ui";
import EditRegistryDialog from "@/components/EditRegistryDialog";
import SettingsSection, { SettingRow } from "@/components/SettingsSection";

export interface RecipeSourcesProps {
  /** The registries, read by the page so the Sources tab can count them. */
  registries: UseQueryResult<OciRegistry[]>;
  /** The installed recipes, for the counts of the two read-only sources. */
  recipes: RecipeSummary[];
  /** The page header's Add source button opens the dialog this owns. */
  addOpen: boolean;
  onAddOpenChange: (open: boolean) => void;
  /** A registry was added, changed or removed: what the catalogue offers is stale. */
  onChanged?: () => void;
}

const message = (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback);

export default function RecipeSources({ registries: registriesQuery, recipes, addOpen, onAddOpenChange, onChanged }: RecipeSourcesProps) {
  const { t, plural } = useI18n();

  const { data: registries, loading, refetch: refetchRegs } = registriesQuery;
  const fetchCollections = useCallback(
    (signal?: AbortSignal) => fetchOciCollections(undefined, undefined, signal),
    [],
  );
  const { data: collections, refetch: refetchCols } = useQuery(fetchCollections);
  const { data: meta } = useQuery(fetchOciMeta);
  const { data: auto, loading: autoLoading, refetch: refetchAuto } = useQuery(fetchOciAutoUpdateSettings);

  const [versions, setVersions] = useState<Record<string, string[]>>({});
  const [alert, setAlert] = useState<{ title: string; message: string } | null>(null);
  const [editing, setEditing] = useState<OciRegistry | null>(null);
  const [removeTarget, setRemoveTarget] = useState<OciRegistry | null>(null);
  const [newName, setNewName] = useState("");
  const [newUrl, setNewUrl] = useState("");

  const [scheduleDraft, setScheduleDraft] = useState<string | null>(null);
  const [autoError, setAutoError] = useState<string | null>(null);
  const [autoRunning, setAutoRunning] = useState(false);
  const [autoResult, setAutoResult] = useState<string | null>(null);

  useEffect(() => {
    registries?.forEach((reg) => {
      fetchOciRegistryVersions(reg.name)
        .then((r) => setVersions((prev) => ({ ...prev, [reg.name]: r.versions })))
        .catch(() => setVersions((prev) => ({ ...prev, [reg.name]: [] })));
    });
  }, [registries]);

  const installedBy = useMemo(() => {
    const counts = new Map<string, number>();
    for (const m of meta ?? []) counts.set(m.source, (counts.get(m.source) ?? 0) + 1);
    return counts;
  }, [meta]);

  const bundled = recipes.filter((r) => r.source === "bundled" || r.id.startsWith("bundled/")).length;
  const custom = recipes.filter((r) => r.source === "custom" || r.id.startsWith("custom-")).length;

  const failed = (title: string, e: unknown) =>
    setAlert({ title, message: message(e, t("common.unknownError")) });
  const changed = () => {
    refetchRegs();
    refetchCols();
    onChanged?.();
  };

  const toggle = async (reg: OciRegistry) => {
    try {
      await updateOciRegistry(reg.name, { enabled: !reg.enabled });
      changed();
    } catch (e) {
      failed(t("library.registryUpdateFailed"), e);
    }
  };

  const test = async (reg: OciRegistry) => {
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

  const save = async (name: string, update: OciRegistryUpdate) => {
    await updateOciRegistry(name, update);
    // A changed URL is worth re-validating now rather than leaving the row
    // marked unreachable until somebody presses Test.
    if (update.url !== undefined) {
      try {
        await testOciRegistry(name);
      } catch {
        // best effort; the refetch below shows whatever state landed
      }
    }
    changed();
  };

  const remove = async (reg: OciRegistry) => {
    try {
      await removeOciRegistry(reg.name);
      changed();
    } catch (e) {
      failed(t("library.registryRemoveFailed"), e);
    }
  };

  const add = async () => {
    if (!newName.trim() || !newUrl.trim()) return;
    try {
      await addOciRegistry({
        name: newName.trim(),
        url: newUrl.trim(),
        enabled: true,
        default: false,
        auth_type: "none",
      });
      setNewName("");
      setNewUrl("");
      onAddOpenChange(false);
      changed();
    } catch (e) {
      failed(t("library.registryAddFailed"), e);
    }
  };

  // ── Auto-update ──────────────────────────────────────────────────────────

  const toggleAuto = async (enabled: boolean) => {
    setAutoError(null);
    try {
      await updateOciAutoUpdateSettings({ enabled });
      refetchAuto();
    } catch (e) {
      setAutoError(message(e, t("common.unknownError")));
    }
  };

  /** Saved when the field loses focus, not per keystroke: every prefix of a
   *  cron expression would otherwise be briefly the schedule. */
  const saveSchedule = async (current: string) => {
    if (scheduleDraft === null || scheduleDraft === current) {
      setScheduleDraft(null);
      return;
    }
    setAutoError(null);
    try {
      await updateOciAutoUpdateSettings({ schedule: scheduleDraft });
      setScheduleDraft(null);
      refetchAuto();
    } catch (e) {
      setAutoError(message(e, t("common.unknownError")));
    }
  };

  const runNow = async () => {
    setAutoRunning(true);
    setAutoError(null);
    setAutoResult(null);
    try {
      const result = await runOciAutoUpdate();
      if (result.skipped) setAutoResult(result.reason || t("settingsPage.autoUpdateSkipped"));
      else if (result.success) {
        setAutoResult(t("settingsPage.autoUpdateDone", { count: result.updated ?? 0 }));
        onChanged?.();
      } else setAutoError(result.error || t("common.unknownError"));
      refetchAuto();
    } catch (e) {
      setAutoError(message(e, t("common.unknownError")));
    } finally {
      setAutoRunning(false);
    }
  };

  return (
    <div className="space-y-4">
      <Section flush>
        {loading ? (
          <div className="flex items-center justify-center py-12">
            <Spinner size="lg" label={t("common.loading")} />
          </div>
        ) : (
          <Table data-testid="recipe-sources-table">
            <THead>
              <Th>{t("recipeSources.colSource")}</Th>
              <Th>{t("registry.colStatus")}</Th>
              <Th>{t("collection.colCollection")}</Th>
              <Th>{t("registry.versions")}</Th>
              <Th numeric>{t("collection.colRecipes")}</Th>
              <Th numeric>{t("collection.colInstalled")}</Th>
              <Th srOnly>{t("models.colActions")}</Th>
            </THead>
            <tbody>
              <Tr data-testid="source-bundled">
                <Td className="max-w-[420px]">
                  <span className="font-mono font-semibold">{t("recipes.sourceBundled")}</span>
                  <p className="text-[12px] text-muted">{t("recipeSources.bundledWhere")}</p>
                </Td>
                <Td muted>—</Td>
                <Td muted>—</Td>
                <Td muted>—</Td>
                <Td numeric>{bundled}</Td>
                <Td numeric>{bundled}</Td>
                <Td actions />
              </Tr>
              <Tr data-testid="source-custom">
                <Td className="max-w-[420px]">
                  <span className="font-mono font-semibold">{t("recipes.sourceCustom")}</span>
                  <p className="text-[12px] text-muted">{t("recipeSources.customWhere")}</p>
                </Td>
                <Td muted>—</Td>
                <Td muted>—</Td>
                <Td muted>—</Td>
                <Td numeric>{custom}</Td>
                <Td numeric>{custom}</Td>
                <Td actions />
              </Tr>
              {(registries ?? []).map((reg) => {
                const own = (collections ?? []).filter((c) => c.registry === reg.name);
                const held = versions[reg.name] ?? [];
                const installed = installedBy.get(reg.name) ?? 0;
                return (
                  <Tr key={reg.name} data-testid={`registry-${reg.name}`} muted={!reg.enabled}>
                    <Td className="max-w-[420px]">
                      <div className="flex items-center gap-2">
                        <span className="truncate font-mono font-semibold" title={reg.url}>
                          {reg.name}
                        </span>
                        {reg.default && (
                          <span className="rounded-full border border-line px-1.5 text-[11.5px] text-muted">
                            {t("registry.defaultTag")}
                          </span>
                        )}
                      </div>
                      {/* The default registry is named by its own URL; print it once. */}
                      {reg.url !== reg.name && (
                        <p className="truncate font-mono text-[12px] text-muted" title={reg.url}>
                          {reg.url}
                        </p>
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
                    <Td>
                      {own.length === 0 ? (
                        <span className="text-muted">—</span>
                      ) : (
                        own.map((c) => (
                          <p key={`${c.name}-${c.version}`} className="whitespace-nowrap">
                            <span className="font-mono">{c.name}</span>{" "}
                            <span className="font-mono text-muted">{c.display_version}</span>
                          </p>
                        ))
                      )}
                    </Td>
                    <Td muted className="whitespace-nowrap" title={held.join(", ") || undefined}>
                      {held.length > 0 ? plural("registry.versionCount", held.length) : "—"}
                    </Td>
                    <Td numeric>{own.reduce((n, c) => n + c.recipe_count, 0)}</Td>
                    <Td numeric>
                      {installed > 0 ? <span className="text-good">{installed}</span> : <span className="text-muted">0</span>}
                    </Td>
                    <Td actions>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t("registry.testConnectionFor", { name: reg.name })}
                        onClick={() => test(reg)}
                      >
                        {t("registry.test")}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t(reg.enabled ? "registry.disableFor" : "registry.enableFor", { name: reg.name })}
                        onClick={() => toggle(reg)}
                      >
                        {reg.enabled ? t("common.disable") : t("common.enable")}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t("registry.editFor", { name: reg.name })}
                        onClick={() => setEditing(reg)}
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
        )}
        {!loading && (registries ?? []).length === 0 && (
          <EmptyState icon={Package}>{t("oci.noRegistries")}</EmptyState>
        )}
      </Section>

      <SettingsSection first title={t("oci.autoUpdate")} hint={t("settingsPage.autoUpdateHint")}>
        {autoLoading ? (
          <div className="flex justify-center py-8">
            <Spinner label={t("common.loading")} />
          </div>
        ) : auto ? (
          <>
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <p className="text-[13.5px] font-medium">{t("oci.enableAutoUpdate")}</p>
                <p className="mt-0.5 text-[12.5px] leading-snug text-muted">
                  {t("settingsPage.autoUpdateToggleHint")}
                </p>
              </div>
              <Toggle on={auto.enabled} onChange={toggleAuto} label={t("oci.enableAutoUpdate")} />
            </div>
            <SettingRow label={t("oci.schedule")} hint={t("settingsPage.scheduleHint")}>
              {(control) => (
                <div className="flex flex-wrap items-center gap-2">
                  <div className="min-w-[160px] flex-1">
                    <Input
                      {...control}
                      mono
                      type="text"
                      value={scheduleDraft ?? auto.schedule}
                      onChange={(e) => setScheduleDraft(e.target.value)}
                      onBlur={() => saveSchedule(auto.schedule)}
                    />
                  </div>
                  <Button loading={autoRunning} onClick={runNow}>
                    {t("settingsPage.runNow")}
                  </Button>
                </div>
              )}
            </SettingRow>
            {autoResult && <p className="text-[13px] text-good">{autoResult}</p>}
            <ErrorLine>{autoError}</ErrorLine>
          </>
        ) : (
          <ErrorLine>{autoError}</ErrorLine>
        )}
      </SettingsSection>

      {addOpen && (
        <Modal
          open
          onClose={() => onAddOpenChange(false)}
          title={t("oci.addRegistry")}
          size="md"
          actions={
            <>
              <Button size="sm" onClick={() => onAddOpenChange(false)}>
                {t("common.cancel")}
              </Button>
              <Button size="sm" variant="primary" onClick={add} disabled={!newName.trim() || !newUrl.trim()}>
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
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder={t("oci.namePlaceholder")}
                />
              )}
            </Field>
            <Field label={t("oci.url")}>
              {(control) => (
                <Input
                  {...control}
                  mono
                  value={newUrl}
                  onChange={(e) => setNewUrl(e.target.value)}
                  placeholder={t("oci.urlPlaceholder")}
                />
              )}
            </Field>
          </div>
        </Modal>
      )}

      <EditRegistryDialog reg={editing} onClose={() => setEditing(null)} onSave={save} />

      {removeTarget && (
        <ConfirmModal
          open
          onClose={() => setRemoveTarget(null)}
          onConfirm={async () => {
            const reg = removeTarget;
            setRemoveTarget(null);
            await remove(reg);
          }}
          title={t("oci.removeRegistry")}
          message={t("oci.removeRegistryConfirm", { name: removeTarget.name })}
          confirmLabel={t("common.delete")}
          confirmVariant="danger"
        />
      )}

      {alert && <AlertModal open title={alert.title} message={alert.message} onClose={() => setAlert(null)} />}
    </div>
  );
}
