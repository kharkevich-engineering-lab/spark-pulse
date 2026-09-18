import { useState, useEffect } from "react";
import { fetchSettings, updateSettings, fetchSecrets, saveSecrets, deleteSecret } from "@/lib/api";
import type { DockerSettings } from "@/lib/types";
import { useQuery } from "@/hooks/useQuery";
import { Settings as SettingsIcon, AlertCircle, Check, KeyRound, Eye, EyeOff, Trash2, Lock, Server, Box, Info, ShieldCheck, Palette, Sun, Moon, Languages, ToggleRight, FlaskConical, Bot, Network } from "lucide-react";
import { SunMoonIcon } from "@/components/BrandIcons";
import { type ThemeMode, getTheme, setTheme } from "@/lib/theme";
import { LANGUAGES, useI18n } from "@/lib/i18n";
import {
  AlertModal,
  Button,
  Card,
  Code,
  ErrorLine,
  IconButton,
  Input,
  Select,
  Spinner,
  Tabs,
  Textarea,
  Toggle,
} from "@/ui";

/** The tabs, in the order an operator meets them.
 *
 * Six cards on one scrolling page put "how much VRAM per deployment" beside
 * "which origins may call this API", which are not the same kind of decision
 * and are never made at the same time. Grouping by *when you go looking* is
 * what the tabs are for.
 *
 * There is deliberately no Cluster tab. Its switch survives — `cluster_enabled`
 * forces a `cluster_only` recipe on below two nodes — but it is a feature
 * switch, so it sits with the others. It is only an override: what normally
 * decides whether those recipes are offered is the registry, which
 * `/api/settings` derives into its `cluster` block. The rest of that tab
 * described the machines, which are managed on the Cluster page, and network
 * discovery moved there with them.
 *
 * There is no Engines tab either: an engine is its image, and the page that
 * lists images now lists both, with the registry settings that govern where
 * engines come from at the bottom of it.
 *
 * `features` holds the switches that change the *shape* of the app rather than
 * the behaviour of a deployment: turning benchmarking off removes a route and
 * a sidebar entry. That is a different kind of decision from a timeout, which
 * is where it used to sit.
 */
const TABS = [
  { id: "deployment", labelKey: "settings.tabDeployment", icon: Server },
  { id: "containers", labelKey: "settings.tabContainers", icon: Box },
  { id: "features", labelKey: "settings.tabFeatures", icon: ToggleRight },
  { id: "preferences", labelKey: "settings.tabPreferences", icon: Palette },
  { id: "secrets", labelKey: "settings.tabSecrets", icon: KeyRound },
  { id: "environment", labelKey: "settings.tabEnvironment", icon: Info },
] as const;

type TabId = (typeof TABS)[number]["id"];

/** Where the operator was last looking, so a save does not send them back to
 *  the first tab. Per-browser and disposable — a lost value costs one click. */
const TAB_STORAGE_KEY = "spark-pulse:settings-tab";

function storedTab(): TabId {
  try {
    const saved = localStorage.getItem(TAB_STORAGE_KEY);
    if (TABS.some((t) => t.id === saved)) return saved as TabId;
  } catch {
    // A private window, or site data turned off. The default is fine.
  }
  return "deployment";
}

/** One card per row, at a measure a form is read at.
 *
 * Every tab used to be `lg:grid-cols-2`, which existed so tabs holding two
 * small cards did not look empty. A settings form is read one field at a time,
 * in order, and a second column doubles the eye travel for every field. The one
 * tab that is a reference table rather than a form — Environment — keeps two
 * columns, because it is scanned rather than filled in.
 */
const sectionCls = "space-y-4 max-w-3xl";
const referenceCls = "grid grid-cols-1 lg:grid-cols-2 gap-4 items-start";

/** One read-only fact about how this process is configured. */
function Fact({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="flex items-start justify-between gap-4 py-2 border-b border-border last:border-0">
      <div className="min-w-0">
        <p className="text-sm font-medium">{label}</p>
        {hint && <p className="text-xs text-text-muted mt-0.5">{hint}</p>}
      </div>
      <div className="text-right shrink-0 max-w-[55%]">{value}</div>
    </div>
  );
}

/** The colour theme, as three explicit choices rather than a cycler.
 *
 * It used to be one unlabelled button in the header that stepped
 * dark → light → system, so the only way to find out what a click would do
 * was to click it. Three buttons say what each is, and which is on.
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
    <div className="grid grid-cols-3 gap-2">
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
            className={`flex flex-col items-center gap-2 px-3 py-4 rounded-sm border transition-colors ${
              active
                ? "border-primary bg-primary/10 text-blue2"
                : "border-border text-text-muted hover:text-text hover:border-border-hover"
            }`}
          >
            {id === "system" ? <SunMoonIcon size={20} /> : id === "light" ? <Sun size={20} /> : <Moon size={20} />}
            <span className="text-sm font-medium">{label}</span>
          </button>
        );
      })}
    </div>
  );
}

/** The interface language.
 *
 * Each language is offered in its own name — somebody looking for French
 * looks for "Français", not for the English word for it. Like the theme this
 * is per browser: the control plane has no opinion about it, and two people
 * signing in to the same machine can read it differently.
 */
function LanguagePicker() {
  const { language, setLanguage, t } = useI18n();

  return (
    <div className="grid grid-cols-2 gap-2" role="group" aria-label={t("preferences.languageLabel")}>
      {LANGUAGES.map(({ id, endonym }) => {
        const active = language === id;
        return (
          <button
            key={id}
            type="button"
            onClick={() => setLanguage(id)}
            aria-pressed={active}
            lang={id}
            className={`px-3 py-3 rounded-sm border text-sm font-medium transition-colors ${
              active
                ? "border-primary bg-primary/10 text-blue2"
                : "border-border text-text-muted hover:text-text hover:border-border-hover"
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
  return (
    <span className="inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-warning/15 text-warning font-normal ml-1.5">
      <Lock size={10} />env
    </span>
  );
}

export default function SettingsPage() {
  const { t } = useI18n();
  const { data: settings, loading, error, refetch } = useQuery(fetchSettings);
  const { data: secrets, refetch: refetchSecrets } = useQuery(fetchSecrets);
  const [form, setForm] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);
  const [tab, setTab] = useState<TabId>(storedTab);

  const selectTab = (id: TabId) => {
    setTab(id);
    try { localStorage.setItem(TAB_STORAGE_KEY, id); } catch { /* not worth reporting */ }
  };

  const envManaged = (settings?.env_managed ?? []) as string[];
  const isEnvManaged = (field: string) => envManaged.includes(field);
  // ── The docker: block ──────────────────────────────────────────────────
  //
  // Read from the form, which is seeded from the server — not from literals
  // in the markup. A field showing 110 GB because the JSX says 110 tells the
  // operator nothing about what this machine will actually do, and that is
  // exactly what this page used to show.
  const dockerCfg = (form.docker ?? {}) as DockerSettings;
  const setDocker = <K extends keyof DockerSettings>(key: K, val: DockerSettings[K]) =>
    setForm({ ...form, docker: { ...dockerCfg, [key]: val } });

  const modCfg = (form.mod ?? {}) as { network_policy?: string };
  const environment = settings?.environment;

  // HF Token state
  const [hfToken, setHfToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [savingToken, setSavingToken] = useState(false);
  const [savedToken, setSavedToken] = useState(false);

  const isDirty = settings != null && Object.keys(form).some(
    (k) => JSON.stringify(form[k]) !== JSON.stringify((settings as unknown as Record<string, unknown>)[k])
  );

  useEffect(() => { if (settings) setForm({ ...settings }); }, [settings]);

  const handleSave = async () => {
    setSaving(true);
    try {
      await updateSettings(form as Parameters<typeof updateSettings>[0]);
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
      refetch();
    } catch (e) {
      setAlertModal({ title: t("common.error"), message: e instanceof Error ? e.message : t("settings.saveFailed") });
    } finally {
      setSaving(false);
    }
  };

  const handleSaveToken = async () => {
    setSavingToken(true);
    try {
      await saveSecrets({ hf_token: hfToken });
      setHfToken("");
      setSavedToken(true);
      setTimeout(() => setSavedToken(false), 3000);
      refetchSecrets();
    } catch (e) {
      setAlertModal({ title: t("common.error"), message: e instanceof Error ? e.message : t("settings.tokenSaveFailed") });
    } finally {
      setSavingToken(false);
    }
  };

  const handleClearToken = async () => {
    try {
      await deleteSecret("hf_token");
      setHfToken("");
      refetchSecrets();
    } catch (e) {
      setAlertModal({ title: t("common.error"), message: e instanceof Error ? e.message : t("settings.tokenClearFailed") });
    }
  };

  if (loading)
    return (
      <div className="flex justify-center py-20">
        <Spinner size="lg" label={t("common.loading")} />
      </div>
    );
  if (error) return <ErrorLine>{error}</ErrorLine>;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">{t("settings.title")}</h2>
        <p className="text-text-muted mt-1">{t("settings.subtitle")}</p>
      </div>

      {/* ── Tabs ─────────────────────────────────────────────────────────── */}
      <Tabs
        label={t("settings.title")}
        value={tab}
        onChange={(id) => selectTab(id as TabId)}
        tabs={TABS.map(({ id, labelKey }) => ({ id, label: t(labelKey) }))}
      />

      {/* ── Deployment ───────────────────────────────────────────────────── */}
      {tab === "deployment" && (
        <div className={sectionCls}>
          <Card padding="none" className="p-5 space-y-4">
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <Server size={16} className="text-blue2" />
              <h3 className="font-semibold">{t("settings.defaults")}</h3>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">{t("settings.portRange")}</label>
              <div className="flex items-center gap-2">
                <Input mono aria-label={t("settings.portRangeStart")} type="number" value={Number(form.default_port_range_start ?? 9000)} onChange={(e) => setForm({ ...form, default_port_range_start: parseInt(e.target.value) || 9000 })} placeholder="9000" />
                <span className="text-text-muted shrink-0 text-sm">–</span>
                <Input mono aria-label={t("settings.portRangeEnd")} type="number" value={Number(form.default_port_range_end ?? 9100)} onChange={(e) => setForm({ ...form, default_port_range_end: parseInt(e.target.value) || 9100 })} placeholder="9100" />
              </div>
              <p className="text-xs text-text-muted mt-1">{t("settings.portRangeHelp")}</p>
            </div>

            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-sm font-medium">{t("settings.sparkPath")}</label>
                {isEnvManaged("spark_vllm_path") && <EnvBadge />}
              </div>
              <Input mono type="text" value={String(form.spark_vllm_path ?? "")} onChange={(e) => setForm({ ...form, spark_vllm_path: e.target.value })} disabled={isEnvManaged("spark_vllm_path")} placeholder="/path/to/spark-vllm-docker" />
              <p className="text-xs text-text-muted mt-1">{isEnvManaged("spark_vllm_path") ? t("settings.sparkPathEnv") : t("settings.sparkPathHelp")}</p>
            </div>
          </Card>

          <Card padding="none" className="p-5 space-y-4">
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <SettingsIcon size={16} className="text-blue2" />
              <h3 className="font-semibold">{t("settings.timeouts")}</h3>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">{t("settings.readyTimeout")}</label>
              <div className="flex items-center gap-2">
                <Input mono type="number" min="30" max="7200" value={Number(form.deploy_ready_timeout_seconds ?? 600)} onChange={(e) => setForm({ ...form, deploy_ready_timeout_seconds: parseInt(e.target.value) || 600 })} className="w-28" />
                <span className="text-sm text-text-muted">seconds</span>
              </div>
              <p className="text-xs text-text-muted mt-1">{t("settings.readyTimeoutHelp")}</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">{t("settings.pullTimeout")}</label>
              <div className="flex items-center gap-2">
                <Input mono type="number" min="30" max="3600" value={Number(form.docker_pull_stall_timeout_seconds ?? 300)} onChange={(e) => setForm({ ...form, docker_pull_stall_timeout_seconds: parseInt(e.target.value) || 300 })} className="w-28" />
                <span className="text-sm text-text-muted">seconds</span>
              </div>
              <p className="text-xs text-text-muted mt-1">{t("settings.pullTimeoutHelp")}</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">{t("settings.retention")}</label>
              <div className="flex items-center gap-2">
                <Input mono type="number" min="0" max="365" value={Number(form.job_retention_days ?? 7)} onChange={(e) => setForm({ ...form, job_retention_days: parseInt(e.target.value) || 0 })} className="w-24 px-3 py-2 rounded-sm bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" />
                <span className="text-sm text-text-muted">days</span>
              </div>
              <p className="text-xs text-text-muted mt-1">{t("settings.retentionHelp")}</p>
            </div>
          </Card>
        </div>
      )}

      {/* ── Containers ───────────────────────────────────────────────────── */}
      {tab === "containers" && (
        <div className={sectionCls}>
          <Card padding="none" className="p-5 space-y-4">
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <Box size={16} className="text-blue2" />
              <h3 className="font-semibold">{t("settings.limits")}</h3>
            </div>
            <p className="text-xs text-text-muted -mt-2">{t("settings.limitsHelp")}</p>

            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium">{t("settings.privileged")}</p>
                <p className="text-xs text-text-muted mt-0.5">{t("settings.privilegedHelp")}</p>
              </div>
              <Toggle on={dockerCfg.privileged !== false} onChange={() => setDocker("privileged", dockerCfg.privileged === false)} label={t("settings.privileged")} />
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">{t("settings.memLimit")}</label>
              <Input mono type="number" min="0" step="1" value={dockerCfg.memory_limit_gb ?? ""} onChange={(e) => setDocker("memory_limit_gb", e.target.value === "" ? null : parseFloat(e.target.value))} placeholder={t("settings.noLimit")} />
              <p className="text-xs text-text-muted mt-1">{t("settings.memLimitHelp")}</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">{t("settings.swapLimit")}</label>
              <Input mono type="number" min="0" step="1" value={dockerCfg.memory_swap_limit_gb ?? ""} onChange={(e) => setDocker("memory_swap_limit_gb", e.target.value === "" ? null : parseFloat(e.target.value))} placeholder={t("settings.noLimit")} />
              <p className="text-xs text-text-muted mt-1">{t("settings.swapLimitHelp")}</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">{t("settings.shm")}</label>
              <Input mono type="number" min="1" step="1" value={Number(dockerCfg.shm_size_gb ?? 64)} onChange={(e) => setDocker("shm_size_gb", parseInt(e.target.value) || 64)} placeholder="64" />
              <p className="text-xs text-text-muted mt-1"><code className="font-mono">/dev/shm</code>. Tensor-parallel workers pass tensors through it; too small shows up as a hang, not an error.</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">{t("settings.pids")}</label>
              <Input mono type="number" min="64" step="64" value={Number(dockerCfg.pids_limit ?? 4096)} onChange={(e) => setDocker("pids_limit", parseInt(e.target.value) || 4096)} placeholder="4096" />
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">{t("settings.nofile")}</label>
              <Input mono type="number" min="1024" step="1024" value={Number(dockerCfg.nofile_limit ?? 1048576)} onChange={(e) => setDocker("nofile_limit", parseInt(e.target.value) || 1048576)} placeholder="1048576" />
            </div>
          </Card>

          <div className="space-y-4">
            <Card padding="none" className="p-5 space-y-4">
              <div className="flex items-center gap-2 pb-3 border-b border-border">
                <Box size={16} className="text-blue2" />
                <h3 className="font-semibold">{t("settings.caches")}</h3>
              </div>

              <div>
                <label className="block text-sm font-medium mb-1">{t("settings.cacheDirs")}</label>
                <Textarea
                  aria-label={t("settings.cacheDirs")}
                  rows={4}
                  value={(dockerCfg.cache_dirs ?? []).join("\n")}
                  onChange={(e) => setDocker("cache_dirs", e.target.value.split("\n").map((l) => l.trim()).filter(Boolean))}
                 
                  placeholder="~/.cache/vllm"
                />
                <p className="text-xs text-text-muted mt-1">{t("settings.cacheDirsHelp")}</p>
              </div>

              <div className="flex items-center justify-between pt-4 border-t border-border">
                <div>
                  <p className="text-sm font-medium">{t("settings.keepEntrypoint")}</p>
                  <p className="text-xs text-text-muted mt-0.5">{t("settings.keepEntrypointHelp")}</p>
                </div>
                <Toggle on={!!dockerCfg.keep_entrypoint} onChange={() => setDocker("keep_entrypoint", !dockerCfg.keep_entrypoint)} label={t("settings.keepEntrypoint")} />
              </div>
            </Card>

            <Card padding="none" className="p-5 space-y-4">
              <div className="flex items-center gap-2 pb-3 border-b border-border">
                <ShieldCheck size={16} className="text-blue2" />
                <h3 className="font-semibold">{t("settings.mods")}</h3>
              </div>
              <div>
                <label className="block text-sm font-medium mb-1">{t("settings.modPolicy")}</label>
                <Select
                  aria-label={t("settings.modPolicyLabel")}
                  value={String(modCfg.network_policy ?? "warn")}
                  onChange={(e) => setForm({ ...form, mod: { ...modCfg, network_policy: e.target.value } })}
                 
                >
                  <option value="allow">{t("settings.modAllow")}</option>
                  <option value="warn">{t("settings.modWarn")}</option>
                  <option value="deny">{t("settings.modDeny")}</option>
                </Select>
                <p className="text-xs text-text-muted mt-1">{t("settings.modPolicyHelp")}</p>
              </div>
            </Card>
          </div>
        </div>
      )}


      {/* ── Features ─────────────────────────────────────────────────────── */}
      {tab === "features" && (
        <div className={sectionCls}>
          <Card padding="none" className="p-5 space-y-4">
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <ToggleRight size={16} className="text-blue2" />
              <h3 className="font-semibold">{t("settings.features")}</h3>
            </div>
            <p className="text-xs text-text-muted -mt-2">{t("settings.featuresHelp")}</p>

            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium flex items-center gap-1.5">
                  <FlaskConical size={14} className="text-text-muted" />
                  {t("settings.benchmarking")}
                </p>
                <p className="text-xs text-text-muted mt-0.5">{t("settings.benchmarkingHelp")}</p>
              </div>
              <Toggle on={!!form.benchmarking_enabled} onChange={() => setForm({ ...form, benchmarking_enabled: !form.benchmarking_enabled })} label={t("settings.benchmarking")} />
            </div>

            {/* Cluster mode decides whether recipes marked `cluster_only` are
                offered at all, which is the same kind of decision as
                benchmarking: it changes what the operator is shown, not how a
                deployment behaves. The machines themselves are on /cluster. */}
            <div className="flex items-center justify-between pt-4 border-t border-border">
              <div>
                <p className="text-sm font-medium flex items-center gap-1.5">
                  <Network size={14} className="text-text-muted" />
                  {t("settings.clusterMode")}
                </p>
                <p className="text-xs text-text-muted mt-0.5">{t("settings.clusterModeHelp")}</p>
                {environment?.cluster_experimental && (
                  <p className="text-xs text-warning mt-1 flex items-start gap-1.5">
                    <AlertCircle size={12} className="shrink-0 mt-0.5" />
                    <span>{t("settings.clusterExperimental")}</span>
                  </p>
                )}
              </div>
              <Toggle on={!!form.cluster_enabled} onChange={() => setForm({ ...form, cluster_enabled: !form.cluster_enabled })} label={t("settings.clusterMode")} />
            </div>

            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="font-medium">{t("settings.agentAutoUpdate")}</p>
                <p className="text-xs text-text-muted mt-1">{t("settings.agentAutoUpdateHelp")}</p>
              </div>
              <Toggle
                on={form.agent_auto_update !== false}
                onChange={() => setForm({ ...form, agent_auto_update: !(form.agent_auto_update !== false) })}
                label={t("settings.agentAutoUpdate")}
              />
            </div>

            {/* MCP is env-managed, so it is reported rather than switched. It
                belongs beside benchmarking anyway: both decide whether a way
                in exists at all. */}
            <div className="flex items-center justify-between pt-4 border-t border-border">
              <div>
                <p className="text-sm font-medium flex items-center gap-1.5">
                  <Bot size={14} className="text-text-muted" />
                  {t("settings.mcpEndpoint")}
                </p>
                <p className="text-xs text-text-muted mt-0.5">{t("settings.mcpFeatureHelp")}</p>
              </div>
              {environment?.mcp_enabled
                ? <Code>{environment.mcp_path}</Code>
                : <span className="text-xs text-text-muted">{t("common.disabled")}</span>}
            </div>
          </Card>
        </div>
      )}

      {/* ── Preferences ──────────────────────────────────────────────────── */}
      {tab === "preferences" && (
        <div className={sectionCls}>
          <Card padding="none" className="p-5 space-y-4">
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <Palette size={16} className="text-blue2" />
              <h3 className="font-semibold">{t("preferences.appearance")}</h3>
            </div>
            <div>
              <label className="block text-sm font-medium mb-2">{t("preferences.theme")}</label>
              <ThemePicker />
              <p className="text-xs text-text-muted mt-2">{t("preferences.themeNote")}</p>
            </div>
          </Card>

          <Card padding="none" className="p-5 space-y-4">
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <Languages size={16} className="text-blue2" />
              <h3 className="font-semibold">{t("preferences.language")}</h3>
            </div>
            <div>
              <label className="block text-sm font-medium mb-2">
                {t("preferences.languageLabel")}
              </label>
              <LanguagePicker />
              <p className="text-xs text-text-muted mt-2">{t("preferences.languageNote")}</p>
            </div>
          </Card>
        </div>
      )}

      {/* ── Secrets ──────────────────────────────────────────────────────── */}
      {tab === "secrets" && (
        <div className={sectionCls}>
          <Card padding="none" className="p-5 space-y-4">
            <div className="flex items-center justify-between pb-3 border-b border-border">
              <div className="flex items-center gap-2">
                <KeyRound size={16} className="text-blue2" />
                <h3 className="font-semibold">{t("settings.secrets")}</h3>
              </div>
              <span className="text-xs text-text-muted px-2 py-0.5 rounded bg-bg border border-border font-mono">{t("settings.mode600")}</span>
            </div>

            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-sm font-medium">{t("settings.hfToken")}</label>
                {secrets?.hf_token && <span className="text-xs text-success font-mono">{t("settings.hfActive", { tail: secrets.hf_token.slice(-4) })}</span>}
              </div>
              <div className="flex gap-2">
                <div className="relative flex-1">
                  <Input mono type={showToken ? "text" : "password"} value={hfToken} onChange={(e) => setHfToken(e.target.value)} placeholder={secrets?.hf_token ? t("settings.hfReplace") : "hf_…"} className="pr-9" autoComplete="off" aria-label={t("settings.hfToken")} />
                  <button type="button" onClick={() => setShowToken(v => !v)} aria-label={showToken ? t("settings.hideToken") : t("settings.showToken")} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-text-muted hover:text-text transition-colors">
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
              <p className="text-xs text-text-muted mt-1.5">
                Passed as <code className="font-mono">HF_TOKEN</code> when launching a deployment and used to fetch gated models. Stored in
                <code className="font-mono"> ~/.config/spark-pulse/secrets.json</code>; it is never sent back to this page.
              </p>
            </div>
          </Card>
        </div>
      )}

      {/* ── Environment ──────────────────────────────────────────────────── */}
      {tab === "environment" && environment && (
        <div className={referenceCls}>
          <Card padding="none" className="p-5 space-y-4">
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <Info size={16} className="text-blue2" />
              <h3 className="font-semibold">{t("settings.runtime")}</h3>
            </div>
            <p className="text-xs text-text-muted -mt-2">
              How this process is configured. Read-only here on purpose: these are set in
              <code className="font-mono"> settings.json</code> or the environment, and a browser that
              could change them would be a way past every other check.
            </p>

            <div>
              <Fact label={t("settings.stateStore")} hint={t("settings.stateStoreHelp")} value={<Code>{environment.database_url || t("settings.defaultPath", { backend: environment.database_backend })}</Code>} />
              <Fact label={t("settings.runtimeRow")} hint={t("settings.runtimeRowHelp")} value={<Code>{String(form.runtime ?? "native")}</Code>} />
              <Fact label={t("settings.webuiPort")} value={<Code>{String(form.webui_port ?? "")}</Code>} />
              <Fact label={t("settings.workerThreads")} hint={t("settings.workerThreadsHelp")} value={<Code>{environment.thread_pool_size}</Code>} />
              {environment.image_registry?.mode && (
                <Fact
                  label={t("settings.imageRegistry")}
                  hint={environment.image_registry.mode === "proxy"
                    ? t("settings.imageRegistryProxy", { upstream: environment.image_registry.upstream || t("settings.theUpstream") })
                    : t("settings.imageRegistryLocal")}
                  value={<Code>{`${environment.image_registry.mode} · ${environment.image_registry.address}:${environment.image_registry.port}`}</Code>}
                />
              )}
            </div>
          </Card>

          <Card padding="none" className="p-5 space-y-4">
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <ShieldCheck size={16} className="text-blue2" />
              <h3 className="font-semibold">{t("settings.access")}</h3>
            </div>

            <div>
              <Fact
                label={t("settings.auth")}
                hint={environment.auth_enabled ? t("settings.authOn") : t("settings.authOff")}
                value={
                  <span className={`text-xs font-medium ${environment.auth_enabled ? "text-success" : "text-warning"}`}>
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
                    {environment.cors_allowed_origins.map((o) => <div key={o}><Code>{o}</Code></div>)}
                  </div>
                }
              />
              <Fact
                label={t("settings.mcpEndpoint")}
                hint={t("settings.mcpHelp")}
                value={environment.mcp_enabled ? <Code>{environment.mcp_path}</Code> : <span className="text-xs text-text-muted">{t("common.disabled")}</span>}
              />
            </div>
          </Card>
        </div>
      )}

      {/* ── Save ─────────────────────────────────────────────────────────── */}
      {tab !== "secrets" && tab !== "environment" && tab !== "preferences" && (
        <div className="flex items-center gap-3">
          <Button
            variant="primary"
            icon={saved ? Check : SettingsIcon}
            loading={saving}
            disabled={!isDirty}
            onClick={handleSave}
          >
            {saving ? t("common.saving") : saved ? t("common.saved") : t("settings.save")}
          </Button>
          {/* One form across every tab, so this saves edits made on any of
              them — including a tab the operator has since navigated away
              from. Saying so beats a button that silently does more than it
              appears to. */}
          {isDirty && !saved && <span className="text-xs text-text-muted">{t("settings.unsaved")}</span>}
          {saved && <span className="text-xs text-success">{t("settings.savedTo")} <code className="font-mono">~/.config/spark-pulse/settings.json</code></span>}
        </div>
      )}

      {alertModal && <AlertModal open={!!alertModal} onClose={() => setAlertModal(null)} title={alertModal.title} message={alertModal.message} />}
    </div>
  );
}
