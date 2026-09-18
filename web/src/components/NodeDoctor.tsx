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
import { Wrench } from "lucide-react";
import { fetchNodeDoctor, treatNode } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Button, ErrorLine, Field, Input, Modal, NodeState, Spinner, type NodeCondition } from "@/ui";
import type { ClusterNode, DoctorFinding, DoctorReport } from "@/lib/types";

/** The doctor's four verdicts in the one node vocabulary. It used to have four
 *  icons of its own — a tick, a triangle, an alert and an info — which is a
 *  second thing to learn for the same four states the rest of the app already
 *  says with a coloured dot. */
const STATUS_STATE: Record<DoctorFinding["status"], NodeCondition> = {
  ok: "ok",
  warn: "warn",
  broken: "bad",
  unknown: "unknown",
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
      {report.findings.map((f) => (
        <div key={f.check} className="rounded-sm border border-line p-2.5 text-[14px]">
          <div className="flex items-start gap-2">
            <NodeState state={STATUS_STATE[f.status]} dotOnly className="mt-1.5" />
            <div className="min-w-0">
              <p>
                <span className="font-medium">{f.check}</span>
                {f.status !== "ok" && label[f.verdict] && (
                  <span className="ml-2 text-[13px] text-muted">· {label[f.verdict]}</span>
                )}
              </p>
              <p className="text-muted">{f.detail}</p>
              {f.status !== "ok" && f.remedy && (
                <p className="mt-1 text-[13px] text-muted">
                  <span className="font-medium">{t("nodes.doctor.remedyLabel")}:</span> {f.remedy}
                </p>
              )}
            </div>
          </div>
        </div>
      ))}
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
    <Modal
      open
      onClose={onClose}
      size="md"
      title={t("nodes.doctor.title", { name: node.name })}
      icon={<Wrench size={20} className="text-blue2" />}
      actions={
        <>
          <Button size="sm" onClick={onClose}>
            {t("nodes.doctor.close")}
          </Button>
          {fixable.length > 0 && !node.is_control_plane && (
            <Button size="sm" variant="primary" icon={Wrench} loading={repairing} onClick={repair}>
              {repairing ? t("nodes.doctor.repairing") : t("nodes.doctor.repair")}
            </Button>
          )}
        </>
      }
    >
      <>
        {loading && (
          <p className="flex items-center gap-2 text-[14px] text-muted" role="status">
            <Spinner size="sm" />
            {t("nodes.doctor.running")}
          </p>
        )}

        {report && !loading && (
          <div className="space-y-4">
            <div className="flex items-center justify-between text-[14px]">
              <span className={report.healthy ? "text-good" : "text-muted"}>
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
              <div className="border-t border-line pt-3">
                <Field label={t("nodes.doctor.sudoPassword")} hint={t("nodes.doctor.sudoNote")}>
                  {(control) => (
                    <Input
                      {...control}
                      type="password"
                      autoComplete="off"
                      value={sudoPassword}
                      onChange={(e) => setSudoPassword(e.target.value)}
                      disabled={repairing}
                    />
                  )}
                </Field>
              </div>
            )}

            <ErrorLine>{error}</ErrorLine>
          </div>
        )}

        {!report && <ErrorLine className="mt-3">{error}</ErrorLine>}
      </>
    </Modal>
  );
}
