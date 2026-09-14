/** The doctor, for one node, on the page about the machines.
 *
 * `spark_pulse/agent/doctor.py` was written and never surfaced. This is its
 * face: open it on a node and it says what is wrong, grouped by who can fix
 * it — the control plane, a person deciding, or someone on that machine —
 * with each finding's remedy. "Repair what is fixable" acts only on the
 * `fixable-here` findings and shows what was done. Diagnosis changes nothing;
 * a repair is the one button that does, and it names its cost.
 */

import { useCallback, useEffect, useState } from "react";
import { AlertCircle, AlertTriangle, CheckCircle2, Info, Loader2, Wrench, X } from "lucide-react";
import { fetchNodeDoctor, treatNode } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import type { ClusterNode, DoctorFinding, DoctorReport } from "@/lib/types";

const STATUS_ICON: Record<DoctorFinding["status"], typeof CheckCircle2> = {
  ok: CheckCircle2,
  warn: AlertTriangle,
  broken: AlertCircle,
  unknown: Info,
};

const STATUS_CLASS: Record<DoctorFinding["status"], string> = {
  ok: "text-success",
  warn: "text-warning",
  broken: "text-danger",
  unknown: "text-text-muted",
};

function Findings({ report }: { report: DoctorReport }) {
  const { t } = useI18n();
  const label: Record<string, string> = {
    "fixable-here": t("nodes.doctor.fixable"),
    "needs-a-decision": t("nodes.doctor.decision"),
    "needs-a-human-on-that-machine": t("nodes.doctor.human"),
  };
  return (
    <div className="space-y-2" data-testid="doctor-findings">
      {report.findings.map((f) => {
        const Icon = STATUS_ICON[f.status];
        return (
          <div key={f.check} className="rounded-lg border border-border p-2.5 text-sm">
            <div className="flex items-start gap-2">
              <Icon size={15} className={`mt-0.5 shrink-0 ${STATUS_CLASS[f.status]}`} />
              <div className="min-w-0">
                <p>
                  <span className="font-medium">{f.check}</span>
                  {f.status !== "ok" && label[f.verdict] && (
                    <span className="ml-2 text-xs text-text-muted">· {label[f.verdict]}</span>
                  )}
                </p>
                <p className="text-text-muted">{f.detail}</p>
                {f.status !== "ok" && f.remedy && (
                  <p className="mt-1 text-xs text-text-muted">
                    <span className="font-medium">{t("nodes.doctor.remedyLabel")}:</span> {f.remedy}
                  </p>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

interface Props {
  node: ClusterNode;
  onClose: () => void;
  onChanged: () => void;
}

export default function NodeDoctor({ node, onClose, onChanged }: Props) {
  const { t } = useI18n();
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [repairing, setRepairing] = useState(false);
  const [sudoPassword, setSudoPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  const diagnose = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setReport(await fetchNodeDoctor(node.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not diagnose the node");
    } finally {
      setLoading(false);
    }
  }, [node.id]);

  useEffect(() => {
    void diagnose();
  }, [diagnose]);

  const repair = async () => {
    setRepairing(true);
    setError(null);
    try {
      setReport(await treatNode(node.id, sudoPassword || undefined));
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "The repair failed");
    } finally {
      setRepairing(false);
    }
  };

  const fixable = (report?.findings ?? []).filter((f) => f.verdict === "fixable-here" && f.status !== "ok");
  const problems = (report?.findings ?? []).filter((f) => f.status === "warn" || f.status === "broken");

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div
        role="dialog"
        aria-label={t("nodes.doctor.title", { name: node.name })}
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-2xl border border-border bg-surface p-6 shadow-2xl"
      >
        <div className="mb-4 flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-lg font-bold">
            <Wrench size={20} className="text-primary" />
            {t("nodes.doctor.title", { name: node.name })}
          </h3>
          <button onClick={onClose} aria-label={t("common.close")} className="rounded-lg p-1 hover:bg-surface-hover">
            <X size={18} />
          </button>
        </div>

        {loading && (
          <p className="flex items-center gap-2 text-sm text-text-muted" role="status">
            <Loader2 size={14} className="animate-spin" />
            {t("nodes.doctor.running")}
          </p>
        )}

        {report && !loading && (
          <div className="space-y-4">
            <div className="flex items-center justify-between text-sm">
              <span className={report.healthy ? "text-success" : "text-text-muted"}>
                {report.healthy
                  ? t("nodes.doctor.healthy")
                  : t("nodes.doctor.problems", { count: problems.length })}
              </span>
              {report.channels.length > 0 && (
                <span className="text-xs text-text-muted">
                  {t("nodes.doctor.channels", { channels: report.channels.join(", ") })}
                </span>
              )}
            </div>

            <Findings report={report} />

            {report.repairs.length > 0 && (
              <div data-testid="doctor-repairs">
                <p className="mb-1 text-sm font-medium">{t("nodes.doctor.repairs")}</p>
                <ul className="space-y-1 text-xs">
                  {report.repairs.map((r, i) => (
                    <li key={i} className="flex items-start gap-2">
                      <span className={r.applied ? "text-success" : "text-text-muted"}>
                        {r.applied ? t("nodes.doctor.applied") : t("nodes.doctor.notApplied")}
                      </span>
                      <span className="text-text-muted">
                        {r.check}: {r.detail}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {fixable.length > 0 && !node.is_control_plane && (
              <div className="space-y-2 border-t border-border pt-3">
                <label htmlFor="doctor-sudo" className="block text-sm font-medium text-text-muted">
                  {t("nodes.doctor.sudoPassword")}
                </label>
                <input
                  id="doctor-sudo"
                  type="password"
                  autoComplete="off"
                  value={sudoPassword}
                  onChange={(e) => setSudoPassword(e.target.value)}
                  disabled={repairing}
                  className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-text focus:outline-none focus:ring-2 focus:ring-primary/50"
                />
                <p className="text-xs text-text-muted">{t("nodes.doctor.sudoNote")}</p>
              </div>
            )}

            {error && (
              <div role="alert" className="flex items-center gap-2 rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
                <AlertCircle size={16} className="shrink-0" />
                <span>{error}</span>
              </div>
            )}
          </div>
        )}

        {error && !report && (
          <div role="alert" className="mt-3 flex items-center gap-2 rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
            <AlertCircle size={16} className="shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <div className="mt-6 flex items-center justify-end gap-3 border-t border-border pt-4">
          <button onClick={onClose} className="rounded-lg border border-border px-4 py-2 transition-colors hover:bg-surface-hover">
            {t("nodes.doctor.close")}
          </button>
          {fixable.length > 0 && !node.is_control_plane && (
            <button
              onClick={repair}
              disabled={repairing}
              className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
            >
              {repairing ? <Loader2 size={16} className="animate-spin" /> : <Wrench size={16} />}
              {repairing ? t("nodes.doctor.repairing") : t("nodes.doctor.repair")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
