import { useState, useEffect } from "react";
import { fetchSettings, updateSettings, fetchSecrets, saveSecrets, deleteSecret, fetchGitUpdateStatus, triggerGitFetch, triggerGitPull, runDiscovery, applyNcclDefaults, type DiscoveryResult, type ValidationResult } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { Settings as SettingsIcon, Loader2, AlertCircle, Check, KeyRound, Eye, EyeOff, Trash2, Lock, Server, Clock, GitBranch, GitCommit, ArrowDownUp, Network, Radio, Wifi, WifiOff } from "lucide-react";
import { AlertModal } from "@/components/Modal";
import { HealthMonitorControls } from "@/components/HealthBadge";
import { setRefresh } from "@/lib/refresh";

export default function SettingsPage() {
  const { data: settings, loading, error, refetch } = useQuery(fetchSettings);
  const { data: secrets, refetch: refetchSecrets } = useQuery(fetchSecrets);
  const [form, setForm] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);

  const envManaged = (settings?.env_managed ?? []) as string[];
  const isEnvManaged = (field: string) => envManaged.includes(field);
  const EnvBadge = () => (
    <span className="inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded bg-warning/15 text-warning font-normal ml-1.5">
      <Lock size={10} />env
    </span>
  );

  // ── Docker / NCCL helpers ──────────────────────────────────────────────
  const dockerCfg = form.docker as Record<string, unknown> | undefined;
  const ncclCfg = form.nccl as Record<string, unknown> | undefined;
  const getDocker = <K extends keyof NonNullable<typeof dockerCfg>>(key: K, def: NonNullable<typeof dockerCfg>[K]) =>
    (dockerCfg?.[key] ?? def) as NonNullable<typeof dockerCfg>[K];
  const getNccl = <K extends keyof NonNullable<typeof ncclCfg>>(key: K, def: NonNullable<typeof ncclCfg>[K]) =>
    (ncclCfg?.[key] ?? def) as NonNullable<typeof ncclCfg>[K];
  const setDocker = <K extends keyof NonNullable<typeof dockerCfg>>(key: K, val: NonNullable<typeof dockerCfg>[K]) =>
    setForm({ ...form, docker: { ...(dockerCfg ?? {}), [key]: val } });
  const setNccl = <K extends keyof NonNullable<typeof ncclCfg>>(key: K, val: NonNullable<typeof ncclCfg>[K]) =>
    setForm({ ...form, nccl: { ...(ncclCfg ?? {}), [key]: val } });

  // HF Token state
  const [hfToken, setHfToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [savingToken, setSavingToken] = useState(false);
  const [savedToken, setSavedToken] = useState(false);

  // Git update state
  const { data: gitStatus, refetch: refetchGitStatus, loading: gitLoading } = useQuery(fetchGitUpdateStatus);
  const [gitActionLoading, setGitActionLoading] = useState<string | null>(null);
  const [gitError, setGitError] = useState<string | null>(null);

  // Network discovery state
  const [discoveryResult, setDiscoveryResult] = useState<DiscoveryResult | null>(null);
  const [validationResult, setValidationResult] = useState<ValidationResult | null>(null);
  const [discoveryLoading, setDiscoveryLoading] = useState(false);

  // Health monitoring state
  const [isHealthMonitoring, setIsHealthMonitoring] = useState(false);
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);
  const [applyingNccl, setApplyingNccl] = useState(false);

  const isDirty = settings != null && Object.keys(form).some(
    (k) => JSON.stringify(form[k]) !== JSON.stringify((settings as unknown as Record<string, unknown>)[k])
  );

  useEffect(() => { setRefresh(refetch); }, [refetch]);
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

  const handleGitFetch = async () => {
    setGitError(null);
    setGitActionLoading("fetch");
    try {
      await triggerGitFetch();
      await refetchGitStatus();
    } catch (e) {
      setGitError(e instanceof Error ? e.message : "Fetch failed");
    } finally {
      setGitActionLoading(null);
    }
  };

  const handleGitPull = async () => {
    setGitError(null);
    setGitActionLoading("pull");
    try {
      await triggerGitPull();
      await refetchGitStatus();
    } catch (e) {
      setGitError(e instanceof Error ? e.message : "Pull failed");
    } finally {
      setGitActionLoading(null);
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

  const handleApplyNccl = async () => {
    if (!discoveryResult?.nccl_defaults) return;
    setApplyingNccl(true);
    try {
      await applyNcclDefaults({
        socket_ifname: discoveryResult.nccl_defaults.socket_ifname,
        ib_hca: discoveryResult.nccl_defaults.ib_hca,
        ib_disable: discoveryResult.nccl_defaults.ib_disable,
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
      refetch();
    } catch (e) {
      setAlertModal({ title: "Error", message: e instanceof Error ? e.message : "Failed to apply NCCL defaults" });
    } finally {
      setApplyingNccl(false);
    }
  };

  const inputCls = "w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm";

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-primary" size={32} /></div>;
  if (error) return <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3"><AlertCircle size={20} /><span>{error}</span></div>;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">Settings</h2>
        <p className="text-text-muted mt-1">Configure Spark Manager and spark-vllm-docker integration</p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">

        {/* ── Left: Deployment Defaults ── */}
        <div className="rounded-xl bg-surface border border-border p-5 space-y-4">
          <div className="flex items-center gap-2 pb-3 border-b border-border">
            <Server size={16} className="text-primary" />
            <h3 className="font-semibold">Deployment Defaults</h3>
          </div>

          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-sm font-medium">spark-vllm-docker path</label>
              {isEnvManaged("spark_vllm_path") && <EnvBadge />}
            </div>
            <input type="text" value={String(form.spark_vllm_path ?? "")} onChange={(e) => setForm({ ...form, spark_vllm_path: e.target.value })} disabled={isEnvManaged("spark_vllm_path")} className={`${inputCls} disabled:opacity-40 disabled:cursor-not-allowed`} placeholder="/path/to/spark-vllm-docker" />
            <p className="text-xs text-text-muted mt-1">{isEnvManaged("spark_vllm_path") ? "Controlled by SPARK_VLLM_PATH environment variable." : "Absolute path to the spark-vllm-docker installation."}</p>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">Default container</label>
            <input type="text" value={String(form.default_container ?? "vllm-node")} onChange={(e) => setForm({ ...form, default_container: e.target.value })} className={inputCls} />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">GPU memory utilization</label>
            <input type="number" step="0.05" min="0.1" max="1.0" value={Number(form.default_gpu_mem_util ?? 0.8)} onChange={(e) => setForm({ ...form, default_gpu_mem_util: parseFloat(e.target.value) || 0.8 })} className={inputCls} />
            <p className="text-xs text-text-muted mt-1">Fraction of GPU VRAM allocated per deployment (0.1 – 1.0).</p>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">Port range</label>
            <div className="flex items-center gap-2">
              <input type="number" value={Number(form.default_port_range_start ?? 9000)} onChange={(e) => setForm({ ...form, default_port_range_start: parseInt(e.target.value) || 9000 })} className={inputCls} placeholder="9000" />
              <span className="text-text-muted shrink-0 text-sm">–</span>
              <input type="number" value={Number(form.default_port_range_end ?? 9100)} onChange={(e) => setForm({ ...form, default_port_range_end: parseInt(e.target.value) || 9100 })} className={inputCls} placeholder="9100" />
            </div>
          </div>

          <div className="flex items-center gap-3 pt-4 border-t border-border">
            <button onClick={handleSave} disabled={saving || !isDirty} className="px-4 py-2 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-40 disabled:cursor-not-allowed text-white font-medium text-sm transition-colors flex items-center gap-2">
              {saving ? <Loader2 className="animate-spin" size={14} /> : saved ? <Check size={14} /> : <SettingsIcon size={14} />}
              {saving ? "Saving…" : saved ? "Saved!" : "Save settings"}
            </button>
            {saved && <span className="text-xs text-success">Saved to <code className="font-mono">~/.config/spark-pulse/settings.json</code></span>}
          </div>
        </div>

        {/* ── Docker Config ── */}
        <div className="rounded-xl bg-surface border border-border p-5 space-y-4">
          <div className="flex items-center gap-2 pb-3 border-b border-border">
            <Server size={16} className="text-primary" />
            <h3 className="font-semibold">Docker</h3>
          </div>

          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium">Privileged mode</p>
              <p className="text-xs text-text-muted mt-0.5">Grant full host access (needed for GPU devices). Less secure but simpler.</p>
            </div>
            <button type="button" onClick={() => setDocker("privileged", !getDocker("privileged", true) as boolean)} className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${getDocker("privileged", true) ? "bg-primary" : "bg-border"}`}>
              <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${getDocker("privileged", true) ? "translate-x-5" : "translate-x-0"}`} />
            </button>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">Memory limit (GB)</label>
            <input type="number" min="1" step="1" value={Number(getDocker("memory_limit_gb", 110))} onChange={(e) => setDocker("memory_limit_gb", parseInt(e.target.value) || 110)} className={inputCls} placeholder="110" />
            <p className="text-xs text-text-muted mt-1">Container memory limit. Set to 0 to disable.</p>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">SHM size (GB)</label>
            <input type="number" min="1" step="1" value={Number(getDocker("shm_size_gb", 64))} onChange={(e) => setDocker("shm_size_gb", parseInt(e.target.value) || 64)} className={inputCls} placeholder="64" />
            <p className="text-xs text-text-muted mt-1">/dev/shm size for shared memory.</p>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">PID limit</label>
            <input type="number" min="64" step="64" value={Number(getDocker("pids_limit", 4096))} onChange={(e) => setDocker("pids_limit", parseInt(e.target.value) || 4096)} className={inputCls} placeholder="4096" />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">NCCL socket interface</label>
            <input type="text" value={String(getNccl("socket_ifname", "") || "")} onChange={(e) => setNccl("socket_ifname", e.target.value || null)} className={inputCls} placeholder="auto-detect" />
            <p className="text-xs text-text-muted mt-1">Leave empty to auto-detect. E.g. eth0, enp3s0.</p>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">NCCL InfiniBand HCA</label>
            <input type="text" value={String(getNccl("ib_hca", "") || "")} onChange={(e) => setNccl("ib_hca", e.target.value || null)} className={inputCls} placeholder="auto-detect" />
            <p className="text-xs text-text-muted mt-1">InfiniBand HCA selector. E.g. GPU,mlx5_*. Leave empty for default.</p>
          </div>

          {/* ── Health Monitoring ── */}
          <div className="pt-4 border-t border-border space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <AlertCircle size={16} className="text-primary" />
                <h4 className="font-semibold text-sm">Health Monitoring</h4>
              </div>
            </div>
            <p className="text-xs text-text-muted">Enable continuous health monitoring for deployments and clusters.</p>
            <HealthMonitorControls
              isMonitoring={isHealthMonitoring}
              onToggle={() => setIsHealthMonitoring(!isHealthMonitoring)}
            />
          </div>

          {/* ── Network Discovery ── */}
          <div className="pt-4 border-t border-border space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Network size={16} className="text-primary" />
                <h4 className="font-semibold text-sm">Network Discovery</h4>
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
                {/* Local IP */}
                <div className="flex items-center justify-between text-sm">
                  <span className="text-text-muted">Local IP</span>
                  <code className="px-2 py-0.5 rounded bg-bg font-mono text-xs">
                    {discoveryResult.local_ip || <span className="text-text-muted">not detected</span>}
                  </code>
                </div>

                {/* Ethernet interface */}
                <div className="flex items-center justify-between text-sm">
                  <span className="text-text-muted">Ethernet</span>
                  <code className="px-2 py-0.5 rounded bg-bg font-mono text-xs">
                    {discoveryResult.ethernet_if || <span className="text-text-muted">not detected</span>}
                  </code>
                </div>

                {/* InfiniBand */}
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

                {/* IB devices detail */}
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

                {/* NCCL defaults */}
                {discoveryResult.nccl_defaults && (
                  <div className="pt-2 border-t border-border space-y-1.5">
                    <div className="flex items-center justify-between text-sm">
                      <span className="text-text-muted">NCCL socket</span>
                      <code className="px-2 py-0.5 rounded bg-bg font-mono text-xs">{discoveryResult.nccl_defaults.socket_ifname}</code>
                    </div>
                    <div className="flex items-center justify-between text-sm">
                      <span className="text-text-muted">NCCL IB HCA</span>
                      <code className="px-2 py-0.5 rounded bg-bg font-mono text-xs">
                        {discoveryResult.nccl_defaults.ib_hca || "none"}
                      </code>
                    </div>
                    <div className="flex items-center justify-between text-sm">
                      <span className="text-text-muted">NCCL IB disabled</span>
                      <span className={`text-xs font-medium ${discoveryResult.nccl_defaults.ib_disable ? "text-warning" : "text-success"}`}>
                        {discoveryResult.nccl_defaults.ib_disable ? "yes" : "no"}
                      </span>
                    </div>

                    {/* Apply button */}
                    <button
                      type="button"
                      onClick={handleApplyNccl}
                      disabled={applyingNccl}
                      className="w-full mt-2 px-3 py-2 rounded-lg border border-primary/50 hover:border-primary text-primary hover:bg-primary/5 disabled:opacity-50 text-xs font-medium transition-colors flex items-center justify-center gap-1.5"
                    >
                      {applyingNccl ? <Loader2 className="animate-spin" size={12} /> : <Check size={12} />}
                      Apply detected NCCL settings
                    </button>
                  </div>
                )}

                {/* Validation */}
                {validationResult && (
                  <div className={`pt-2 border-t border-border text-xs space-y-1 ${!validationResult.healthy ? "text-danger" : validationResult.warnings.length > 0 ? "text-warning" : "text-success"}`}>
                    <div className="flex items-center gap-1.5 font-medium">
                      {validationResult.healthy ? <Check size={12} /> : <AlertCircle size={12} />}
                      Network: {validationResult.healthy ? "Healthy" : "Issues found"}
                    </div>
                    {validationResult.warnings.length > 0 && (
                      <div className="pl-3.5 space-y-0.5">
                        {validationResult.warnings.map((w, i) => (
                          <div key={i}>⚠ {w}</div>
                        ))}
                      </div>
                    )}
                    {validationResult.errors.length > 0 && (
                      <div className="pl-3.5 space-y-0.5">
                        {validationResult.errors.map((e, i) => (
                          <div key={i}>✕ {e}</div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}

            {!discoveryResult && !discoveryLoading && (
              <p className="text-xs text-text-muted">Click "Discover" to detect network interfaces and generate NCCL defaults.</p>
            )}
          </div>
        </div>

        {/* ── Right column ── */}
        <div className="space-y-4">

          {/* Job History */}
          <div className="rounded-xl bg-surface border border-border p-5 space-y-4">
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <Clock size={16} className="text-primary" />
              <h3 className="font-semibold">Job History</h3>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Retention</label>
              <div className="flex items-center gap-2">
                <input type="number" min="0" max="365" value={Number(form.job_retention_days ?? 7)} onChange={(e) => setForm({ ...form, job_retention_days: parseInt(e.target.value) || 0 })} className="w-24 px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" />
                <span className="text-sm text-text-muted">days</span>
              </div>
              <p className="text-xs text-text-muted mt-1">Stopped and failed deployments older than this are removed automatically. 0 = keep forever.</p>
            </div>

            <div className="flex items-center justify-between pt-4 border-t border-border">
              <div>
                <p className="text-sm font-medium">Cluster mode</p>
                <p className="text-xs text-text-muted mt-0.5">Allow launching cluster-only recipes.</p>
              </div>
              <button type="button" onClick={() => setForm({ ...form, cluster_enabled: !form.cluster_enabled })} className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${form.cluster_enabled ? "bg-primary" : "bg-border"}`}>
                <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${form.cluster_enabled ? "translate-x-5" : "translate-x-0"}`} />
              </button>
            </div>
          </div>

          {/* Cluster Config */}
          <div className="rounded-xl bg-surface border border-border p-5 space-y-4">
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <Server size={16} className="text-primary" />
              <h3 className="font-semibold">Cluster Orchestration</h3>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Default cluster image</label>
              <input type="text" value={String(getDocker("cluster_image", "eugr/spark-vllm-docker:latest"))} onChange={(e) => setDocker("cluster_image", e.target.value)} className={inputCls} placeholder="eugr/spark-vllm-docker:latest" />
              <p className="text-xs text-text-muted mt-1">Docker image used for cluster nodes.</p>
            </div>

            <div>
              <label className="block text-sm font-medium mb-1">Ray head port</label>
              <input type="number" min="1024" max="65535" value={Number(getDocker("ray_port", 29501))} onChange={(e) => setDocker("ray_port", parseInt(e.target.value) || 29501)} className={inputCls} placeholder="29501" />
              <p className="text-xs text-text-muted mt-1">Port for Ray head communication.</p>
            </div>

            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium">Default GPU count per node</p>
                <p className="text-xs text-text-muted mt-0.5">Used for cluster capacity validation.</p>
              </div>
              <input type="number" min="1" max="8" value={Number(getDocker("gpu_count", 8))} onChange={(e) => setDocker("gpu_count", parseInt(e.target.value) || 8)} className="w-20 px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm text-center" />
            </div>

            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium">Enable cluster mode</p>
                <p className="text-xs text-text-muted mt-0.5">Allow multi-node cluster deployments.</p>
              </div>
              <button type="button" onClick={() => setDocker("cluster_enabled", !getDocker("cluster_enabled", false) as boolean)} className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${getDocker("cluster_enabled", false) ? "bg-primary" : "bg-border"}`}>
                <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${getDocker("cluster_enabled", false) ? "translate-x-5" : "translate-x-0"}`} />
              </button>
            </div>
          </div>

          {/* Git Auto-Update */}
          <div className="rounded-xl bg-surface border border-border p-5 space-y-4">
            <div className="flex items-center gap-2 pb-3 border-b border-border">
              <GitBranch size={16} className="text-primary" />
              <h3 className="font-semibold">Git Auto-Update</h3>
            </div>

            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium">Enable auto-update checks</p>
                <p className="text-xs text-text-muted mt-0.5">Periodically check for new commits in spark-vllm-docker.</p>
              </div>
              <button type="button" onClick={() => setForm({ ...form, git_update_enabled: !form.git_update_enabled })} className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${form.git_update_enabled ? "bg-primary" : "bg-border"}`}>
                <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${form.git_update_enabled ? "translate-x-5" : "translate-x-0"}`} />
              </button>
            </div>

            <div className="flex items-center gap-2">
              <label className="text-sm font-medium whitespace-nowrap">Check interval</label>
              <input
                type="number"
                min="60"
                max="86400"
                step="60"
                value={Number(form.git_update_check_interval_seconds ?? 3600)}
                onChange={(e) => setForm({ ...form, git_update_check_interval_seconds: parseInt(e.target.value) || 3600 })}
                className="w-24 px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm"
              />
              <span className="text-sm text-text-muted">seconds</span>
            </div>

            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium">Auto-pull</p>
                <p className="text-xs text-text-muted mt-0.5">Automatically pull updates when available (instead of fetch only).</p>
              </div>
              <button type="button" onClick={() => setForm({ ...form, git_update_auto_pull: !form.git_update_auto_pull })} className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ${form.git_update_auto_pull ? "bg-primary" : "bg-border"}`}>
                <span className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${form.git_update_auto_pull ? "translate-x-5" : "translate-x-0"}`} />
              </button>
            </div>

            {/* Git status display */}
            {gitStatus && !gitLoading && (
              <div className="pt-3 border-t border-border space-y-2">
                <div className="flex items-center gap-2 text-xs">
                  {gitStatus.git_available ? (
                    gitStatus.is_repo ? (
                      <>
                        <GitCommit size={12} className={gitStatus.version_available ? "text-primary" : "text-text-muted"} />
                        <span className="text-text-muted">
                          Local: <code className="px-1 rounded bg-bg font-mono">{gitStatus.local_version}</code>
                          {gitStatus.version_available && (
                            <>
                              <ArrowDownUp size={10} className="mx-0.5" />
                              Remote: <code className="px-1 rounded bg-bg font-mono">{gitStatus.remote_version}</code>
                              <span className="text-primary ml-1 font-medium">update available</span>
                            </>
                          )}
                          {!gitStatus.version_available && (
                            <span className="text-success ml-1 font-medium">up to date</span>
                          )}
                        </span>
                      </>
                    ) : (
                      <>
                        <GitCommit size={12} className="text-text-muted" />
                        <span className="text-text-muted">Not a git repository</span>
                      </>
                    )
                  ) : (
                    <>
                      <AlertCircle size={12} className="text-warning" />
                      <span className="text-warning">Git not installed</span>
                    </>
                  )}
                </div>

                {gitError && (
                  <div className="text-xs text-danger flex items-center gap-1.5">
                    <AlertCircle size={12} />
                    <span>{gitError}</span>
                  </div>
                )}

                <div className="flex gap-2">
                  <button
                    onClick={handleGitFetch}
                    disabled={gitActionLoading === "fetch"}
                    className="px-3 py-1.5 rounded-lg border border-border hover:border-primary/50 text-xs font-medium transition-colors disabled:opacity-50 flex items-center gap-1"
                  >
                    {gitActionLoading === "fetch" ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      <GitCommit size={12} />
                    )}
                    Fetch
                  </button>
                  {gitStatus.version_available && (
                    <button
                      onClick={handleGitPull}
                      disabled={gitActionLoading === "pull"}
                      className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-white text-xs font-medium transition-colors disabled:opacity-50 flex items-center gap-1"
                    >
                      {gitActionLoading === "pull" ? (
                        <Loader2 size={12} className="animate-spin" />
                      ) : (
                        <ArrowDownUp size={12} />
                      )}
                      Pull
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* Secrets */}
          <div className="rounded-xl bg-surface border border-border p-5 space-y-4">
            <div className="flex items-center justify-between pb-3 border-b border-border">
              <div className="flex items-center gap-2">
                <KeyRound size={16} className="text-primary" />
                <h3 className="font-semibold">Secrets</h3>
              </div>
              <span className="text-xs text-text-muted px-2 py-0.5 rounded bg-bg border border-border font-mono">mode 600</span>
            </div>

            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-sm font-medium">HuggingFace Token</label>
                {secrets?.hf_token && <span className="text-xs text-success font-mono">Active ···{secrets.hf_token.slice(-4)}</span>}
              </div>
              <div className="flex gap-2">
                <div className="relative flex-1">
                  <input type={showToken ? "text" : "password"} value={hfToken} onChange={(e) => setHfToken(e.target.value)} placeholder={secrets?.hf_token ? "Enter new token to replace…" : "hf_…"} className={`${inputCls} pr-9`} autoComplete="off" />
                  <button type="button" onClick={() => setShowToken(v => !v)} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-text-muted hover:text-text transition-colors">
                    {showToken ? <EyeOff size={15} /> : <Eye size={15} />}
                  </button>
                </div>
                <button onClick={handleSaveToken} disabled={savingToken || !hfToken.trim()} className="px-3 py-2 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50 text-white font-medium text-sm transition-colors flex items-center gap-1.5">
                  {savingToken ? <Loader2 className="animate-spin" size={14} /> : savedToken ? <Check size={14} /> : <KeyRound size={14} />}
                  {savedToken ? "Saved!" : "Save"}
                </button>
                {secrets?.hf_token && (
                  <button onClick={handleClearToken} className="px-3 py-2 rounded-lg border border-border hover:border-danger/50 hover:text-danger text-text-muted transition-colors" title="Clear token">
                    <Trash2 size={15} />
                  </button>
                )}
              </div>
              <p className="text-xs text-text-muted mt-1.5">Passed as <code className="font-mono">HF_TOKEN</code> when launching deployments. Set env var to override.</p>
            </div>
          </div>

        </div>
      </div>

      {alertModal && <AlertModal open={!!alertModal} onClose={() => setAlertModal(null)} title={alertModal.title} message={alertModal.message} />}
    </div>
  );
}
