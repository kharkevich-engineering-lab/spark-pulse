import { useState, useEffect } from "react";
import { fetchSettings, updateSettings, fetchSecrets, saveSecrets, deleteSecret } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { Settings as SettingsIcon, Loader2, AlertCircle, Check, KeyRound, Eye, EyeOff, Trash2, Lock, Server, Clock } from "lucide-react";
import { AlertModal } from "@/components/Modal";
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

  // HF Token state
  const [hfToken, setHfToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [savingToken, setSavingToken] = useState(false);
  const [savedToken, setSavedToken] = useState(false);

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
              <p className="text-xs text-text-muted mt-1">Stopped and failed jobs older than this are removed automatically. 0 = keep forever.</p>
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
