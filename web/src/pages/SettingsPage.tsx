import { useState, useEffect } from "react";
import { fetchSettings, updateSettings, fetchSecrets, saveSecrets, deleteSecret, runDiscovery, fetchEngines, refreshEngines, type DiscoveryResult, type ValidationResult } from "@/lib/api";
import type { DockerSettings } from "@/lib/types";
import { useQuery } from "@/hooks/useQuery";
import { Settings as SettingsIcon, Loader2, AlertCircle, Check, KeyRound, Eye, EyeOff, Trash2, Lock, Server, Box, Network, Radio, Wifi, WifiOff, Cpu, RefreshCw, Info, ShieldCheck, Palette, Sun, Moon } from "lucide-react";
import { SunMoonIcon } from "@/components/BrandIcons";
import { type ThemeMode, getTheme, setTheme } from "@/lib/theme";
import { EngineList } from "@/components/EngineBadge";
import { AlertModal } from "@/components/Modal";

/** The tabs, in the order an operator meets them.
 *
 * Six cards on one scrolling page put "how much VRAM per deployment" beside
 * "which origins may call this API", which are not the same kind of decision
 * and are never made at the same time. Grouping by *when you go looking* is
 * what the tabs are for.
 */
const TABS = [
  { id: "deployment", label: "Deployment", icon: Server },
  { id: "containers", label: "Containers", icon: Box },
  { id: "cluster", label: "Cluster", icon: Network },
  { id: "engines", label: "Engines", icon: Cpu },
  { id: "preferences", label: "Preferences", icon: Palette },
  { id: "secrets", label: "Secrets", icon: KeyRound },
  { id: "environment", label: "Environment", icon: Info },
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

const inputCls = "w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm";
const cardCls = "rounded-xl bg-surface border border-border p-5 space-y-4";

/** Defined here rather than inside the page.
 *
 * A component declared in a render body is a new component type on every
 * render, so React unmounts and remounts its subtree each time — which throws
 * away focus and any state it holds. These have neither today, and a switch
 * that loses focus mid-keyboard-navigation is the bug that would appear the
 * moment one of them grew some. */
function Toggle({ on, onClick, label }: { on: boolean; onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      onClick={onClick}
      className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${on ? "bg-primary" : "bg-border"}`}
    >
      <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${on ? "translate-x-5" : "translate-x-0"}`} />
    </button>
  );
}

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

function Code({ children }: { children: React.ReactNode }) {
  return <code className="px-2 py-0.5 rounded bg-bg font-mono text-xs break-all">{children}</code>;
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
const THEMES: { id: ThemeMode; label: string; hint: string }[] = [
  { id: "system", label: "System", hint: "Follow the operating system" },
  { id: "light", label: "Light", hint: "Always light" },
  { id: "dark", label: "Dark", hint: "Always dark" },
];

function ThemePicker() {
  const [mode, setMode] = useState<ThemeMode>(getTheme);

  const choose = (next: ThemeMode) => {
    setMode(next);
    // Persisted to localStorage — a per-browser preference, not something the
    // control plane has an opinion about.
    setTheme(next);
  };

  return (
    <div className="grid grid-cols-3 gap-2">
      {THEMES.map(({ id, label, hint }) => {
        const active = mode === id;
        return (
          <button
            key={id}
            type="button"
            onClick={() => choose(id)}
            aria-pressed={active}
            title={hint}
            className={`flex flex-col items-center gap-2 px-3 py-4 rounded-lg border transition-colors ${
              active
                ? "border-primary bg-primary/10 text-primary"
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

/** The field the environment owns, marked as such. */
function EnvBadge() {
  return (
    <span className="inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-warning/15 text-warning font-normal ml-1.5">
      <Lock size={10} />env
    </span>
  );
}

export default function SettingsPage() {
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

  // Engine registry state
  const { data: engineData, refetch: refetchEngines, loading: enginesLoading } = useQuery(fetchEngines);
  const [refreshingEngines, setRefreshingEngines] = useState(false);

  // Network discovery state
  const [discoveryResult, setDiscoveryResult] = useState<DiscoveryResult | null>(null);
  const [validationResult, setValidationResult] = useState<ValidationResult | null>(null);
  const [discoveryLoading, setDiscoveryLoading] = useState(false);
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);

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
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to save settings" });
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
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to save token" });
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
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to clear token" });
    }
  };

  const handleDiscover = async () => {
    setDiscoveryError(null);
    setDiscoveryLoading(true);
    try {
      const response = await runDiscovery();
      setDiscoveryResult(response.detected);
      setValidationResult(response.validation);
    } catch (e) {
      setDiscoveryError(e instanceof Error ? e.message : "Discovery failed");
    } finally {
      setDiscoveryLoading(false);
    }
  };

  const handleRefreshEngines = async () => {
    setRefreshingEngines(true);
    try {
      await refreshEngines();
      await refetchEngines();
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Engine refresh failed" });
    } finally {
      setRefreshingEngines(false);
    }
  };

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-primary" size={32} /></div>;
  if (error) return <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3"><AlertCircle size={20} /><span>{error}</span></div>;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">Settings</h2>
        <p className="text-text-muted mt-1">
          What every deployment inherits, and how this control plane is configured.
        </p>
      </div>

      {/* ── Tabs ─────────────────────────────────────────────────────────── */}
      <div className="flex gap-1 border-b border-border overflow-x-auto" role="tablist">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            onClick={() => selectTab(id)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors whitespace-nowrap ${
              tab === id ? "border-primary text-primary" : "border-transparent text-text-muted hover:text-text"
            }`}
          >
            <span className="flex items-center gap-1.5"><Icon size={16} />{label}</span>
          </button>
        ))}
      </div>

      {/* ── Deployment ───────────────────────────────────────────────────── */}
      {tab === "deployment" && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
          <div className={cardCls}>
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <Server size={16} className="text-primary" />
              <h3 className="font-semibold">Deployment Defaults</h3>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Default container</label>
              <input type="text" value={String(form.default_container ?? "vllm-node")} onChange={(e) => setForm({ ...form, default_container: e.target.value })} className={inputCls} />
              <p className="text-xs text-text-muted mt-1">Legacy recipes name a container tag rather than an engine; this is the one used when they do not.</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">GPU memory utilization</label>
              <input type="number" step="0.05" min="0.1" max="1.0" value={Number(form.default_gpu_mem_util ?? 0.8)} onChange={(e) => setForm({ ...form, default_gpu_mem_util: parseFloat(e.target.value) || 0.8 })} className={inputCls} />
              <p className="text-xs text-text-muted mt-1">Fraction of GPU VRAM allocated per deployment (0.1 – 1.0).</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Port range</label>
              <div className="flex items-center gap-2">
                <input aria-label="Port range start" type="number" value={Number(form.default_port_range_start ?? 9000)} onChange={(e) => setForm({ ...form, default_port_range_start: parseInt(e.target.value) || 9000 })} className={inputCls} placeholder="9000" />
                <span className="text-text-muted shrink-0 text-sm">–</span>
                <input aria-label="Port range end" type="number" value={Number(form.default_port_range_end ?? 9100)} onChange={(e) => setForm({ ...form, default_port_range_end: parseInt(e.target.value) || 9100 })} className={inputCls} placeholder="9100" />
              </div>
              <p className="text-xs text-text-muted mt-1">A deployment that does not ask for a port is given a free one from this range.</p>
            </div>

            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-sm font-medium">spark-vllm-docker path</label>
                {isEnvManaged("spark_vllm_path") && <EnvBadge />}
              </div>
              <input type="text" value={String(form.spark_vllm_path ?? "")} onChange={(e) => setForm({ ...form, spark_vllm_path: e.target.value })} disabled={isEnvManaged("spark_vllm_path")} className={`${inputCls} disabled:opacity-40 disabled:cursor-not-allowed`} placeholder="/path/to/spark-vllm-docker" />
              <p className="text-xs text-text-muted mt-1">{isEnvManaged("spark_vllm_path") ? "Controlled by SPARK_VLLM_PATH environment variable." : "Optional. Recipes are served from the bundled set and the OCI registry whether or not this is set."}</p>
            </div>
          </div>

          <div className={cardCls}>
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <SettingsIcon size={16} className="text-primary" />
              <h3 className="font-semibold">Timeouts and History</h3>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Readiness timeout</label>
              <div className="flex items-center gap-2">
                <input type="number" min="30" max="7200" value={Number(form.deploy_ready_timeout_seconds ?? 600)} onChange={(e) => setForm({ ...form, deploy_ready_timeout_seconds: parseInt(e.target.value) || 600 })} className="w-28 px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" />
                <span className="text-sm text-text-muted">seconds</span>
              </div>
              <p className="text-xs text-text-muted mt-1">How long a deployment may take to answer its readiness probe before it is called failed. A large model loading from cold disk is slow.</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Image pull stall timeout</label>
              <div className="flex items-center gap-2">
                <input type="number" min="30" max="3600" value={Number(form.docker_pull_stall_timeout_seconds ?? 300)} onChange={(e) => setForm({ ...form, docker_pull_stall_timeout_seconds: parseInt(e.target.value) || 300 })} className="w-28 px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" />
                <span className="text-sm text-text-muted">seconds</span>
              </div>
              <p className="text-xs text-text-muted mt-1">A pull making no progress for this long is abandoned rather than left hanging. Measured from the last byte, not from the start.</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Deployment history retention</label>
              <div className="flex items-center gap-2">
                <input type="number" min="0" max="365" value={Number(form.job_retention_days ?? 7)} onChange={(e) => setForm({ ...form, job_retention_days: parseInt(e.target.value) || 0 })} className="w-24 px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" />
                <span className="text-sm text-text-muted">days</span>
              </div>
              <p className="text-xs text-text-muted mt-1">Stopped and failed deployments older than this are removed automatically. 0 = keep forever.</p>
            </div>

            <div className="flex items-center justify-between pt-4 border-t border-border">
              <div>
                <p className="text-sm font-medium">Benchmarking</p>
                <p className="text-xs text-text-muted mt-0.5">Show the Benchmarking page and its API.</p>
              </div>
              <Toggle on={!!form.benchmarking_enabled} onClick={() => setForm({ ...form, benchmarking_enabled: !form.benchmarking_enabled })} label="Benchmarking" />
            </div>
          </div>
        </div>
      )}

      {/* ── Containers ───────────────────────────────────────────────────── */}
      {tab === "containers" && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
          <div className={cardCls}>
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <Box size={16} className="text-primary" />
              <h3 className="font-semibold">Container Limits</h3>
            </div>
            <p className="text-xs text-text-muted -mt-2">
              Applied on top of the engine's own profile, so a value here overrides what the
              engine asked for. Leave a limit empty to let the engine decide.
            </p>

            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium">Privileged mode</p>
                <p className="text-xs text-text-muted mt-0.5">Full host access. The GPU devices need it; nothing else here does.</p>
              </div>
              <Toggle on={dockerCfg.privileged !== false} onClick={() => setDocker("privileged", dockerCfg.privileged === false)} label="Privileged mode" />
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Memory limit (GB)</label>
              <input type="number" min="0" step="1" value={dockerCfg.memory_limit_gb ?? ""} onChange={(e) => setDocker("memory_limit_gb", e.target.value === "" ? null : parseFloat(e.target.value))} className={inputCls} placeholder="no limit" />
              <p className="text-xs text-text-muted mt-1">Empty means no limit — which is not the same as a limit of zero.</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Memory + swap limit (GB)</label>
              <input type="number" min="0" step="1" value={dockerCfg.memory_swap_limit_gb ?? ""} onChange={(e) => setDocker("memory_swap_limit_gb", e.target.value === "" ? null : parseFloat(e.target.value))} className={inputCls} placeholder="no limit" />
              <p className="text-xs text-text-muted mt-1">Total of memory and swap. Set it equal to the memory limit to forbid swapping.</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Shared memory (GB)</label>
              <input type="number" min="1" step="1" value={Number(dockerCfg.shm_size_gb ?? 64)} onChange={(e) => setDocker("shm_size_gb", parseInt(e.target.value) || 64)} className={inputCls} placeholder="64" />
              <p className="text-xs text-text-muted mt-1"><code className="font-mono">/dev/shm</code>. Tensor-parallel workers pass tensors through it; too small shows up as a hang, not an error.</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Process limit</label>
              <input type="number" min="64" step="64" value={Number(dockerCfg.pids_limit ?? 4096)} onChange={(e) => setDocker("pids_limit", parseInt(e.target.value) || 4096)} className={inputCls} placeholder="4096" />
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Open file limit</label>
              <input type="number" min="1024" step="1024" value={Number(dockerCfg.nofile_limit ?? 1048576)} onChange={(e) => setDocker("nofile_limit", parseInt(e.target.value) || 1048576)} className={inputCls} placeholder="1048576" />
            </div>
          </div>

          <div className="space-y-4">
            <div className={cardCls}>
              <div className="flex items-center gap-2 pb-3 border-b border-border">
                <Box size={16} className="text-primary" />
                <h3 className="font-semibold">Caches and Entrypoint</h3>
              </div>

              <div>
                <label className="block text-sm font-medium mb-1">Cache directories</label>
                <textarea
                  aria-label="Cache directories"
                  rows={4}
                  value={(dockerCfg.cache_dirs ?? []).join("\n")}
                  onChange={(e) => setDocker("cache_dirs", e.target.value.split("\n").map((l) => l.trim()).filter(Boolean))}
                  className={`${inputCls} resize-y`}
                  placeholder="~/.cache/vllm"
                />
                <p className="text-xs text-text-muted mt-1">One per line. Mounted into every container, so a compiled kernel survives the container that built it — the difference between a warm start and several minutes of recompilation.</p>
              </div>

              <div className="flex items-center justify-between pt-4 border-t border-border">
                <div>
                  <p className="text-sm font-medium">Keep the image entrypoint</p>
                  <p className="text-xs text-text-muted mt-0.5">Off means the launch script runs as PID 1, which is what puts the engine's output in the deployment log.</p>
                </div>
                <Toggle on={!!dockerCfg.keep_entrypoint} onClick={() => setDocker("keep_entrypoint", !dockerCfg.keep_entrypoint)} label="Keep the image entrypoint" />
              </div>
            </div>

            <div className={cardCls}>
              <div className="flex items-center gap-2 pb-3 border-b border-border">
                <ShieldCheck size={16} className="text-primary" />
                <h3 className="font-semibold">Mods</h3>
              </div>
              <div>
                <label className="block text-sm font-medium mb-1">Network access policy</label>
                <select
                  aria-label="Mod network access policy"
                  value={String(modCfg.network_policy ?? "warn")}
                  onChange={(e) => setForm({ ...form, mod: { ...modCfg, network_policy: e.target.value } })}
                  className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none text-sm"
                >
                  <option value="allow">Allow — mods may reach the network</option>
                  <option value="warn">Warn — allowed, but reported</option>
                  <option value="deny">Deny — refuse a mod that reaches out</option>
                </select>
                <p className="text-xs text-text-muted mt-1">A mod is a shell script that runs inside the container before the engine starts. One that downloads something is installing code nobody reviewed.</p>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── Cluster ──────────────────────────────────────────────────────── */}
      {tab === "cluster" && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
          <div className={cardCls}>
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <Network size={16} className="text-primary" />
              <h3 className="font-semibold">Multi-node</h3>
            </div>

            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium">Cluster mode</p>
                <p className="text-xs text-text-muted mt-0.5">Allow recipes that need more than one node.</p>
              </div>
              <Toggle on={!!form.cluster_enabled} onClick={() => setForm({ ...form, cluster_enabled: !form.cluster_enabled })} label="Cluster mode" />
            </div>

            {environment?.cluster_experimental && (
              <p className="text-xs text-warning flex items-start gap-1.5 pt-3 border-t border-border">
                <AlertCircle size={13} className="shrink-0 mt-0.5" />
                <span>Multi-node is marked experimental in this build. It is implemented against the upstream reference but has not been proven on real hardware here.</span>
              </p>
            )}

            <p className="text-xs text-text-muted pt-3 border-t border-border">
              Nodes are enrolled and inspected on the Cluster page. There is nothing per-node to
              set here: interface pinning and node addresses belong to the node, not to this
              control plane.
            </p>
          </div>

          <div className={cardCls}>
            <div className="flex items-center justify-between pb-3 border-b border-border">
              <div className="flex items-center gap-2">
                <Radio size={16} className="text-primary" />
                <h3 className="font-semibold">Network Discovery</h3>
              </div>
              <button
                type="button"
                onClick={handleDiscover}
                disabled={discoveryLoading}
                className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50 text-white font-medium text-xs transition-colors flex items-center gap-1.5"
              >
                {discoveryLoading ? <Loader2 className="animate-spin" size={12} /> : <Radio size={12} />}
                Discover
              </button>
            </div>

            {discoveryError && (
              <div className="text-xs text-danger flex items-center gap-1.5">
                <AlertCircle size={12} />
                <span>{discoveryError}</span>
              </div>
            )}

            {discoveryResult && (
              <div className="space-y-3">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-text-muted">Local IP</span>
                  <Code>{discoveryResult.local_ip || "not detected"}</Code>
                </div>

                <div className="flex items-center justify-between text-sm">
                  <span className="text-text-muted">Ethernet</span>
                  <Code>{discoveryResult.ethernet_if || "not detected"}</Code>
                </div>

                <div className="flex items-center justify-between text-sm">
                  <span className="text-text-muted">InfiniBand</span>
                  <span className="flex items-center gap-1 text-xs">
                    {discoveryResult.infiniband_present ? (
                      <>
                        <Wifi size={12} className="text-success" />
                        <span className="text-success">{discoveryResult.infiniband_devices.length} HCA{discoveryResult.infiniband_devices.length > 1 ? "s" : ""}</span>
                      </>
                    ) : (
                      <>
                        <WifiOff size={12} className="text-text-muted" />
                        <span className="text-text-muted">not present</span>
                      </>
                    )}
                  </span>
                </div>

                {discoveryResult.infiniband_present && discoveryResult.infiniband_devices.length > 0 && (
                  <div className="text-xs text-text-muted space-y-0.5 pl-1">
                    {discoveryResult.infiniband_devices.map((dev) => (
                      <div key={dev.hca} className="flex items-center gap-1.5">
                        <span className="font-mono">{dev.hca}</span>
                        <span className={`px-1.5 py-0.5 rounded ${dev.state === "ACTIVE" ? "bg-success/15 text-success" : "bg-warning/15 text-warning"}`}>
                          {dev.state}
                        </span>
                        {dev.ports.length > 0 && <span>ports: {dev.ports.join(",")}</span>}
                      </div>
                    ))}
                  </div>
                )}

                {discoveryResult.nccl_defaults && (
                  <div className="pt-2 border-t border-border space-y-1.5">
                    <div className="flex items-center justify-between text-sm">
                      <span className="text-text-muted">NCCL socket</span>
                      <Code>{discoveryResult.nccl_defaults.socket_ifname}</Code>
                    </div>
                    <div className="flex items-center justify-between text-sm">
                      <span className="text-text-muted">NCCL IB HCA</span>
                      <Code>{discoveryResult.nccl_defaults.ib_hca || "none"}</Code>
                    </div>
                    <div className="flex items-center justify-between text-sm">
                      <span className="text-text-muted">NCCL IB disabled</span>
                      <span className={`text-xs font-medium ${discoveryResult.nccl_defaults.ib_disable ? "text-warning" : "text-success"}`}>
                        {discoveryResult.nccl_defaults.ib_disable ? "yes" : "no"}
                      </span>
                    </div>

                    <p className="text-xs text-text-muted pt-1">
                      Detected on this machine. Interface pinning is per node, not global: record these
                      on the node's entry in the Cluster page's registry, which is what a deploy reads.
                    </p>
                  </div>
                )}

                {validationResult && (
                  <div className={`pt-2 border-t border-border text-xs space-y-1 ${!validationResult.healthy ? "text-danger" : validationResult.warnings.length > 0 ? "text-warning" : "text-success"}`}>
                    <div className="flex items-center gap-1.5 font-medium">
                      {validationResult.healthy ? <Check size={12} /> : <AlertCircle size={12} />}
                      Network: {validationResult.healthy ? "Healthy" : "Issues found"}
                    </div>
                    {validationResult.warnings.map((w, i) => <div key={`w${i}`} className="pl-3.5">⚠ {w}</div>)}
                    {validationResult.errors.map((e, i) => <div key={`e${i}`} className="pl-3.5">✕ {e}</div>)}
                  </div>
                )}
              </div>
            )}

            {!discoveryResult && !discoveryLoading && (
              <p className="text-xs text-text-muted">Detects this machine's interfaces and the NCCL defaults they imply. Reports only — nothing is saved.</p>
            )}
          </div>
        </div>
      )}

      {/* ── Engines ──────────────────────────────────────────────────────── */}
      {tab === "engines" && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
          <div className={cardCls}>
            <div className="flex items-center justify-between pb-3 border-b border-border">
              <div className="flex items-center gap-2">
                <Cpu size={16} className="text-primary" />
                <h3 className="font-semibold">Engines</h3>
              </div>
              <button onClick={handleRefreshEngines} disabled={refreshingEngines} className="px-2.5 py-1 rounded-lg border border-border hover:border-primary/50 text-text-muted hover:text-text text-xs transition-colors flex items-center gap-1.5 disabled:opacity-50" title="Re-fetch the configured engine indexes">
                <RefreshCw size={13} className={refreshingEngines ? "animate-spin" : ""} />
                Refresh
              </button>
            </div>

            {enginesLoading && !engineData ? (
              <div className="flex justify-center py-4"><Loader2 className="animate-spin text-primary" size={20} /></div>
            ) : (
              <EngineList engines={engineData?.engines ?? []} defaultEngine={engineData?.default_engine ?? ""} />
            )}
          </div>

          <div className={cardCls}>
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <SettingsIcon size={16} className="text-primary" />
              <h3 className="font-semibold">Engine Registry</h3>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Default engine</label>
              <input type="text" value={String(form.default_engine ?? "vllm")} onChange={(e) => setForm({ ...form, default_engine: e.target.value })} className={inputCls} placeholder="vllm" />
              <p className="text-xs text-text-muted mt-1">Used when neither the deploy request nor the recipe names one.</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Indexes</label>
              <textarea
                aria-label="Engine indexes"
                rows={3}
                value={((form.engine_indexes ?? []) as string[]).join("\n")}
                onChange={(e) => setForm({ ...form, engine_indexes: e.target.value.split("\n").map((l) => l.trim()).filter(Boolean) })}
                className={`${inputCls} resize-y`}
                placeholder="https://…/engines.json"
              />
              <p className="text-xs text-text-muted mt-1">One URL per line. Where the engine list and its pinned image digests come from.</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Index cache lifetime</label>
              <div className="flex items-center gap-2">
                <input type="number" min="0" value={Number(form.engine_index_cache_ttl_seconds ?? 3600)} onChange={(e) => setForm({ ...form, engine_index_cache_ttl_seconds: parseInt(e.target.value) || 0 })} className="w-28 px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" />
                <span className="text-sm text-text-muted">seconds</span>
              </div>
              <p className="text-xs text-text-muted mt-1">How long a fetched index is reused before it is fetched again. Refresh above ignores it.</p>
            </div>
          </div>
        </div>
      )}

      {/* ── Preferences ──────────────────────────────────────────────────── */}
      {tab === "preferences" && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
          <div className={cardCls}>
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <Palette size={16} className="text-primary" />
              <h3 className="font-semibold">Appearance</h3>
            </div>
            <div>
              <label className="block text-sm font-medium mb-2">Colour theme</label>
              <ThemePicker />
              <p className="text-xs text-text-muted mt-2">
                Remembered in this browser. It is a preference of yours, not a setting of the
                control plane's — nothing here is sent to the server or shared with anyone else
                signing in.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* ── Secrets ──────────────────────────────────────────────────────── */}
      {tab === "secrets" && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
          <div className={cardCls}>
            <div className="flex items-center justify-between pb-3 border-b border-border">
              <div className="flex items-center gap-2">
                <KeyRound size={16} className="text-primary" />
                <h3 className="font-semibold">Secrets</h3>
              </div>
              <span className="text-xs text-text-muted px-2 py-0.5 rounded bg-bg border border-border font-mono">mode 600</span>
            </div>

            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-sm font-medium">HuggingFace token</label>
                {secrets?.hf_token && <span className="text-xs text-success font-mono">Active ···{secrets.hf_token.slice(-4)}</span>}
              </div>
              <div className="flex gap-2">
                <div className="relative flex-1">
                  <input type={showToken ? "text" : "password"} value={hfToken} onChange={(e) => setHfToken(e.target.value)} placeholder={secrets?.hf_token ? "Enter new token to replace…" : "hf_…"} className={`${inputCls} pr-9`} autoComplete="off" aria-label="HuggingFace token" />
                  <button type="button" onClick={() => setShowToken(v => !v)} aria-label={showToken ? "Hide token" : "Show token"} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-text-muted hover:text-text transition-colors">
                    {showToken ? <EyeOff size={15} /> : <Eye size={15} />}
                  </button>
                </div>
                <button onClick={handleSaveToken} disabled={savingToken || !hfToken.trim()} className="px-3 py-2 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50 text-white font-medium text-sm transition-colors flex items-center gap-1.5">
                  {savingToken ? <Loader2 className="animate-spin" size={14} /> : savedToken ? <Check size={14} /> : <KeyRound size={14} />}
                  {savedToken ? "Saved!" : "Save"}
                </button>
                {secrets?.hf_token && (
                  <button onClick={handleClearToken} className="px-3 py-2 rounded-lg border border-border hover:border-danger/50 hover:text-danger text-text-muted transition-colors" title="Clear token" aria-label="Clear token">
                    <Trash2 size={15} />
                  </button>
                )}
              </div>
              <p className="text-xs text-text-muted mt-1.5">
                Passed as <code className="font-mono">HF_TOKEN</code> when launching a deployment and used to fetch gated models. Stored in
                <code className="font-mono"> ~/.config/spark-pulse/secrets.json</code>; it is never sent back to this page.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* ── Environment ──────────────────────────────────────────────────── */}
      {tab === "environment" && environment && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
          <div className={cardCls}>
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <Info size={16} className="text-primary" />
              <h3 className="font-semibold">Runtime</h3>
            </div>
            <p className="text-xs text-text-muted -mt-2">
              How this process is configured. Read-only here on purpose: these are set in
              <code className="font-mono"> settings.json</code> or the environment, and a browser that
              could change them would be a way past every other check.
            </p>

            <div>
              <Fact label="State store" hint="Deployments, nodes, sessions and the enrolment ledger." value={<Code>{environment.database_url || `${environment.database_backend} (default path)`}</Code>} />
              <Fact label="Runtime" hint="How deployments are launched." value={<Code>{String(form.runtime ?? "native")}</Code>} />
              <Fact label="Web UI port" value={<Code>{String(form.webui_port ?? "")}</Code>} />
              <Fact label="Worker threads" hint="Blocking work is offloaded here rather than run on the event loop." value={<Code>{environment.thread_pool_size}</Code>} />
              {environment.image_registry?.mode && (
                <Fact
                  label="Image registry"
                  hint={environment.image_registry.mode === "proxy"
                    ? `Pull-through cache of ${environment.image_registry.upstream || "the upstream"}. Worker nodes pull from here rather than from the internet.`
                    : "A full registry on this node. Worker nodes pull from here rather than from the internet."}
                  value={<Code>{`${environment.image_registry.mode} · ${environment.image_registry.address}:${environment.image_registry.port}`}</Code>}
                />
              )}
            </div>
          </div>

          <div className={cardCls}>
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <ShieldCheck size={16} className="text-primary" />
              <h3 className="font-semibold">Access</h3>
            </div>

            <div>
              <Fact
                label="Authentication"
                hint={environment.auth_enabled ? "OIDC sign-in is required." : "This API answers every caller that can reach it. The browser boundary is the origin check."}
                value={
                  <span className={`text-xs font-medium ${environment.auth_enabled ? "text-success" : "text-warning"}`}>
                    {environment.auth_enabled ? "enabled" : "disabled"}
                  </span>
                }
              />
              {environment.auth_enabled && environment.oidc_provider_url && (
                <Fact label="Identity provider" value={<Code>{environment.oidc_provider_url}</Code>} />
              )}
              <Fact
                label="External URL"
                hint="Pins the OIDC redirect. Empty means it is worked out from the request, which trusts the Host header."
                value={<Code>{environment.external_url || "not pinned"}</Code>}
              />
              <Fact
                label="Allowed origins"
                hint="Browser origins that may call this API. Never a wildcard."
                value={
                  <div className="space-y-0.5">
                    {environment.cors_allowed_origins.map((o) => <div key={o}><Code>{o}</Code></div>)}
                  </div>
                }
              />
              <Fact
                label="MCP endpoint"
                hint="Model Context Protocol, on this same app and behind the same auth."
                value={environment.mcp_enabled ? <Code>{environment.mcp_path}</Code> : <span className="text-xs text-text-muted">disabled</span>}
              />
            </div>
          </div>
        </div>
      )}

      {/* ── Save ─────────────────────────────────────────────────────────── */}
      {tab !== "secrets" && tab !== "environment" && tab !== "preferences" && (
        <div className="flex items-center gap-3">
          <button onClick={handleSave} disabled={saving || !isDirty} className="px-4 py-2 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-40 disabled:cursor-not-allowed text-white font-medium text-sm transition-colors flex items-center gap-2">
            {saving ? <Loader2 className="animate-spin" size={14} /> : saved ? <Check size={14} /> : <SettingsIcon size={14} />}
            {saving ? "Saving…" : saved ? "Saved!" : "Save settings"}
          </button>
          {/* One form across every tab, so this saves edits made on any of
              them — including a tab the operator has since navigated away
              from. Saying so beats a button that silently does more than it
              appears to. */}
          {isDirty && !saved && <span className="text-xs text-text-muted">Unsaved changes on this page.</span>}
          {saved && <span className="text-xs text-success">Saved to <code className="font-mono">~/.config/spark-pulse/settings.json</code></span>}
        </div>
      )}

      {alertModal && <AlertModal open={!!alertModal} onClose={() => setAlertModal(null)} title={alertModal.title} message={alertModal.message} />}
    </div>
  );
}
