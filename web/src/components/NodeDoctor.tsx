/** The doctor, for one node: what is wrong with the machine, and who can fix it.
 *
 * `spark_pulse/agent/doctor.py`'s face. It was a modal, then a prose list in a
 * panel beside the node list — a paragraph per check, the passing ones among
 * them, which made a two-problem node a long scroll. It is a table now, at the
 * foot of the node's page: problems first, everything that passed or was not
 * looked at folded into one row, and each remedy one hover away. It mounts
 * when the operator runs the checks and diagnoses then; "Repair what is
 * fixable" is the one button that changes anything, and it acts only on the
 * `fixable-here` findings.
 */

import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Wrench } from "lucide-react";
import { fetchNodeDoctor, treatNode } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { formatSize } from "@/lib/utils";
import {
  Button,
  ConfirmModal,
  ErrorLine,
  Field,
  InfoTip,
  Input,
  NodeState,
  Spinner,
  Table,
  THead,
  Th,
  Tr,
  Td,
  type NodeCondition,
} from "@/ui";
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

/** Worst first: what is broken, then what warns, then the rest. */
const RANK: Record<DoctorFinding["status"], number> = { broken: 0, warn: 1, unknown: 2, ok: 3 };

function FindingRow({ f }: { f: DoctorFinding }) {
  const { t } = useI18n();
  const label: Record<string, string> = {
    "fixable-here": t("nodes.doctor.fixable"),
    "needs-a-decision": t("nodes.doctor.decision"),
    "needs-a-human-on-that-machine": t("nodes.doctor.human"),
  };
  const problem = f.status === "warn" || f.status === "broken";
  return (
    <Tr data-testid={`doctor-check-${f.check}`} muted={!problem}>
      <Td className="whitespace-nowrap font-medium">
        <span className="inline-flex items-center gap-2">
          <NodeState state={STATUS_STATE[f.status]} dotOnly />
          {f.check}
        </span>
      </Td>
      <Td className="whitespace-nowrap text-[12.5px] text-muted">{problem ? (label[f.verdict] ?? "") : ""}</Td>
      <Td className="text-[13px]">
        <span className="inline-flex items-start gap-1">
          <span className="min-w-0">{f.detail}</span>
          {problem && f.remedy && (
            <InfoTip label={t("nodesPage.howToFix")}>
              <span className="break-words font-mono text-[12px]">{f.remedy}</span>
            </InfoTip>
          )}
        </span>
      </Td>
    </Tr>
  );
}

/** The node clean-ups, kept apart from the faults: nothing here is wrong with
 *  the node, it is memory a model could have. Applying one is a click on that
 *  row, never a side effect of "Repair what is fixable". */
function TuningSection({
  findings,
  busy,
  onChange,
}: {
  findings: DoctorFinding[];
  /** The id being changed, or null. */
  busy: string | null;
  onChange: (id: string, direction: "apply" | "restore") => void;
}) {
  const { t } = useI18n();
  const [asking, setAsking] = useState<DoctorFinding | null>(null);
  const present = findings.filter((f) => f.tuning?.present);
  const applied = present.filter((f) => f.tuning?.applied).length;

  const confirmBody = (f: DoctorFinding) => {
    const id = f.tuning!.id;
    if (id === "snapd") {
      const snaps = f.tuning!.snaps ?? [];
      return snaps.length > 0
        ? t("nodeCleanup.doctor.confirmSnapd", { snaps: snaps.join(", ") })
        : t("nodeCleanup.doctor.confirmSnapdNone");
    }
    return t("nodeCleanup.doctor.confirmDesktop");
  };

  return (
    <div data-testid="doctor-tuning" className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-[13px] font-medium">{t("nodeCleanup.title")}</p>
        <p className="text-[13px] text-muted" data-testid="doctor-tuning-summary">
          {t("nodeCleanup.doctor.summary", { applied, total: present.length })}
        </p>
      </div>
      <div className="overflow-hidden rounded-md border border-line">
        <Table>
          <THead>
            <Th>{t("nodeCleanup.doctor.colItem")}</Th>
            <Th>{t("nodeCleanup.doctor.colState")}</Th>
            <Th>{t("nodeCleanup.doctor.colAction")}</Th>
          </THead>
          <tbody>
            {findings.map((f) => {
              const info = f.tuning!;
              const state =
                f.status === "unknown"
                  ? "unknown"
                  : !info.present
                    ? "notPresent"
                    : info.applied
                      ? "applied"
                      : "notApplied";
              const commands = info.applied ? info.restore_commands : info.apply_commands;
              const mine = busy === info.id;
              return (
                <Tr key={f.check} data-testid={`doctor-tuning-${info.id}`} muted={state === "notPresent"}>
                  <Td className="font-medium">
                    <span className="block">{t(`nodeCleanup.items.${info.id}.name`)}</span>
                    {state === "notApplied" && info.memory_bytes ? (
                      <span className="block text-[12.5px] font-normal text-muted">
                        {t("nodeCleanup.doctor.holds", { size: formatSize(info.memory_bytes) })}
                      </span>
                    ) : null}
                  </Td>
                  <Td className="whitespace-nowrap text-[13px] text-muted">{t(`nodeCleanup.doctor.state.${state}`)}</Td>
                  <Td>
                    {state === "unknown" || state === "notPresent" ? null : info.can_treat ? (
                      info.applied ? (
                        <Button size="sm" variant="ghost" loading={mine} disabled={busy !== null} onClick={() => onChange(info.id, "restore")}>
                          {t("nodeCleanup.doctor.restore")}
                        </Button>
                      ) : (
                        <Button
                          size="sm"
                          variant="primary"
                          loading={mine}
                          disabled={busy !== null}
                          onClick={() => (info.consequential ? setAsking(f) : onChange(info.id, "apply"))}
                        >
                          {t("nodeCleanup.doctor.apply")}
                        </Button>
                      )
                    ) : (
                      <InfoTip label={t("nodeCleanup.doctor.runThere")}>
                        <span className="block break-words font-mono text-[12px]">
                          {commands.map((c) => (
                            <span key={c} className="block">
                              {c}
                            </span>
                          ))}
                        </span>
                      </InfoTip>
                    )}
                  </Td>
                </Tr>
              );
            })}
          </tbody>
        </Table>
      </div>
      {asking && (
        <ConfirmModal
          open
          onClose={() => setAsking(null)}
          onConfirm={() => {
            const id = asking.tuning!.id;
            setAsking(null);
            onChange(id, "apply");
          }}
          title={t("nodeCleanup.doctor.confirmTitle", { name: t(`nodeCleanup.items.${asking.tuning!.id}.name`) })}
          message={confirmBody(asking)}
          confirmLabel={t("nodeCleanup.doctor.apply")}
        />
      )}
    </div>
  );
}

function Findings({ report }: { report: DoctorReport }) {
  const { t, plural } = useI18n();
  const [showRest, setShowRest] = useState(false);
  const sorted = report.findings
    .filter((f) => !f.tuning)
    .sort((a, b) => RANK[a.status] - RANK[b.status]);
  const problems = sorted.filter((f) => f.status === "warn" || f.status === "broken");
  const rest = sorted.filter((f) => !(f.status === "warn" || f.status === "broken"));
  return (
    <div data-testid="doctor-findings" className="overflow-hidden rounded-md border border-line">
      <Table>
        <THead>
          <Th>{t("nodes.doctor.colCheck")}</Th>
          <Th>{t("nodes.doctor.colWho")}</Th>
          <Th>{t("nodes.doctor.colFound")}</Th>
        </THead>
        <tbody>
          {problems.map((f) => (
            <FindingRow key={f.check} f={f} />
          ))}
          {rest.length > 0 && (
            <tr className="border-b border-line last:border-b-0">
              <td colSpan={3} className="px-3 py-1.5">
                <button
                  type="button"
                  onClick={() => setShowRest((v) => !v)}
                  aria-expanded={showRest}
                  className="inline-flex items-center gap-1 text-[12.5px] text-muted hover:text-text"
                >
                  {showRest ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
                  {plural("nodes.doctor.moreChecks", rest.length)}
                </button>
              </td>
            </tr>
          )}
          {showRest && rest.map((f) => <FindingRow key={f.check} f={f} />)}
        </tbody>
      </Table>
    </div>
  );
}

interface Props {
  node: ClusterNode;
  /** A repair changes the machine, so whoever holds the node list reloads it. */
  onChanged: () => void;
}

export default function NodeDoctor({ node, onChanged }: Props) {
  const { t } = useI18n();
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [repairing, setRepairing] = useState(false);
  const [changing, setChanging] = useState<string | null>(null);
  const [sudoPassword, setSudoPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  const diagnose = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setReport(await fetchNodeDoctor(node.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : t("nodes.doctor.diagnoseFailed"));
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
      setError(e instanceof Error ? e.message : t("nodes.doctor.repairFailed"));
    } finally {
      setRepairing(false);
    }
  };

  /** Apply or restore one clean-up. Naming it is the only way one changes. */
  const change = async (id: string, direction: "apply" | "restore") => {
    setChanging(id);
    setError(null);
    try {
      setReport(await treatNode(node.id, sudoPassword || undefined, { [direction]: [id] }));
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("nodes.doctor.repairFailed"));
    } finally {
      setChanging(null);
    }
  };

  // A clean-up is not a fault, so it is neither counted as one nor repaired by
  // the button that repairs faults.
  const faults = (report?.findings ?? []).filter((f) => !f.tuning);
  const tunings = (report?.findings ?? []).filter((f) => f.tuning);
  const fixable = faults.filter((f) => f.verdict === "fixable-here" && f.status !== "ok");
  const problems = faults.filter((f) => f.status === "warn" || f.status === "broken");
  const canRepair = fixable.length > 0 && !node.is_control_plane;
  const canChange = tunings.some((f) => f.tuning?.can_treat && f.tuning.present);

  return (
    <div data-testid={`doctor-${node.id}`}>
      {loading && (
        <p className="flex items-center gap-2 text-[13px] text-muted" role="status">
          <Spinner size="sm" />
          {t("nodes.doctor.running")}
        </p>
      )}

      {report && !loading && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-[13px]">
            <span className={problems.length === 0 ? "text-good" : "text-muted"}>
              {problems.length === 0
                ? t("nodes.doctor.healthy")
                : t("nodes.doctor.problems", { count: problems.length })}
            </span>
            {report.channels.length > 0 && (
              <span className="text-[13px] text-muted">
                {t("nodes.doctor.channels", { channels: report.channels.join(", ") })}
              </span>
            )}
          </div>

          <Findings report={report} />

          {tunings.length > 0 && <TuningSection findings={tunings} busy={changing} onChange={change} />}

          {report.repairs.length > 0 && (
            <div data-testid="doctor-repairs">
              <p className="mb-1 text-[13px] font-medium">{t("nodes.doctor.repairs")}</p>
              <ul className="space-y-1 text-[13px]">
                {report.repairs.map((r, i) => (
                  <li key={i} className="flex items-start gap-2">
                    <span className={r.applied ? "text-good" : "text-muted"}>
                      {r.applied ? t("nodes.doctor.applied") : t("nodes.doctor.notApplied")}
                    </span>
                    <span className="text-muted">
                      {r.check}: {r.detail}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {(canRepair || canChange) && (
            <div className="space-y-3">
              <Field label={t("nodes.doctor.sudoPassword")} hint={t("nodes.doctor.sudoNote")}>
                {(control) => (
                  <Input
                    {...control}
                    type="password"
                    autoComplete="off"
                    value={sudoPassword}
                    onChange={(e) => setSudoPassword(e.target.value)}
                    disabled={repairing || changing !== null}
                  />
                )}
              </Field>
              {canRepair && (
              <Button
                size="sm"
                variant="primary"
                icon={Wrench}
                loading={repairing}
                onClick={repair}
                className="w-full min-[520px]:w-auto"
              >
                {repairing ? t("nodes.doctor.repairing") : t("nodes.doctor.repair")}
              </Button>
              )}
            </div>
          )}

          <ErrorLine>{error}</ErrorLine>
        </div>
      )}

      {!report && !loading && <ErrorLine>{error}</ErrorLine>}
    </div>
  );
}
