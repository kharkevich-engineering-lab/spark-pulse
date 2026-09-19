import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  deleteSecret,
  fetchModelSources,
  fetchSecrets,
  fetchSettings,
  saveModelSources,
  saveSecrets,
  updateSettings,
} from "@/lib/api";
import type { DockerSettings, ModelSource } from "@/lib/types";
import { useQuery } from "@/hooks/useQuery";
import {
  AlertCircle,
  Bot,
  Check,
  Eye,
  EyeOff,
  KeyRound,
  Lock,
  Moon,
  Network,
  Sun,
  Trash2,
  FlaskConical,
} from "lucide-react";
import { SunMoonIcon } from "@/components/BrandIcons";
import { type ThemeMode, getTheme, setTheme } from "@/lib/theme";
import { LANGUAGES, useI18n, useT } from "@/lib/i18n";
import { getConfig, useConfig } from "@/lib/config";
import SettingsSection from "@/components/SettingsSection";
import SettingsLibrary from "@/components/SettingsLibrary";
import SettingsMcp from "@/components/SettingsMcp";
import {
  Button,
  Code,
  ErrorLine,
  Field,
  IconButton,
  Input,
  Select,
  PageHeader,
  Spinner,
  Tabs,
  Textarea,
  Toggle,
} from "@/ui";

/** The tabs, in the order an operator meets them.
 *
 * One scrolling page put "how much VRAM per deployment" beside "which origins
 * may call this API", which are not the same kind of decision and are never
 * made at the same time. Grouping by *when you go looking* is what the tabs
 * are for.
 *
 * Two of them are new, and both are settings that used to live on the page
 * they governed. **Library** answers one question — where does this control
 * plane fetch things from — that was previously answered in three places: the
 * engine registry at the bottom of Engines, the model sources at the top of
 * Models, the OCI registries behind a sub-tab of Registries. **MCP** was a
 * route of its own that an operator visited once to copy a snippet; it is a
 * way *in* to this control plane, so it belongs beside authentication and the
 * allowed origins.
 *
 * There is deliberately no Cluster tab. Its switch survives — `cluster_enabled`
 * forces a `cluster_only` recipe on below two nodes — but it is a feature
 * switch, so it sits with the others. The rest of that tab described the
 * machines, which are managed on the Cluster page.
 */
const TABS = [
  { id: "deployment", labelKey: "settings.tabDeployment" },
  { id: "containers", labelKey: "settings.tabContainers" },
  { id: "features", labelKey: "settings.tabFeatures" },
  { id: "library", labelKey: "settingsPage.tabLibrary" },
  { id: "mcp", labelKey: "settingsPage.tabMcp" },
  { id: "preferences", labelKey: "settings.tabPreferences" },
  { id: "secrets", labelKey: "settings.tabSecrets" },
  { id: "environment", labelKey: "settings.tabEnvironment" },
] as const;

type TabId = (typeof TABS)[number]["id"];

const isTabId = (v: string): v is TabId => TABS.some((t) => t.id === v);

/** Where the operator was last looking, so a save does not send them back to
 *  the first tab. Per-browser and disposable — a lost value costs one click. */
const TAB_STORAGE_KEY = "spark-pulse:settings-tab";

function storedTab(): TabId | null {
  try {
    const saved = localStorage.getItem(TAB_STORAGE_KEY);
    if (saved && isTabId(saved)) return saved;
  } catch {
    // A private window, or site data turned off. The default is fine.
  }
  return null;
}

/** A form is read one field at a time, in order; a second column doubles the
 *  eye travel for every field. The one tab that is a reference table rather
 *  than a form — Environment — keeps two columns, because it is scanned. */
const formCls = "max-w-3xl";
const referenceCls = "grid grid-cols-1 items-start gap-x-12 lg:grid-cols-2";

/** One read-only fact about how this process is configured. */
function Fact({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-line py-2.5 last:border-0">
      <div className="min-w-0">
        <p className="text-[14px] font-medium">{label}</p>
        {hint && <p className="mt-0.5 text-[13px] leading-snug text-muted">{hint}</p>}
      </div>
      <div className="max-w-[55%] shrink-0 text-right">{value}</div>
    </div>
  );
}

/** A switch with its name and its one line of explanation. */
function SwitchRow({
  label,
  hint,
  icon: Icon,
  on,
  onChange,
  children,
}: {
  label: string;
  hint: string;
  icon?: typeof Bot;
  on: boolean;
  onChange: (next: boolean) => void;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <p className="flex items-center gap-1.5 text-[14px] font-medium">
          {Icon && <Icon size={14} className="text-muted" />}
          {label}
        </p>
        <p className="mt-0.5 text-[13px] leading-snug text-muted">{hint}</p>
        {children}
      </div>
      <Toggle on={on} onChange={onChange} label={label} />
    </div>
  );
}

/** The colour theme, as three explicit choices rather than a cycler.
 *
 * "System" is the default and follows the operating system, which is why it
 * gets the half-sun-half-moon mark rather than a third unrelated symbol.
 */
const THEMES: { id: ThemeMode; key: string }[] = [
  { id: "system", key: "System" },
  { id: "light", key: "Light" },
  { id: "dark", key: "Dark" },
];

function ThemePicker() {
  const { t } = useI18n();
  const [mode, setMode] = useState<ThemeMode>(getTheme);

  const choose = (next: ThemeMode) => {
    setMode(next);
    // Persisted to localStorage — a per-browser preference, not something the
    // control plane has an opinion about.
    setTheme(next);
  };

  return (
    <div className="grid grid-cols-1 gap-2 min-[400px]:grid-cols-3">
      {THEMES.map(({ id, key }) => {
        const label = t(`preferences.theme${key}`);
        const hint = t(`preferences.theme${key}Hint`);
        const active = mode === id;
        return (
          <button
            key={id}
            type="button"
            onClick={() => choose(id)}
            aria-pressed={active}
            title={hint}
            className={`flex flex-col items-center gap-2 rounded-sm border px-3 py-4 transition-colors duration-200 ${
              active
                ? "border-line-strong bg-blue/10 text-text"
                : "border-line text-muted hover:border-line-strong hover:text-text"
            }`}
          >
            {id === "system" ? (
              <SunMoonIcon size={20} />
            ) : id === "light" ? (
              <Sun size={20} />
            ) : (
              <Moon size={20} />
            )}
            <span className="text-[14px] font-medium">{label}</span>
          </button>
        );
      })}
    </div>
  );
}

/** The interface language, each offered in its own name — somebody looking for
 *  French looks for "Français". Per browser, like the theme. */
function LanguagePicker() {
  const { language, setLanguage, t } = useI18n();

  return (
    <div
      className="grid grid-cols-1 gap-2 md:grid-cols-2"
      role="group"
      aria-label={t("preferences.languageLabel")}
    >
      {LANGUAGES.map(({ id, endonym }) => {
        const active = language === id;
        return (
          <button
            key={id}
            type="button"
            onClick={() => setLanguage(id)}
            aria-pressed={active}
            lang={id}
            className={`rounded-sm border px-3 py-3 text-[14px] font-medium transition-colors duration-200 ${
              active
                ? "border-line-strong bg-blue/10 text-text"
                : "border-line text-muted hover:border-line-strong hover:text-text"
            }`}
          >
            {endonym}
          </button>
        );
      })}
    </div>
  );
}

/** The field the environment owns, marked as such. */
function EnvBadge() {
  const t = useT();
  return (
    <span className="ml-1.5 inline-flex items-center gap-1 rounded-sm bg-warn/15 px-1.5 py-0.5 text-[12.5px] font-normal text-warn">
      <Lock size={10} />
      {t("settings.envBadge")}
    </span>
  );
}

export default function SettingsPage() {
  const { t } = useI18n();
  const location = useLocation();
  const navigate = useNavigate();
  const { data: settings, loading, error, refetch } = useQuery(fetchSettings);
  const { data: secrets, refetch: refetchSecrets } = useQuery(fetchSecrets);
  const { data: modelSources, refetch: refetchSources } = useQuery(fetchModelSources);
  const [form, setForm] = useState<Record<string, unknown>>({});
  /** The model sources as they are being edited. They are their own endpoint
   *  but the same form: the page's one Save writes them too. */
  const [sources, setSources] = useState<ModelSource[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  /** The tab, deep-linked by hash so `/settings#mcp` — and the redirect from
   *  the old `/mcp` route — land where they say they do. The hash wins; the
   *  remembered tab is the fallback, so a save (which re-reads the settings
   *  and re-renders the page) does not send the operator back to the first
   *  tab. */
  const hashTab = location.hash.replace(/^#/, "");
  const [fallbackTab, setFallbackTab] = useState<TabId>(() => storedTab() ?? "deployment");
  const tab: TabId = isTabId(hashTab) ? hashTab : fallbackTab;

  const selectTab = (id: TabId) => {
    setFallbackTab(id);
    navigate({ hash: id }, { replace: true });
    try {
      localStorage.setItem(TAB_STORAGE_KEY, id);
    } catch {
      /* not worth reporting */
    }
  };

  const envManaged = (settings?.env_managed ?? []) as string[];
  const isEnvManaged = (field: string) => envManaged.includes(field);

  // ── The docker: block ──────────────────────────────────────────────────
  //
  // Read from the form, which is seeded from the server — not from literals
  // in the markup. A field showing 110 GB because the JSX says 110 tells the
  // operator nothing about what this machine will actually do.
  const dockerCfg = (form.docker ?? {}) as DockerSettings;
  const setDocker = <K extends keyof DockerSettings>(key: K, val: DockerSettings[K]) =>
    setForm({ ...form, docker: { ...dockerCfg, [key]: val } });

  const modCfg = (form.mod ?? {}) as { network_policy?: string };
  const environment = settings?.environment;

  // MCP is mounted by `app.py` only when `config.mcp_enabled`, which is what
  // `/api/config` publishes. Outside a ConfigProvider the context is null;
  // `getConfig()` is the same cached answer the rest of the SPA falls back to.
  const { config } = useConfig();
  const mcpEnabled = config?.mcp_enabled ?? getConfig().mcp_enabled;

  // HF token state
  const [hfToken, setHfToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [savingToken, setSavingToken] = useState(false);
  const [savedToken, setSavedToken] = useState(false);
  const [tokenError, setTokenError] = useState<string | null>(null);

  const settingsDirty =
    settings != null &&
    Object.keys(form).some(
      (k) => JSON.stringify(form[k]) !== JSON.stringify((settings as unknown as Record<string, unknown>)[k]),
    );
  const sourcesDirty =
    sources != null && modelSources != null && JSON.stringify(sources) !== JSON.stringify(modelSources);
  const isDirty = settingsDirty || sourcesDirty;

  useEffect(() => {
    if (settings) setForm({ ...settings });
  }, [settings]);
  useEffect(() => {
    if (modelSources) setSources(modelSources);
  }, [modelSources]);

  const handleSave = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      if (settingsDirty) await updateSettings(form as Parameters<typeof updateSettings>[0]);
      if (sourcesDirty && sources) await saveModelSources(sources);
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
      refetch();
      if (sourcesDirty) refetchSources();
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : t("settings.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const handleSaveToken = async () => {
    setSavingToken(true);
    setTokenError(null);
    try {
      await saveSecrets({ hf_token: hfToken });
      setHfToken("");
      setSavedToken(true);
      setTimeout(() => setSavedToken(false), 3000);
      refetchSecrets();
    } catch (e) {
      setTokenError(e instanceof Error ? e.message : t("settings.tokenSaveFailed"));
    } finally {
      setSavingToken(false);
    }
  };

  const handleClearToken = async () => {
    setTokenError(null);
    try {
      await deleteSecret("hf_token");
      setHfToken("");
      refetchSecrets();
    } catch (e) {
      setTokenError(e instanceof Error ? e.message : t("settings.tokenClearFailed"));
    }
  };

  const tabItems = useMemo(
    () => TABS.map(({ id, labelKey }) => ({ id, label: t(labelKey) })),
    [t],
  );

  if (loading)
    return (
      <div className="flex justify-center py-20">
        <Spinner size="lg" label={t("common.loading")} />
      </div>
    );
  if (error) return <ErrorLine>{error}</ErrorLine>;

  return (
    <div>
      <PageHeader
        eyebrow={t("nav.settings")}
        title={t("settings.heading")}
        description={t("settingsPage.subtitle")}
        actions={
          /* Save is on every tab, not on the three that happen to hold
             `/api/settings` fields. One form means an edit on Containers is
             still unsaved while the operator is reading Environment, and a
             button that disappears when they look away is how it gets lost. */
          <div className="flex flex-wrap items-center gap-3">
            {saveError ? (
              <ErrorLine>{saveError}</ErrorLine>
            ) : saved ? (
              <span className="text-[13px] text-good">
                {t("settings.savedTo")} <Code>~/.config/spark-pulse/settings.json</Code>
              </span>
            ) : isDirty ? (
              <span className="text-[13px] text-muted">{t("settings.unsaved")}</span>
            ) : null}
            <Button
              variant="primary"
              icon={saved ? Check : undefined}
              loading={saving}
              disabled={!isDirty}
              onClick={handleSave}
            >
              {saving ? t("common.saving") : t("settings.save")}
            </Button>
          </div>
        }
      />

      <Tabs
        label={t("settings.title")}
        value={tab}
        onChange={(id) => selectTab(id as TabId)}
        tabs={tabItems}
        className="mb-8"
      />

      {/* ── Deployment ───────────────────────────────────────────────────── */}
      {tab === "deployment" && (
        <div className={formCls}>
          <SettingsSection first title={t("settings.defaults")}>
            <Field label={t("settings.portRange")} hint={t("settings.portRangeHelp")}>
              <div className="flex items-center gap-2">
                <Input
                  mono
                  aria-label={t("settings.portRangeStart")}
                  type="number"
                  value={Number(form.default_port_range_start ?? 9000)}
                  onChange={(e) =>
                    setForm({ ...form, default_port_range_start: parseInt(e.target.value) || 9000 })
                  }
                  placeholder="9000"
                />
                <span className="shrink-0 text-[14px] text-muted">–</span>
                <Input
                  mono
                  aria-label={t("settings.portRangeEnd")}
                  type="number"
                  value={Number(form.default_port_range_end ?? 9100)}
                  onChange={(e) =>
                    setForm({ ...form, default_port_range_end: parseInt(e.target.value) || 9100 })
                  }
                  placeholder="9100"
                />
              </div>
            </Field>
          </SettingsSection>

          <SettingsSection title={t("settings.timeouts")}>
            <Field label={t("settings.readyTimeout")} hint={t("settings.readyTimeoutHelp")}>
              {(control) => (
                <div className="flex items-center gap-2">
                  {/* The width is on the wrapper, not the control: `Input` is
                      `w-full`, and `cn` is clsx rather than tailwind-merge, so
                      a `w-28` passed to it loses to the base class. */}
                  <div className="w-28 shrink-0">
                    <Input
                      {...control}
                      mono
                      type="number"
                      min="30"
                      max="7200"
                      value={Number(form.deploy_ready_timeout_seconds ?? 600)}
                      onChange={(e) =>
                        setForm({
                          ...form,
                          deploy_ready_timeout_seconds: parseInt(e.target.value) || 600,
                        })
                      }
                    />
                  </div>
                  <span className="shrink-0 text-[14px] text-muted">{t("common.seconds")}</span>
                </div>
              )}
            </Field>

            <Field label={t("settings.pullTimeout")} hint={t("settings.pullTimeoutHelp")}>
              {(control) => (
                <div className="flex items-center gap-2">
                  <div className="w-28 shrink-0">
                    <Input
                      {...control}
                      mono
                      type="number"
                      min="30"
                      max="3600"
                      value={Number(form.docker_pull_stall_timeout_seconds ?? 300)}
                      onChange={(e) =>
                        setForm({
                          ...form,
                          docker_pull_stall_timeout_seconds: parseInt(e.target.value) || 300,
                        })
                      }
                    />
                  </div>
                  <span className="shrink-0 text-[14px] text-muted">{t("common.seconds")}</span>
                </div>
              )}
            </Field>

            <Field label={t("settings.retention")} hint={t("settings.retentionHelp")}>
              {(control) => (
                <div className="flex items-center gap-2">
                  <div className="w-28 shrink-0">
                    <Input
                      {...control}
                      mono
                      type="number"
                      min="0"
                      max="365"
                      value={Number(form.job_retention_days ?? 7)}
                      onChange={(e) =>
                        setForm({ ...form, job_retention_days: parseInt(e.target.value) || 0 })
                      }
                    />
                  </div>
                  <span className="shrink-0 text-[14px] text-muted">{t("common.days")}</span>
                </div>
              )}
            </Field>
          </SettingsSection>
        </div>
      )}

      {/* ── Containers ───────────────────────────────────────────────────── */}
      {tab === "containers" && (
        <div className={formCls}>
          <SettingsSection first title={t("settings.limits")} hint={t("settings.limitsHelp")}>
            <SwitchRow
              label={t("settings.privileged")}
              hint={t("settings.privilegedHelp")}
              on={dockerCfg.privileged !== false}
              onChange={() => setDocker("privileged", dockerCfg.privileged === false)}
            />

            <Field label={t("settings.memLimit")} hint={t("settings.memLimitHelp")}>
              {(control) => (
                <Input
                  {...control}
                  mono
                  type="number"
                  min="0"
                  step="1"
                  value={dockerCfg.memory_limit_gb ?? ""}
                  onChange={(e) =>
                    setDocker(
                      "memory_limit_gb",
                      e.target.value === "" ? null : parseFloat(e.target.value),
                    )
                  }
                  placeholder={t("settings.noLimit")}
                />
              )}
            </Field>

            <Field label={t("settings.swapLimit")} hint={t("settings.swapLimitHelp")}>
              {(control) => (
                <Input
                  {...control}
                  mono
                  type="number"
                  min="0"
                  step="1"
                  value={dockerCfg.memory_swap_limit_gb ?? ""}
                  onChange={(e) =>
                    setDocker(
                      "memory_swap_limit_gb",
                      e.target.value === "" ? null : parseFloat(e.target.value),
                    )
                  }
                  placeholder={t("settings.noLimit")}
                />
              )}
            </Field>

            <Field label={t("settings.shm")} hint={t("settingsPage.shmHelp")}>
              {(control) => (
                <Input
                  {...control}
                  mono
                  type="number"
                  min="1"
                  step="1"
                  value={Number(dockerCfg.shm_size_gb ?? 64)}
                  onChange={(e) => setDocker("shm_size_gb", parseInt(e.target.value) || 64)}
                  placeholder="64"
                />
              )}
            </Field>

            <Field label={t("settings.pids")} hint={t("settingsPage.pidsHelp")}>
              {(control) => (
                <Input
                  {...control}
                  mono
                  type="number"
                  min="64"
                  step="64"
                  value={Number(dockerCfg.pids_limit ?? 4096)}
                  onChange={(e) => setDocker("pids_limit", parseInt(e.target.value) || 4096)}
                  placeholder="4096"
                />
              )}
            </Field>

            <Field label={t("settings.nofile")} hint={t("settingsPage.nofileHelp")}>
              {(control) => (
                <Input
                  {...control}
                  mono
                  type="number"
                  min="1024"
                  step="1024"
                  value={Number(dockerCfg.nofile_limit ?? 1048576)}
                  onChange={(e) => setDocker("nofile_limit", parseInt(e.target.value) || 1048576)}
                  placeholder="1048576"
                />
              )}
            </Field>
          </SettingsSection>

          <SettingsSection title={t("settings.caches")}>
            <Field label={t("settings.cacheDirs")} hint={t("settings.cacheDirsHelp")}>
              {(control) => (
                <Textarea
                  {...control}
                  aria-label={t("settings.cacheDirs")}
                  mono
                  rows={4}
                  value={(dockerCfg.cache_dirs ?? []).join("\n")}
                  onChange={(e) =>
                    setDocker(
                      "cache_dirs",
                      e.target.value.split("\n").map((l) => l.trim()).filter(Boolean),
                    )
                  }
                  placeholder="~/.cache/vllm"
                />
              )}
            </Field>

            <SwitchRow
              label={t("settings.keepEntrypoint")}
              hint={t("settings.keepEntrypointHelp")}
              on={!!dockerCfg.keep_entrypoint}
              onChange={() => setDocker("keep_entrypoint", !dockerCfg.keep_entrypoint)}
            />
          </SettingsSection>

          <SettingsSection title={t("settings.mods")}>
            <Field label={t("settings.modPolicy")} hint={t("settings.modPolicyHelp")}>
              <Select
                aria-label={t("settings.modPolicyLabel")}
                value={String(modCfg.network_policy ?? "warn")}
                onChange={(e) => setForm({ ...form, mod: { ...modCfg, network_policy: e.target.value } })}
              >
                <option value="allow">{t("settings.modAllow")}</option>
                <option value="warn">{t("settings.modWarn")}</option>
                <option value="deny">{t("settings.modDeny")}</option>
              </Select>
            </Field>
          </SettingsSection>
        </div>
      )}

      {/* ── Features ─────────────────────────────────────────────────────── */}
      {tab === "features" && (
        <div className={formCls}>
          <SettingsSection first title={t("settings.features")} hint={t("settings.featuresHelp")}>
            <SwitchRow
              icon={FlaskConical}
              label={t("settings.benchmarking")}
              hint={t("settings.benchmarkingHelp")}
              on={!!form.benchmarking_enabled}
              onChange={() => setForm({ ...form, benchmarking_enabled: !form.benchmarking_enabled })}
            />

            {/* Cluster mode decides whether recipes marked `cluster_only` are
                offered at all, which is the same kind of decision as
                benchmarking: it changes what the operator is shown, not how a
                a run behaves. The nodes themselves are on /cluster. */}
            <SwitchRow
              icon={Network}
              label={t("settings.clusterMode")}
              hint={t("settings.clusterModeHelp")}
              on={!!form.cluster_enabled}
              onChange={() => setForm({ ...form, cluster_enabled: !form.cluster_enabled })}
            >
              {environment?.cluster_experimental && (
                <p className="mt-1.5 flex items-start gap-1.5 text-[13px] text-warn">
                  <AlertCircle size={12} className="mt-0.5 shrink-0" />
                  <span>{t("settings.clusterExperimental")}</span>
                </p>
              )}
            </SwitchRow>

            <SwitchRow
              label={t("settings.agentAutoUpdate")}
              hint={t("settings.agentAutoUpdateHelp")}
              on={form.agent_auto_update !== false}
              onChange={() =>
                setForm({ ...form, agent_auto_update: !(form.agent_auto_update !== false) })
              }
            />

            {/* MCP is env-managed, so it is reported rather than switched. What
                it is *for* now has a tab of its own. */}
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <p className="flex items-center gap-1.5 text-[14px] font-medium">
                  <Bot size={14} className="text-muted" />
                  {t("settings.mcpEndpoint")}
                </p>
                <p className="mt-0.5 text-[13px] leading-snug text-muted">
                  {t("settings.mcpFeatureHelp")}
                </p>
              </div>
              {environment?.mcp_enabled ? (
                <Code>{environment.mcp_path}</Code>
              ) : (
                <span className="text-[13px] text-muted">{t("common.disabled")}</span>
              )}
            </div>
          </SettingsSection>
        </div>
      )}

      {/* ── Library ──────────────────────────────────────────────────────── */}
      {tab === "library" && (
        <div className="max-w-4xl">
          <SettingsLibrary form={form} setForm={setForm} sources={sources} setSources={setSources} />
        </div>
      )}

      {/* ── MCP ──────────────────────────────────────────────────────────── */}
      {tab === "mcp" && (
        <div className="max-w-4xl">
          <SettingsMcp enabled={mcpEnabled} port={Number(settings?.webui_port ?? 8100)} />
        </div>
      )}

      {/* ── Preferences ──────────────────────────────────────────────────── */}
      {tab === "preferences" && (
        <div className={formCls}>
          <SettingsSection first title={t("preferences.appearance")} hint={t("preferences.themeNote")}>
            <Field label={t("preferences.theme")}>
              <ThemePicker />
            </Field>
          </SettingsSection>

          <SettingsSection title={t("preferences.language")} hint={t("preferences.languageNote")}>
            <Field label={t("preferences.languageLabel")}>
              <LanguagePicker />
            </Field>
          </SettingsSection>
        </div>
      )}

      {/* ── Secrets ──────────────────────────────────────────────────────── */}
      {tab === "secrets" && (
        <div className={formCls}>
          <SettingsSection
            first
            title={t("settings.secrets")}
            hint={t("settings.hfHelp")}
            actions={
              <span className="rounded-sm border border-line bg-bg px-2 py-0.5 font-mono text-[12.5px] text-muted">
                {t("settings.mode600")}
              </span>
            }
          >
            <Field
              label={t("settings.hfToken")}
              error={tokenError}
              hint={
                secrets?.hf_token ? (
                  <span className="text-good">
                    {t("settings.hfActive", { tail: secrets.hf_token.slice(-4) })}
                  </span>
                ) : undefined
              }
            >
              {(control) => (
                <div className="flex gap-2">
                  <div className="relative flex-1">
                    <Input
                      {...control}
                      mono
                      type={showToken ? "text" : "password"}
                      value={hfToken}
                      onChange={(e) => setHfToken(e.target.value)}
                      placeholder={secrets?.hf_token ? t("settings.hfReplace") : "hf_…"}
                      className="pr-9"
                      autoComplete="off"
                      aria-label={t("settings.hfToken")}
                    />
                    <button
                      type="button"
                      onClick={() => setShowToken((v) => !v)}
                      aria-label={showToken ? t("settings.hideToken") : t("settings.showToken")}
                      className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted transition-colors hover:text-text"
                    >
                      {showToken ? <EyeOff size={15} /> : <Eye size={15} />}
                    </button>
                  </div>
                  <Button
                    size="sm"
                    variant="primary"
                    icon={savedToken ? Check : KeyRound}
                    loading={savingToken}
                    disabled={!hfToken.trim()}
                    onClick={handleSaveToken}
                  >
                    {savedToken ? t("common.saved") : t("common.save")}
                  </Button>
                  {secrets?.hf_token && (
                    <IconButton
                      size="sm"
                      variant="danger"
                      icon={Trash2}
                      label={t("settings.clearToken")}
                      onClick={handleClearToken}
                    />
                  )}
                </div>
              )}
            </Field>
          </SettingsSection>
        </div>
      )}

      {/* ── Environment ──────────────────────────────────────────────────── */}
      {tab === "environment" && environment && (
        <div className={referenceCls}>
          <SettingsSection first title={t("settings.runtime")} hint={t("settings.runtimeNote")}>
            <div>
              <Fact
                label={t("settings.stateStore")}
                hint={t("settings.stateStoreHelp")}
                value={
                  <Code>
                    {environment.database_url ||
                      t("settings.defaultPath", { backend: environment.database_backend })}
                  </Code>
                }
              />
              <Fact
                label={t("settings.runtimeRow")}
                hint={t("settings.runtimeRowHelp")}
                value={<Code>{String(form.runtime ?? "native")}</Code>}
              />
              <Fact
                label={t("settings.webuiPort")}
                hint={isEnvManaged("webui_port") ? t("settings.webuiPortEnv") : undefined}
                value={
                  <>
                    <Code>{String(form.webui_port ?? "")}</Code>
                    {isEnvManaged("webui_port") && <EnvBadge />}
                  </>
                }
              />
              <Fact
                label={t("settings.workerThreads")}
                hint={t("settings.workerThreadsHelp")}
                value={<Code>{environment.thread_pool_size}</Code>}
              />
              {environment.image_registry?.mode && (
                <Fact
                  label={t("settings.imageRegistry")}
                  hint={
                    environment.image_registry.mode === "proxy"
                      ? t("settings.imageRegistryProxy", {
                          upstream: environment.image_registry.upstream || t("settings.theUpstream"),
                        })
                      : t("settings.imageRegistryLocal")
                  }
                  value={
                    <Code>{`${environment.image_registry.mode} · ${environment.image_registry.address}:${environment.image_registry.port}`}</Code>
                  }
                />
              )}
            </div>
          </SettingsSection>

          <SettingsSection first title={t("settings.access")} className="max-lg:mt-10 max-lg:border-t max-lg:border-line max-lg:pt-12">
            <div>
              <Fact
                label={t("settings.auth")}
                hint={environment.auth_enabled ? t("settings.authOn") : t("settings.authOff")}
                value={
                  <span
                    className={`text-[13px] font-medium ${environment.auth_enabled ? "text-good" : "text-warn"}`}
                  >
                    {environment.auth_enabled ? t("common.enabled") : t("common.disabled")}
                  </span>
                }
              />
              {environment.auth_enabled && environment.oidc_provider_url && (
                <Fact label={t("settings.idp")} value={<Code>{environment.oidc_provider_url}</Code>} />
              )}
              <Fact
                label={t("settings.externalUrl")}
                hint={t("settings.externalUrlHelp")}
                value={<Code>{environment.external_url || t("settings.notPinned")}</Code>}
              />
              <Fact
                label={t("settings.origins")}
                hint={t("settings.originsHelp")}
                value={
                  <div className="space-y-0.5">
                    {environment.cors_allowed_origins.map((o) => (
                      <div key={o}>
                        <Code>{o}</Code>
                      </div>
                    ))}
                  </div>
                }
              />
              <Fact
                label={t("settings.mcpEndpoint")}
                hint={t("settings.mcpHelp")}
                value={
                  environment.mcp_enabled ? (
                    <Code>{environment.mcp_path}</Code>
                  ) : (
                    <span className="text-[13px] text-muted">{t("common.disabled")}</span>
                  )
                }
              />
            </div>
          </SettingsSection>
        </div>
      )}
    </div>
  );
}
