import { useState, useEffect } from "react";
import { fetchSettings, updateSettings } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { Settings as SettingsIcon, Loader2, AlertCircle, Check } from "lucide-react";
import { AlertModal } from "@/components/Modal";
import { setRefresh } from "@/lib/refresh";

export default function SettingsPage() {
  const { data: settings, loading, error, refetch } = useQuery(fetchSettings);
  const [form, setForm] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);

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

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-primary" size={32} /></div>;
  if (error) return <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3"><AlertCircle size={20} /><span>{error}</span></div>;

  return (
    <div className="space-y-6">
      <div><h2 className="text-2xl font-bold">Settings</h2><p className="text-text-muted mt-1">Configure Spark Manager and spark-vllm-docker integration</p></div>
      <div className="rounded-xl bg-surface border border-border p-6 space-y-6 max-w-xl">
        <div>
          <label className="block text-sm font-medium mb-1">spark-vllm-docker path</label>
          <input type="text" value={String(form.spark_vllm_path ?? "")} onChange={(e) => setForm({ ...form, spark_vllm_path: e.target.value })} className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" placeholder="/path/to/spark-vllm-docker" />
          <p className="text-xs text-text-muted mt-1">Path to the spark-vllm-docker installation</p>
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">Default container</label>
          <input type="text" value={String(form.default_container ?? "vllm-node")} onChange={(e) => setForm({ ...form, default_container: e.target.value })} className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">Default GPU memory utilization</label>
          <input type="number" step="0.05" min="0.1" max="1.0" value={Number(form.default_gpu_mem_util ?? 0.8)} onChange={(e) => setForm({ ...form, default_gpu_mem_util: parseFloat(e.target.value) || 0.8 })} className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" />
        </div>
        <div className="grid grid-cols-2 gap-4">
          <div><label className="block text-sm font-medium mb-1">Port range (start)</label><input type="number" value={Number(form.default_port_range_start ?? 9000)} onChange={(e) => setForm({ ...form, default_port_range_start: parseInt(e.target.value) || 9000 })} className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" /></div>
          <div><label className="block text-sm font-medium mb-1">Port range (end)</label><input type="number" value={Number(form.default_port_range_end ?? 9100)} onChange={(e) => setForm({ ...form, default_port_range_end: parseInt(e.target.value) || 9100 })} className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm" /></div>
        </div>
        <div className="flex items-center gap-3 pt-2">
          <button onClick={handleSave} disabled={saving} className="px-6 py-2.5 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50 text-white font-medium transition-colors flex items-center gap-2">
            {saving ? <Loader2 className="animate-spin" size={16} /> : saved ? <Check size={16} /> : <SettingsIcon size={16} />}{saving ? "Saving..." : saved ? "Saved!" : "Save"}
          </button>
        </div>
      </div>

      {/* Alert modal */}
      {alertModal && (
        <AlertModal open={!!alertModal} onClose={() => setAlertModal(null)} title={alertModal.title} message={alertModal.message} />
      )}
    </div>
  );
}
