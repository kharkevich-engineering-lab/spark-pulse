/** The ConnectX fabric across the cluster: what it is, what it should be, apply.
 *
 * Every row is a node's ports as its agent last reported them — no login —
 * and the plan is the addresses `spark-vllm-docker`'s networking guide gives
 * them, applied through the node's agent rather than by hand.
 * "Configure fabric" applies the plan through each node's own agent, which
 * drives nmcli (no SSH), and reads each port back; the report shows the peers
 * that answered over each cable, because a cable that does not go where the
 * plan assumed is a ping that fails, not a connection that was written.
 */

import { useCallback, useEffect, useState } from "react";
import { AlertCircle, Cable, CheckCircle2, RefreshCw } from "lucide-react";
import { applyFabric, fetchFabric } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import {
  Button,
  ErrorLine,
  Field,
  IconButton,
  Input,
  Modal,
  NodeState,
  Spinner,
  type NodeCondition,
} from "@/ui";
import type {
  FabricApplyReport,
  FabricNode,
  FabricNodePlan,
  FabricNodeStatus,
  FabricResponse,
} from "@/lib/types";

/** The fabric's four words in the one node vocabulary. `proposed` is a warning
 *  rather than an accent: the cable is not carrying traffic yet. */
const STATUS_STATE: Record<FabricNodeStatus, NodeCondition> = {
  configured: "ok",
  proposed: "warn",
  unknown: "unknown",
  refused: "bad",
};

function upPorts(node: FabricNode) {
  return node.ports.filter((p) => p.is_up);
}

interface ApplyDialogProps {
  plan: FabricNodePlan[];
  onClose: () => void;
  onApplied: () => void;
}

function ApplyDialog({ plan, onClose, onApplied }: ApplyDialogProps) {
  const { t } = useI18n();
  const [sudoPassword, setSudoPassword] = useState("");
  const [override, setOverride] = useState(false);
  const [running, setRunning] = useState(false);
  const [reports, setReports] = useState<FabricApplyReport[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const proposed = plan.filter((n) => n.status === "proposed");
  const configured = plan.filter((n) => n.status === "configured");
  const count = proposed.length + (override ? configured.length : 0);

  const run = async () => {
    setRunning(true);
    setError(null);
    try {
      const result = await applyFabric({
        override,
        ...(sudoPassword ? { sudo_password: sudoPassword } : {}),
      });
      setReports(result.reports);
      onApplied();
    } catch (e) {
      setError(e instanceof Error ? e.message : "The apply failed");
    } finally {
      setRunning(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      size="md"
      title={t("fabric.configure")}
      icon={<Cable size={20} className="text-blue2" />}
      actions={
        reports ? (
          <Button variant="primary" size="sm" onClick={onClose}>
            {t("fabric.close")}
          </Button>
        ) : (
          <>
            <Button size="sm" onClick={onClose} disabled={running}>
              {t("fabric.cancel")}
            </Button>
            <Button
              size="sm"
              variant="primary"
              icon={Cable}
              loading={running}
              onClick={run}
              disabled={count === 0}
            >
              {t("fabric.configureCount", { count })}
            </Button>
          </>
        )
      }
    >
      <>
        {reports ? (
          <div className="space-y-3" data-testid="fabric-apply-report">
            {reports.map((r) => (
              <div
                key={r.node_id}
                role="status"
                className={`rounded-sm border p-3 text-sm ${
                  r.verified
                    ? "border-success/30 bg-success/10"
                    : r.applied
                      ? "border-warning/30 bg-warning/10"
                      : "border-danger/30 bg-danger/10"
                }`}
              >
                <p className="flex items-center gap-2 font-medium">
                  {r.verified ? <CheckCircle2 size={16} className="text-success" /> : <AlertCircle size={16} />}
                  {r.verified
                    ? t("fabric.applied", { name: r.name })
                    : r.applied
                      ? t("fabric.appliedUnverified", { name: r.name })
                      : t("fabric.notApplied", { name: r.name })}
                </p>
                {r.errors.length > 0 && (
                  <ul className="mt-1 list-disc pl-5 text-xs">
                    {r.errors.map((e, i) => (
                      <li key={i}>{e}</li>
                    ))}
                  </ul>
                )}
                {r.pings.length > 0 && (
                  <div className="mt-2 text-xs">
                    <p className="font-medium">{t("fabric.pings")}</p>
                    <ul className="mt-0.5 space-y-0.5 font-mono">
                      {r.pings.map((p, i) => (
                        <li key={i}>
                          {p.netdev} → {p.peer} ({p.address}):{" "}
                          {p.reachable ? t("fabric.reachable") : t("fabric.unreachable")}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {r.steps.length > 0 && (
                  <details className="mt-2 text-xs text-text-muted">
                    <summary className="cursor-pointer">{t("fabric.steps")}</summary>
                    <ol className="mt-1 list-decimal pl-5">
                      {r.steps.map((s, i) => (
                        <li key={i}>{s}</li>
                      ))}
                    </ol>
                  </details>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="space-y-4">
            {plan
              .filter((n) => n.status === "proposed" || (override && n.status === "configured"))
              .map((n) => (
                <details key={n.node_id} className="rounded-sm border border-border p-3">
                  <summary className="cursor-pointer text-sm font-medium">
                    {t("fabric.showPlan", { name: n.name })}
                  </summary>
                  <div className="mt-2 overflow-x-auto">
                    <table className="w-full text-xs">
                      <tbody>
                        {n.assignments.map((a) => (
                          <tr key={a.netdev}>
                            <td className="py-0.5 pr-3 font-mono">{a.netdev}</td>
                            <td className="py-0.5 pr-3 font-mono">{a.cidr || "—"}</td>
                            <td className="py-0.5 font-mono text-text-muted">MTU {a.mtu}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              ))}

            {configured.length > 0 && (
              <label className="flex items-center gap-2 text-[14px]">
                <input
                  type="checkbox"
                  className="accent-[var(--blue)]"
                  checked={override}
                  onChange={(e) => setOverride(e.target.checked)}
                  disabled={running}
                />
                {t("fabric.override")}
              </label>
            )}

            <Field label={t("fabric.sudoPassword")} hint={t("fabric.sudoNote")}>
              {(control) => (
                <Input
                  {...control}
                  type="password"
                  autoComplete="off"
                  value={sudoPassword}
                  onChange={(e) => setSudoPassword(e.target.value)}
                  disabled={running}
                />
              )}
            </Field>

            {running && (
              <p className="flex items-center gap-2 text-[13px] text-muted" role="status">
                <Spinner size="sm" />
                {t("fabric.applying")}
              </p>
            )}
            <ErrorLine>{error}</ErrorLine>
          </div>
        )}
      </>
    </Modal>
  );
}

/** The rule that separates one section of the page from the next. Cards are
 *  for destinations and panels; a section is a heading over a rule. */
const SECTION = "border-t border-line pt-12 mt-10 first:border-t-0 first:pt-0 first:mt-0";

/** One node's ConnectX ports, as its agent reported them, with what the plan
 * would change.
 *
 * Exported because the node registry's per-node expand shows exactly this:
 * the ports belong to the machine, so the row about that machine is where an
 * operator looks for them. One implementation, so the fabric section and the
 * registry cannot disagree about what a node's cables are doing.
 */
export function NodeFabricPorts({
  node,
  plan,
}: {
  node?: FabricNode;
  plan?: FabricNodePlan;
}) {
  const { t } = useI18n();
  if (!node || !node.reported) {
    return <p className="text-[13px] text-muted">{t("fleet.fabricNone")}</p>;
  }
  const up = upPorts(node);
  if (up.length === 0) {
    return <p className="text-[13px] text-muted">{t("fabric.noPortsUp")}</p>;
  }
  return (
    <div role="list" className="divide-y divide-line">
      {up.map((p) => {
        const planned = plan?.assignments.find((a) => a.netdev === p.netdev);
        return (
          <div
            key={p.netdev}
            role="listitem"
            className="flex flex-wrap items-baseline gap-x-4 gap-y-0.5 py-1.5 font-mono text-[12.5px]"
          >
            <span className="min-[520px]:w-24 shrink-0">{p.netdev}</span>
            <span>
              {p.cidr || <span className="font-sans text-muted">{t("fabric.noAddress")}</span>}
            </span>
            {plan?.status === "proposed" && planned && planned.cidr !== p.cidr && (
              <span className="text-blue2">→ {planned.cidr}</span>
            )}
            <span className="text-muted">
              {t("fleet.labelMtu")} {p.mtu || "—"}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export default function FabricCard() {
  const { t } = useI18n();
  const [data, setData] = useState<FabricResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [configuring, setConfiguring] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await fetchFabric());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not read the fabric");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const plan = data?.plan;
  const planFor = (id: string) => plan?.nodes.find((n) => n.node_id === id);
  const proposed = plan?.proposed.length ?? 0;
  const modeText =
    plan?.mode === "direct"
      ? t("fabric.modeDirect")
      : plan?.mode === "dual"
        ? t("fabric.modeDual")
        : plan?.mode === "mesh"
          ? t("fabric.modeMesh")
          : t("fabric.modeNone");
  const statusLabel: Record<FabricNodeStatus, string> = {
    configured: t("fabric.statusConfigured"),
    proposed: t("fabric.statusProposed"),
    unknown: t("fabric.statusUnknown"),
    refused: t("fabric.statusRefused"),
  };

  return (
    <section data-testid="fabric-card" className={SECTION}>
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-[22px] font-bold tracking-[-0.02em]">{t("fabric.heading")}</h2>
          <p className="mt-1 text-[13px] text-muted">{t("fabric.subtitle")}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <IconButton
            size="sm"
            icon={RefreshCw}
            label={t("fabric.refresh")}
            loading={loading}
            onClick={() => void load()}
          />
          <Button
            size="sm"
            variant="primary"
            icon={Cable}
            onClick={() => setConfiguring(true)}
            disabled={!data || !data.transport || (proposed === 0 && !plan?.nodes.some((n) => n.status === "configured"))}
          >
            {t("fabric.configure")}
          </Button>
        </div>
      </div>

      <ErrorLine className="mb-3">{error}</ErrorLine>

      {data && !data.transport && <p className="text-[14px] text-muted">{t("fabric.noTransport")}</p>}

      {data && data.transport && (
        <>
          <p className="mb-3 text-[14px] text-muted">{modeText}</p>
          {plan && (plan.advice?.length ?? 0) > 0 && (
            <ul className="mb-3 space-y-1" data-testid="fabric-advice">
              {plan.advice!.map((a, i) => (
                <li key={i} role="note" className="text-[13px] text-muted">
                  {a}
                </li>
              ))}
            </ul>
          )}
          {plan && plan.problems.length > 0 && (
            <ul className="mb-3 space-y-1" data-testid="fabric-problems">
              {plan.problems.map((p, i) => (
                <li key={i} role="note" className="text-[13px] text-warn">
                  {p}
                </li>
              ))}
            </ul>
          )}
          {/* Rows, not a table: a node's ports are a short list under its name,
              which reads the same at 1280 and at 390 — the five-column table
              this replaced could only be scrolled sideways on a phone. */}
          <div role="list" className="border-y border-line divide-y divide-line">
            {data.nodes.map((node) => {
              const nodePlan = planFor(node.node_id);
              return (
                <div
                  key={node.node_id}
                  role="listitem"
                  aria-label={node.name}
                  data-testid={`fabric-node-${node.node_id}`}
                  className="py-4 space-y-2"
                >
                  <div className="flex flex-wrap items-center gap-3">
                    <span className="text-[15px] font-semibold">{node.name}</span>
                    {nodePlan && (
                      <NodeState
                        state={STATUS_STATE[nodePlan.status]}
                        label={statusLabel[nodePlan.status]}
                        title={nodePlan.reasons.join(" ")}
                      />
                    )}
                    {node.reported && (
                      <span className="text-[13px] text-muted">
                        {node.pinned?.fabric_mode ? t("fabric.pinned") : t("fabric.notPinned")}
                      </span>
                    )}
                    {node.reported && node.wired_management_up === false && plan?.mode === "mesh" && (
                      <span className="text-[13px] text-warn">{t("fabric.noWired")}</span>
                    )}
                  </div>
                  <NodeFabricPorts node={node} plan={nodePlan} />
                </div>
              );
            })}
          </div>
        </>
      )}

      {configuring && plan && (
        <ApplyDialog plan={plan.nodes} onClose={() => setConfiguring(false)} onApplied={() => void load()} />
      )}
    </section>
  );
}
