/** The Library tab of Settings: where engines, models and recipes come from.
 *
 * Three answers to one question — *where does this control plane fetch things
 * from* — that used to be three settings panels on three different pages: the
 * engine registry at the bottom of Engines, the model sources at the top of
 * Models, and the OCI registries behind a sub-tab of Registries. An operator
 * setting up a fresh install had to find all three, and nothing on any of
 * them said the other two existed.
 *
 * The engine fields are part of `/api/settings`, so they are bound to the
 * page's one form and written by the page's one Save. The model sources are
 * their own endpoint (`PUT /api/models/sources`) but are edited the same way
 * and saved by the same button — a second Save button inside a form that
 * already has one is how half an edit gets lost.
 *
 * The registries are not form fields. Enabling one, testing it, forgetting it
 * and running the update sweep each take effect immediately, because each is
 * an action against a registry rather than a value the form holds. Their
 * failures are a line under the control that failed, not a modal.
 */

import { useCallback, useState } from "react";
import { Plus, RefreshCw, X } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import {
  addOciRegistry,
  fetchOciAutoUpdateSettings,
  fetchOciRegistries,
  refreshEngines,
  removeOciRegistry,
  runOciAutoUpdate,
  testOciRegistry,
  updateOciAutoUpdateSettings,
  updateOciRegistry,
} from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import type { ModelSource, OciRegistry, OciRegistryUpdate } from "@/lib/types";
import {
  Button,
  ConfirmModal,
  EmptyState,
  ErrorLine,
  Field,
  IconButton,
  Input,
  Select,
  Spinner,
  Textarea,
  Toggle,
} from "@/ui";
import RegistryCard from "@/components/RegistryCard";
import EditRegistryDialog from "@/components/EditRegistryDialog";
import SettingsSection from "@/components/SettingsSection";

/** The `/api/settings` keys this tab edits. Held by the page, so the page's
 *  Save writes them with everything else. */
export interface EngineRegistryForm {
  default_engine?: string;
  engine_indexes?: string[];
  engine_index_cache_ttl_seconds?: number;
}

export interface SettingsLibraryProps {
  form: Record<string, unknown>;
  setForm: (next: Record<string, unknown>) => void;
  /** The model sources as they are being edited. `null` until they load. */
  sources: ModelSource[] | null;
  setSources: (next: ModelSource[]) => void;
}

const message = (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback);

export default function SettingsLibrary({ form, setForm, sources, setSources }: SettingsLibraryProps) {
  const { t } = useI18n();
  const engines = form as EngineRegistryForm;

  const [refreshing, setRefreshing] = useState(false);
  const [engineError, setEngineError] = useState<string | null>(null);

  const { data: registries, loading: regsLoading, refetch: refetchRegs } = useQuery(fetchOciRegistries);
  const { data: auto, loading: autoLoading, refetch: refetchAuto } = useQuery(fetchOciAutoUpdateSettings);

  const [registryError, setRegistryError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState("");
  const [newUrl, setNewUrl] = useState("");
  const [editing, setEditing] = useState<OciRegistry | null>(null);
  const [removeTarget, setRemoveTarget] = useState<OciRegistry | null>(null);

  /** The schedule as it is being typed. Saved on blur, not per keystroke: the
   *  original handler PUT the settings on every character, so a cron
   *  expression was saved once per prefix and every invalid one along the way
   *  was briefly the schedule. */
  const [scheduleDraft, setScheduleDraft] = useState<string | null>(null);
  const [autoError, setAutoError] = useState<string | null>(null);
  const [autoRunning, setAutoRunning] = useState(false);
  const [autoResult, setAutoResult] = useState<string | null>(null);

  const refreshIndex = async () => {
    setRefreshing(true);
    setEngineError(null);
    try {
      await refreshEngines();
    } catch (e) {
      setEngineError(message(e, t("engines.saveFailed")));
    } finally {
      setRefreshing(false);
    }
  };

  // ── Model sources ────────────────────────────────────────────────────────

  const draft = sources ?? [];
  const patchSource = (i: number, patch: Partial<ModelSource>) =>
    setSources(draft.map((s, idx) => (idx === i ? { ...s, ...patch } : s)));

  // ── Registries ───────────────────────────────────────────────────────────

  const toggleRegistry = async (reg: OciRegistry) => {
    setRegistryError(null);
    try {
      await updateOciRegistry(reg.name, { enabled: !reg.enabled });
      refetchRegs();
    } catch (e) {
      setRegistryError(message(e, t("common.unknownError")));
    }
  };

  const testRegistry = async (reg: OciRegistry) => {
    setRegistryError(null);
    try {
      const result = await testOciRegistry(reg.name);
      if (!result.ok) setRegistryError(t("settingsPage.registryUnreachable", { name: reg.name }));
      refetchRegs();
    } catch (e) {
      setRegistryError(message(e, t("common.unknownError")));
    }
  };

  const saveRegistry = useCallback(async (name: string, update: OciRegistryUpdate) => {
    await updateOciRegistry(name, update);
    // A changed URL is worth re-validating now rather than leaving the card
    // marked unreachable until somebody presses test.
    if (update.url !== undefined) {
      try {
        await testOciRegistry(name);
      } catch {
        // best effort; the refetch below shows whatever state landed
      }
    }
    refetchRegs();
  }, [refetchRegs]);

  const removeRegistry = async (reg: OciRegistry) => {
    setRegistryError(null);
    try {
      await removeOciRegistry(reg.name);
      refetchRegs();
    } catch (e) {
      setRegistryError(message(e, t("common.unknownError")));
    }
  };

  const addRegistry = async () => {
    if (!newName.trim() || !newUrl.trim()) return;
    setRegistryError(null);
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
      setAdding(false);
      refetchRegs();
    } catch (e) {
      setRegistryError(message(e, t("common.unknownError")));
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
      else if (result.success) setAutoResult(t("settingsPage.autoUpdateDone", { count: result.updated ?? 0 }));
      else setAutoError(result.error || t("common.unknownError"));
      refetchAuto();
    } catch (e) {
      setAutoError(message(e, t("common.unknownError")));
    } finally {
      setAutoRunning(false);
    }
  };

  return (
    <div>
      {/* ── Engines ──────────────────────────────────────────────────────── */}
      <SettingsSection
        first
        title={t("engines.registry")}
        hint={t("settingsPage.enginesHint")}
        actions={
          <Button size="sm" icon={RefreshCw} loading={refreshing} onClick={refreshIndex}>
            {t("engines.refreshIndex")}
          </Button>
        }
      >
        <Field label={t("engines.defaultEngine")} hint={t("settingsPage.defaultEngineHint")}>
          {(control) => (
            <Input
              {...control}
              mono
              type="text"
              value={engines.default_engine ?? ""}
              onChange={(e) => setForm({ ...form, default_engine: e.target.value })}
              placeholder="vllm"
            />
          )}
        </Field>

        <Field label={t("engines.indexes")} hint={t("engines.indexesHelp")} error={engineError}>
          {(control) => (
            <Textarea
              {...control}
              mono
              rows={3}
              value={(engines.engine_indexes ?? []).join("\n")}
              onChange={(e) =>
                setForm({
                  ...form,
                  engine_indexes: e.target.value.split("\n").map((l) => l.trim()).filter(Boolean),
                })
              }
            />
          )}
        </Field>

        <Field label={t("engines.indexTtl")} hint={t("settingsPage.indexTtlHint")}>
          {(control) => (
            <div className="flex items-center gap-2">
              {/* The width is on the wrapper, not the control: `Input` is
                  `w-full`, and `cn` is clsx rather than tailwind-merge, so a
                  `w-28` passed to it loses to the base class. */}
              <div className="w-28 shrink-0">
                <Input
                  {...control}
                  mono
                  type="number"
                  min="0"
                  value={engines.engine_index_cache_ttl_seconds ?? 3600}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      engine_index_cache_ttl_seconds: parseInt(e.target.value) || 0,
                    })
                  }
                />
              </div>
              <span className="shrink-0 text-[14px] text-muted">{t("common.seconds")}</span>
            </div>
          )}
        </Field>
      </SettingsSection>

      {/* ── Model sources ────────────────────────────────────────────────── */}
      <SettingsSection
        title={t("models.sources")}
        hint={t("settingsPage.sourcesHint")}
        actions={
          <Button
            size="sm"
            icon={Plus}
            onClick={() =>
              setSources([
                ...draft,
                { name: "", type: "hf_hub", endpoint: "https://huggingface.co", token_secret: "" },
              ])
            }
          >
            {t("models.addSource")}
          </Button>
        }
      >
        {draft.length === 0 && <p className="text-[14px] text-muted">{t("models.noSources")}</p>}
        <div className="space-y-3">
          {draft.map((s, i) => (
            <div
              key={i}
              className="grid grid-cols-1 items-center gap-2 md:grid-cols-[1fr_140px_1fr_1fr_auto]"
            >
              <Input
                aria-label={t("models.sourceName", { n: i + 1 })}
                value={s.name}
                onChange={(e) => patchSource(i, { name: e.target.value })}
                placeholder={t("models.namePlaceholder")}
              />
              <Select
                aria-label={t("models.sourceType", { n: i + 1 })}
                value={s.type}
                onChange={(e) => patchSource(i, { type: e.target.value as ModelSource["type"] })}
              >
                <option value="hf_hub">hf_hub</option>
                <option value="local_path">local_path</option>
              </Select>
              {s.type === "hf_hub" ? (
                <>
                  <Input
                    mono
                    aria-label={t("models.sourceEndpoint", { n: i + 1 })}
                    value={s.endpoint ?? ""}
                    onChange={(e) => patchSource(i, { endpoint: e.target.value })}
                    placeholder="https://huggingface.co"
                  />
                  <Input
                    mono
                    aria-label={t("models.sourceToken", { n: i + 1 })}
                    value={s.token_secret ?? ""}
                    onChange={(e) => patchSource(i, { token_secret: e.target.value })}
                    placeholder={t("models.tokenPlaceholder")}
                  />
                </>
              ) : (
                <Input
                  mono
                  aria-label={t("models.sourcePath", { n: i + 1 })}
                  value={s.path ?? ""}
                  onChange={(e) => patchSource(i, { path: e.target.value })}
                  placeholder="/models"
                  className="md:col-span-2"
                />
              )}
              <IconButton
                size="sm"
                icon={X}
                label={t("settingsPage.removeSource", { n: String(i + 1) })}
                onClick={() => setSources(draft.filter((_, idx) => idx !== i))}
                className="border-transparent text-muted hover:border-line hover:text-bad"
              />
            </div>
          ))}
        </div>
      </SettingsSection>

      {/* ── Recipe registries ────────────────────────────────────────────── */}
      <SettingsSection
        title={t("oci.registries")}
        hint={t("settingsPage.registriesHint")}
        actions={
          <Button size="sm" icon={Plus} onClick={() => setAdding(true)}>
            {t("oci.addRegistryButton")}
          </Button>
        }
      >
        {regsLoading ? (
          <div className="flex justify-center py-8">
            <Spinner label={t("common.loading")} />
          </div>
        ) : registries && registries.length > 0 ? (
          <div className="space-y-3">
            {registries.map((reg) => (
              <RegistryCard
                key={reg.name}
                reg={reg}
                onToggle={() => toggleRegistry(reg)}
                onTest={() => testRegistry(reg)}
                onRemove={() => setRemoveTarget(reg)}
                onEdit={() => setEditing(reg)}
              />
            ))}
          </div>
        ) : (
          <EmptyState>{t("oci.noRegistries")}</EmptyState>
        )}

        <ErrorLine>{registryError}</ErrorLine>

        {adding && (
          <div className="space-y-4 rounded-md border border-line bg-bg2 p-5">
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
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="primary"
                onClick={addRegistry}
                disabled={!newName.trim() || !newUrl.trim()}
              >
                {t("common.add")}
              </Button>
              <Button
                size="sm"
                onClick={() => {
                  setAdding(false);
                  setNewName("");
                  setNewUrl("");
                }}
              >
                {t("common.cancel")}
              </Button>
            </div>
          </div>
        )}
      </SettingsSection>

      {/* ── Auto-update ──────────────────────────────────────────────────── */}
      <SettingsSection title={t("oci.autoUpdate")} hint={t("settingsPage.autoUpdateHint")}>
        {autoLoading ? (
          <div className="flex justify-center py-8">
            <Spinner label={t("common.loading")} />
          </div>
        ) : auto ? (
          <>
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <p className="text-[14px] font-medium">{t("oci.enableAutoUpdate")}</p>
                <p className="mt-1 text-[13px] leading-snug text-muted">
                  {t("settingsPage.autoUpdateToggleHint")}
                </p>
              </div>
              <Toggle on={auto.enabled} onChange={toggleAuto} label={t("oci.enableAutoUpdate")} />
            </div>

            <div className="flex flex-col gap-3 min-[520px]:flex-row min-[520px]:items-end min-[520px]:gap-4">
              <Field
                label={t("oci.schedule")}
                hint={t("settingsPage.scheduleHint")}
                className="min-[520px]:flex-1"
              >
                {(control) => (
                  <Input
                    {...control}
                    mono
                    type="text"
                    value={scheduleDraft ?? auto.schedule}
                    onChange={(e) => setScheduleDraft(e.target.value)}
                    onBlur={() => saveSchedule(auto.schedule)}
                  />
                )}
              </Field>
              <Button
                variant="primary"
                loading={autoRunning}
                onClick={runNow}
                className="max-[519px]:w-full"
              >
                {t("settingsPage.runNow")}
              </Button>
            </div>

            {autoResult && <p className="text-[13px] text-good">{autoResult}</p>}
            <ErrorLine>{autoError}</ErrorLine>
          </>
        ) : (
          <ErrorLine>{autoError}</ErrorLine>
        )}
      </SettingsSection>

      <EditRegistryDialog reg={editing} onClose={() => setEditing(null)} onSave={saveRegistry} />

      {removeTarget && (
        <ConfirmModal
          open
          title={t("settingsPage.forgetRegistry")}
          message={t("settingsPage.forgetRegistryBody", { name: removeTarget.name })}
          confirmLabel={t("common.delete")}
          confirmVariant="danger"
          onConfirm={() => {
            const reg = removeTarget;
            setRemoveTarget(null);
            removeRegistry(reg);
          }}
          onClose={() => setRemoveTarget(null)}
        />
      )}
    </div>
  );
}
