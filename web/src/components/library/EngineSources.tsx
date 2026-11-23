/** Where engines come from: the indexes, the default, how long a fetch is kept.
 *
 * What Settings → Library used to hold for engines. Every engine on the
 * catalogue next door comes from one of these indexes, so the list says what
 * each one last published and whether it is being read at all. Edits are held
 * here and written by this section's own save bar: only the three keys it
 * owns, which the server merges into settings.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { RefreshCw, X } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { fetchEngines, fetchSettings, refreshEngines, updateSettings } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { Button, ErrorLine, IconButton, Input, Select, Spinner } from "@/ui";
import SettingsSection, { SettingRow } from "@/components/SettingsSection";
import SourcesSaveBar from "./SourcesSaveBar";

interface EngineSourcesForm {
  default_engine: string;
  engine_indexes: string[];
  engine_index_cache_ttl_seconds: number;
}

const message = (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback);

export interface EngineSourcesProps {
  /** The header's *Add index* bumps this; each change appends an empty line. */
  addTick?: number;
}

export default function EngineSources({ addTick = 0 }: EngineSourcesProps) {
  const { t } = useI18n();
  const { data: registry, refetch: refetchEngines } = useQuery(fetchEngines);
  const { data: settings, refetch: refetchSettings } = useQuery(fetchSettings);

  const saved = useMemo<EngineSourcesForm | null>(
    () =>
      settings
        ? {
            default_engine: settings.default_engine ?? "",
            engine_indexes: settings.engine_indexes ?? [],
            engine_index_cache_ttl_seconds: settings.engine_index_cache_ttl_seconds ?? 3600,
          }
        : null,
    [settings],
  );
  const [draft, setDraft] = useState<EngineSourcesForm | null>(null);
  const form = draft ?? saved;

  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const indexesFromEnv = registry?.indexes_env_managed === true;
  const defaultIndexes = registry?.default_indexes ?? [];

  const edit = (patch: Partial<EngineSourcesForm>) => {
    if (form) setDraft({ ...form, ...patch });
  };

  // The header's button: a new empty line, once per press.
  const seenTick = useRef(addTick);
  useEffect(() => {
    if (addTick === seenTick.current) return;
    seenTick.current = addTick;
    if (!form || indexesFromEnv) return;
    setDraft({ ...form, engine_indexes: [...form.engine_indexes, ""] });
    // The form is read at the moment of the press, not a dependency.
  }, [addTick]);

  const trimmed = (f: EngineSourcesForm): EngineSourcesForm => ({
    ...f,
    engine_indexes: f.engine_indexes.map((l) => l.trim()).filter(Boolean),
  });
  const dirty = useMemo(
    () => !!draft && !!saved && JSON.stringify(trimmed(draft)) !== JSON.stringify(saved),
    [draft, saved],
  );

  // Every engine the indexes publish, plus whatever is set now, so a value
  // nothing publishes yet stays visible instead of reading as the first option.
  const engineNames = useMemo(() => {
    const names = new Set((registry?.engines ?? []).map((e) => e.engine));
    if (form?.default_engine) names.add(form.default_engine);
    return [...names].sort();
  }, [registry, form?.default_engine]);

  const statusOf = (ref: string) => (registry?.indexes ?? []).find((i) => i.ref === ref);

  const refresh = async () => {
    setRefreshing(true);
    setRefreshError(null);
    try {
      await refreshEngines();
    } catch (e) {
      setRefreshError(message(e, t("engines.saveFailed")));
    } finally {
      setRefreshing(false);
      refetchEngines();
    }
  };

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    setSaveError(null);
    try {
      await updateSettings(trimmed(draft));
      setDraft(null);
      await Promise.all([refetchSettings(), refetchEngines()]);
    } catch (e) {
      setSaveError(message(e, t("engines.saveFailed")));
    } finally {
      setSaving(false);
    }
  };

  if (!form) {
    return (
      <div className="flex justify-center py-16">
        <Spinner size="lg" label={t("common.loading")} />
      </div>
    );
  }

  const isDefault =
    defaultIndexes.length > 0 &&
    form.engine_indexes.length === defaultIndexes.length &&
    form.engine_indexes.every((ref, i) => ref === defaultIndexes[i]);

  return (
    <div data-testid="engine-sources">
      <SettingsSection
        first
        title={t("engines.indexes")}
        hint={t("engines.indexesHelp")}
        actions={
          <Button size="sm" icon={RefreshCw} loading={refreshing} onClick={refresh}>
            {t("engines.refreshIndex")}
          </Button>
        }
      >
        <div className="space-y-3">
          {form.engine_indexes.length === 0 && (
            <p className="text-[14px] text-muted">{t("engines.noIndexes")}</p>
          )}
          {form.engine_indexes.map((ref, i) => {
            const status = statusOf(ref.trim());
            return (
              <div key={i} data-testid="index-row" className="space-y-1">
                <div className="flex items-center gap-2">
                  <Input
                    mono
                    aria-label={t("engines.indexLabel", { n: i + 1 })}
                    disabled={indexesFromEnv}
                    value={ref}
                    onChange={(e) =>
                      edit({
                        engine_indexes: form.engine_indexes.map((r, idx) => (idx === i ? e.target.value : r)),
                      })
                    }
                  />
                  <IconButton
                    size="sm"
                    icon={X}
                    label={t("engines.removeIndex", { n: i + 1 })}
                    disabled={indexesFromEnv}
                    onClick={() => edit({ engine_indexes: form.engine_indexes.filter((_, idx) => idx !== i) })}
                    className="border-transparent text-muted hover:border-line hover:text-bad"
                  />
                </div>
                {status && (
                  <p className="text-[13px] text-muted">
                    {status.fetched_at
                      ? t("settingsPage.indexFetched", {
                          count: status.engines,
                          time: new Date(status.fetched_at * 1000).toLocaleString(),
                        })
                      : t("settingsPage.indexNeverFetched")}
                    {status.error && <ErrorLine>{status.error}</ErrorLine>}
                  </p>
                )}
              </div>
            );
          })}
          {indexesFromEnv && (
            <p className="text-[13px] text-muted" data-testid="indexes-from-env">
              {t("settingsPage.indexesFromEnv")}
            </p>
          )}
          {!indexesFromEnv && defaultIndexes.length > 0 && !isDefault && (
            <Button size="sm" variant="ghost" onClick={() => edit({ engine_indexes: [...defaultIndexes] })}>
              {t("settingsPage.useDefaultIndex")}
            </Button>
          )}
          <ErrorLine>{refreshError}</ErrorLine>
        </div>

        <SettingRow label={t("engines.defaultEngine")} hint={t("settingsPage.defaultEngineHint")}>
          {(control) =>
            engineNames.length > 0 ? (
              <Select
                {...control}
                className="font-mono text-sm"
                value={form.default_engine}
                onChange={(e) => edit({ default_engine: e.target.value })}
              >
                {engineNames.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </Select>
            ) : (
              <Input
                {...control}
                mono
                value={form.default_engine}
                onChange={(e) => edit({ default_engine: e.target.value })}
                placeholder="vllm"
              />
            )
          }
        </SettingRow>

        <SettingRow label={t("engines.indexTtl")} hint={t("settingsPage.indexTtlHint")}>
          {(control) => (
            <div className="flex items-center gap-2">
              <div className="w-28 shrink-0">
                <Input
                  {...control}
                  mono
                  type="number"
                  min="0"
                  value={form.engine_index_cache_ttl_seconds}
                  onChange={(e) => edit({ engine_index_cache_ttl_seconds: parseInt(e.target.value) || 0 })}
                />
              </div>
              <span className="shrink-0 text-[14px] text-muted">{t("common.seconds")}</span>
            </div>
          )}
        </SettingRow>
      </SettingsSection>

      <SourcesSaveBar
        dirty={dirty}
        saving={saving}
        error={saveError}
        onSave={save}
        onDiscard={() => {
          setDraft(null);
          setSaveError(null);
        }}
      />
    </div>
  );
}
