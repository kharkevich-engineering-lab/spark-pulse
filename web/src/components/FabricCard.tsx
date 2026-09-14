/** The ConnectX fabric across the cluster: what it is, what it should be, apply.
 *
 * Every row is a node's ports as its agent last reported them — no login —
 * and the plan column is what `spark-vllm-docker`'s networking guide would
 * have an operator write into `/etc/netplan/40-cx7.yaml` on that machine.
 * "Configure fabric" writes those files over SSH with the control plane's
 * key, applies them, and reads each port back; the report shows the peers
 * that answered over each cable, because a cable that does not go where the
 * plan assumed is a ping that fails, not a file that was written.
 */

import { useCallback, useEffect, useState } from "react";
import { AlertCircle, Cable, CheckCircle2, Loader2, RefreshCw, X } from "lucide-react";
import { applyFabric, fetchFabric } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import type {
  FabricApplyReport,
  FabricNode,
  FabricNodePlan,
  FabricNodeStatus,
  FabricResponse,
} from "@/lib/types";

const STATUS_CLASS: Record<FabricNodeStatus, string> = {
  configured: "bg-success/20 text-success border-success/30",
  proposed: "bg-primary/15 text-primary border-primary/30",
  unknown: "bg-warning/20 text-warning border-warning/30",
  refused: "bg-danger/20 text-danger border-danger/30",
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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div
        role="dialog"
        aria-label={t("fabric.configure")}
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-2xl border border-border bg-surface p-6 shadow-2xl"
      >
        <div className="mb-4 flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-lg font-bold">
            <Cable size={20} className="text-primary" />
            {t("fabric.configure")}
          </h3>
          <button onClick={onClose} aria-label={t("common.close")} className="rounded-lg p-1 hover:bg-surface-hover">
            <X size={18} />
          </button>
        </div>

        {reports ? (
          <div className="space-y-3" data-testid="fabric-apply-report">
            {reports.map((r) => (
              <div
                key={r.node_id}
                role="status"
                className={`rounded-lg border p-3 text-sm ${
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
                <details key={n.node_id} className="rounded-lg border border-border p-3">
                  <summary className="cursor-pointer text-sm font-medium">
                    {t("fabric.showFile", { name: n.name })}
                  </summary>
                  <p className="mt-1 text-xs text-text-muted">{t("fabric.filePath", { path: n.netplan_path })}</p>
                  <pre className="mt-2 overflow-x-auto rounded bg-bg p-2 font-mono text-xs">{n.netplan}</pre>
                </details>
              ))}

            {configured.length > 0 && (
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} disabled={running} />
                {t("fabric.override")}
              </label>
            )}

            <div>
              <label htmlFor="fabric-sudo" className="mb-1 block text-sm font-medium text-text-muted">
                {t("fabric.sudoPassword")}
              </label>
              <input
                id="fabric-sudo"
                type="password"
                autoComplete="off"
                value={sudoPassword}
                onChange={(e) => setSudoPassword(e.target.value)}
                disabled={running}
                className="w-full rounded-lg border border-border bg-bg px-3 py-2 text-text focus:outline-none focus:ring-2 focus:ring-primary/50"
              />
              <p className="mt-1 text-xs text-text-muted">{t("fabric.sudoNote")}</p>
            </div>

            {running && (
              <p className="flex items-center gap-2 text-sm text-text-muted" role="status">
                <Loader2 size={14} className="animate-spin" />
                {t("fabric.applying")}
              </p>
            )}
            {error && (
              <div role="alert" className="flex items-center gap-2 rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
                <AlertCircle size={16} className="shrink-0" />
                <span>{error}</span>
              </div>
            )}
          </div>
        )}

        <div className="mt-6 flex items-center justify-end gap-3 border-t border-border pt-4">
          {reports ? (
            <button onClick={onClose} className="rounded-lg bg-primary px-4 py-2 text-primary-foreground hover:bg-primary/90">
              {t("fabric.close")}
            </button>
          ) : (
            <>
              <button onClick={onClose} disabled={running} className="rounded-lg border border-border px-4 py-2 hover:bg-surface-hover disabled:opacity-50">
                {t("fabric.cancel")}
              </button>
              <button
                onClick={run}
                disabled={running || count === 0}
                className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {running ? <Loader2 size={16} className="animate-spin" /> : <Cable size={16} />}
                {t("fabric.configureCount", { count })}
              </button>
            </>
          )}
        </div>
      </div>
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
    <section data-testid="fabric-card" className="rounded-xl border border-border bg-surface p-4">
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-2 text-lg font-bold">
            <Cable size={18} className="text-primary" />
            {t("fabric.heading")}
          </h3>
          <p className="mt-0.5 text-sm text-text-muted">{t("fabric.subtitle")}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            onClick={() => void load()}
            disabled={loading}
            aria-label={t("fabric.refresh")}
            title={t("fabric.refresh")}
            className="rounded-lg border border-border p-1.5 hover:bg-surface-hover disabled:opacity-50"
          >
            {loading ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
          </button>
          <button
            onClick={() => setConfiguring(true)}
            disabled={!data || !data.transport || (proposed === 0 && !plan?.nodes.some((n) => n.status === "configured"))}
            className="flex items-center gap-2 rounded-lg bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            <Cable size={14} />
            {t("fabric.configure")}
          </button>
        </div>
      </div>

      {error && (
        <div role="alert" className="flex items-center gap-3 rounded-lg border border-danger/30 bg-danger/10 p-3 text-danger">
          <AlertCircle size={18} />
          <span>{error}</span>
        </div>
      )}

      {data && !data.transport && <p className="text-sm text-text-muted">{t("fabric.noTransport")}</p>}

      {data && data.transport && (
        <>
          <p className="mb-3 text-sm text-text-muted">{modeText}</p>
          {plan && (plan.advice?.length ?? 0) > 0 && (
            <ul className="mb-3 space-y-1" data-testid="fabric-advice">
              {plan.advice!.map((a, i) => (
                <li key={i} role="note" className="rounded-lg border border-border bg-surface-hover p-2 text-sm text-text-muted">
                  {a}
                </li>
              ))}
            </ul>
          )}
          {plan && plan.problems.length > 0 && (
            <ul className="mb-3 space-y-1" data-testid="fabric-problems">
              {plan.problems.map((p, i) => (
                <li key={i} role="note" className="rounded-lg border border-warning/30 bg-warning/10 p-2 text-sm">
                  {p}
                </li>
              ))}
            </ul>
          )}
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-text-muted">
                  <th scope="col" className="py-2 pr-4 font-semibold">{t("fabric.colNode")}</th>
                  <th scope="col" className="py-2 pr-4 font-semibold">{t("fabric.colPorts")}</th>
                  <th scope="col" className="py-2 pr-4 font-semibold">{t("fabric.colAddresses")}</th>
                  <th scope="col" className="py-2 pr-4 font-semibold">{t("fabric.colMtu")}</th>
                  <th scope="col" className="py-2 font-semibold">{t("fabric.colStatus")}</th>
                </tr>
              </thead>
              <tbody>
                {data.nodes.map((node) => {
                  const nodePlan = planFor(node.node_id);
                  const up = upPorts(node);
                  return (
                    <tr key={node.node_id} className="border-b border-border/50 align-top last:border-0">
                      <td className="py-2.5 pr-4 font-medium">
                        {node.name}
                        {node.reported && (
                          <div className="mt-0.5 text-xs font-normal text-text-muted">
                            {node.pinned?.fabric_mode ? t("fabric.pinned") : t("fabric.notPinned")}
                            {node.wired_management_up === false && plan?.mode === "mesh" && (
                              <span className="text-warning"> · {t("fabric.noWired")}</span>
                            )}
                          </div>
                        )}
                      </td>
                      <td className="py-2.5 pr-4 font-mono text-xs">
                        {!node.reported
                          ? <span className="font-sans text-text-muted">{t("fabric.notReported")}</span>
                          : up.length === 0
                            ? <span className="font-sans text-text-muted">{t("fabric.noPortsUp")}</span>
                            : up.map((p) => <div key={p.netdev}>{p.netdev}</div>)}
                      </td>
                      <td className="py-2.5 pr-4 font-mono text-xs">
                        {up.map((p) => (
                          <div key={p.netdev}>
                            {p.cidr || <span className="font-sans text-text-muted">{t("fabric.noAddress")}</span>}
                            {nodePlan?.status === "proposed" &&
                              nodePlan.assignments.find((a) => a.netdev === p.netdev)?.cidr !== p.cidr && (
                                <span className="text-primary"> → {nodePlan.assignments.find((a) => a.netdev === p.netdev)?.cidr}</span>
                              )}
                          </div>
                        ))}
                      </td>
                      <td className="py-2.5 pr-4 font-mono text-xs">
                        {up.map((p) => (
                          <div key={p.netdev}>{p.mtu || "—"}</div>
                        ))}
                      </td>
                      <td className="py-2.5">
                        {nodePlan && (
                          <span
                            title={nodePlan.reasons.join(" ")}
                            className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium ${STATUS_CLASS[nodePlan.status]}`}
                          >
                            {statusLabel[nodePlan.status]}
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}

      {configuring && plan && (
        <ApplyDialog plan={plan.nodes} onClose={() => setConfiguring(false)} onApplied={() => void load()} />
      )}
    </section>
  );
}
