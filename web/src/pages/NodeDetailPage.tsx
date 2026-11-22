/** One node, on a page of its own: `/cluster/:id`.
 *
 * It was a panel beside the node list, then five tabs — and every tab left
 * most of a wide screen empty, one half-width card beside nothing. A node is
 * one page now, read top to bottom: a strip of what it reports right now, its
 * GPU history side by side, the processes holding it beside its disks, its
 * cables beside what is wrong with it, and the doctor last, run on request.
 * A row with only one thing in it gives that thing the whole width.
 */

import { Children, useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { RefreshCw, Server, Stethoscope } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { fetchFabric, fetchNodeDiagnostics, fetchNodes } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { useNodeStats } from "@/hooks/useNodeStats";
import { useGpuHistory } from "@/hooks/useGpuHistory";
import { isLiveRun, useDeployments } from "@/hooks/useDeployments";
import { memoryLayout, runColors } from "@/lib/memoryLayout";
import { cn } from "@/lib/utils";
import type { ClusterNode, FabricResponse, NodeFinding, NodeStats } from "@/lib/types";
import { Button, DetailHeader, EmptyState, ErrorLine, NodeState as NodeStateBadge, Section, Spinner } from "@/ui";
import { FindingLine, NodeActions, STATE_CONDITION, interfaceSummary, useNodeActions } from "@/components/NodeRegistry";
import { NodeCharts, NodeProcesses, NodeStatStrip, NodeStorage } from "@/components/nodes/NodeLive";
import { NodeFabricPorts } from "@/components/FabricCard";
import NodeDoctor from "@/components/NodeDoctor";

/** Two things side by side at a wide width, or the one that is there across
 *  all of it — never a half-width section beside a hole. */
function Row({ wide = false, children }: { wide?: boolean; children: React.ReactNode }) {
  const present = Children.toArray(children).filter(Boolean);
  if (present.length === 0) return null;
  return (
    <div
      className={cn(
        "grid items-start gap-3",
        present.length > 1 &&
          (wide ? "min-[1100px]:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]" : "min-[1100px]:grid-cols-2"),
      )}
    >
      {present}
    </div>
  );
}

export default function NodeDetailPage() {
  const { t } = useI18n();
  const { id = "" } = useParams();
  const navigate = useNavigate();

  const { data: nodes, loading, error, refetch } = useQuery<ClusterNode[]>(fetchNodes);
  const stats = useNodeStats();
  const history = useGpuHistory(stats.nodes);
  const { deployments } = useDeployments();
  const [findings, setFindings] = useState<NodeFinding[]>([]);
  const [fabric, setFabric] = useState<FabricResponse | null>(null);
  const [fabricError, setFabricError] = useState<string | null>(null);
  // The doctor is asked when somebody asks: it logs into the machine.
  const [checksRun, setChecksRun] = useState(0);

  const node = nodes?.find((n) => n.id === id);
  const live: NodeStats | undefined = stats.nodes.find((s) => s.id === id);

  const layout = useMemo(() => {
    if (!live) return null;
    const runs = (deployments ?? []).filter(isLiveRun);
    return memoryLayout(live, runs, runColors(runs));
  }, [live, deployments]);

  const loadDiagnostics = useCallback(() => {
    fetchNodeDiagnostics()
      .then((result) => setFindings(result.findings))
      .catch(() => setFindings([]));
  }, []);
  useEffect(loadDiagnostics, [loadDiagnostics]);

  const reload = useCallback(() => {
    refetch();
    loadDiagnostics();
  }, [refetch, loadDiagnostics]);

  const actions = useNodeActions({ reload, onForgotten: () => navigate("/cluster") });

  // The fabric is one cheap read of what the agents reported.
  useEffect(() => {
    fetchFabric()
      .then(setFabric)
      .catch((e) => setFabricError(e instanceof Error ? e.message : String(e)));
  }, []);

  if (loading && !nodes) {
    return (
      <div className="flex justify-center py-16">
        <Spinner size="lg" label={t("common.loading")} />
      </div>
    );
  }

  if (!node) {
    return (
      <div className="space-y-3">
        <ErrorLine>{error}</ErrorLine>
        <EmptyState icon={Server} action={<Link to="/cluster">{t("nodesPage.backToNodes")}</Link>}>
          {t("nodesPage.notFound", { id })}
        </EmptyState>
      </div>
    );
  }

  const nodeFindings = findings.filter((f) => f.node_ids.length === 1 && f.node_ids[0] === node.id);
  const reachable = live?.reachable ?? false;
  const hasStorage = reachable && live !== undefined && (live.cpu.total > 0 || live.disk.length > 0);

  return (
    <div data-testid={`node-detail-${node.id}`}>
      <DetailHeader
        backTo="/cluster"
        backLabel={t("nav.nodes")}
        title={node.name}
        badge={<NodeStateBadge state={STATE_CONDITION[node.state] ?? "unknown"} />}
        subtitle={
          <span className="font-mono">
            {node.address || "—"} · {node.is_control_plane ? t("nodes.controlPlane") : t("nodes.peer")}
            {node.agent?.version && ` · ${t("nodes.agentVersion", { version: node.agent.version })}`}
          </span>
        }
        actions={<NodeActions node={node} {...actions.actionsFor(node)} />}
      />

      <ErrorLine>{actions.error}</ErrorLine>

      <div className="space-y-3">
        <NodeStatStrip node={node} interfaces={interfaceSummary(node)} stats={live} layout={layout} loading={stats.loading} />

        {live && <NodeCharts stats={live} history={history} />}

        {live && reachable && (
          <Row wide>
            <NodeProcesses stats={live} onChanged={stats.refresh} />
            {hasStorage && <NodeStorage stats={live} />}
          </Row>
        )}

        <Row>
          <Section title={t("fleet.fabricPorts")}>
            <ErrorLine>{fabricError}</ErrorLine>
            {fabric || fabricError ? (
              <NodeFabricPorts
                node={fabric?.nodes.find((n) => n.node_id === node.id)}
                plan={fabric?.plan?.nodes.find((n) => n.node_id === node.id)}
              />
            ) : (
              <Spinner size="sm" label={t("common.loading")} />
            )}
          </Section>
          <Section title={t("nodesPage.findings")} meta={nodeFindings.length > 0 ? String(nodeFindings.length) : undefined}>
            {nodeFindings.length > 0 ? (
              <div className="space-y-1.5" data-testid={`node-finding-${node.id}`}>
                {nodeFindings.map((finding) => (
                  <FindingLine key={finding.code} finding={finding} />
                ))}
              </div>
            ) : (
              <p className="text-[12.5px] text-muted">{t("nodesPage.noFindings")}</p>
            )}
          </Section>
        </Row>

        <Section
          title={t("fleet.doctorSection")}
          actions={
            <Button
              size="sm"
              icon={checksRun > 0 ? RefreshCw : Stethoscope}
              onClick={() => setChecksRun((n) => n + 1)}
            >
              {checksRun > 0 ? t("nodesPage.runChecksAgain") : t("nodesPage.runChecks")}
            </Button>
          }
        >
          {checksRun > 0 ? (
            <NodeDoctor key={checksRun} node={node} onChanged={reload} />
          ) : (
            <p className="text-[12.5px] text-muted">{t("nodesPage.checksIdle")}</p>
          )}
        </Section>
      </div>

      {actions.dialogs}
    </div>
  );
}
