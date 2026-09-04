/** The Cluster page: a node registry, and the deployments running on it.
 *
 * What this page used to be was a front end for the cluster orchestrator —
 * start, stop, validate, rollback, and a list of clusters read off container
 * labels. That orchestrator is gone. A cluster is a deployment of size N, so
 * the two questions this page answers are now answered by two surviving APIs:
 *
 *   which machines do we have?   → `/api/nodes`, rendered by <NodeRegistry />
 *   what is running on them?     → `/api/deployments`, rendered below
 *
 * Deploying is not done from here. It is one deploy form on the Recipes page,
 * whatever the node count, which is the whole point of the convergence.
 */

import { useEffect } from "react";
import { fetchDeployments } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import StatusBadge from "@/components/StatusBadge";
import LaunchScriptAnalyzer from "@/components/LaunchScriptAnalyzer";
import NodeRegistry from "@/components/NodeRegistry";
import { Server, AlertCircle, Loader2 } from "lucide-react";
import { setRefresh } from "@/lib/refresh";
import type { Deployment } from "@/lib/types";
import { ExperimentalBadge, ExperimentalBanner } from "@/components/Experimental";
import {
  MULTI_NODE_BADGE_TITLE,
  MULTI_NODE_REASON,
  MULTI_NODE_TITLE,
  MULTI_NODE_UNPROVEN,
  nodeCount,
} from "@/lib/experimental";
import { useConfig } from "@/lib/config";

/** Where a deployment's ranks run, named rather than counted. */
function placement(deployment: Deployment): string {
  const nodes = deployment.nodes ?? [];
  if (nodes.length === 0) return "this node";
  return nodes.join(", ");
}

export default function ClusterPage() {
  const { data: deployments, loading, error, refetch } = useQuery<Deployment[]>(fetchDeployments);
  const { config } = useConfig();
  const experimental = config?.cluster_experimental ?? true;

  useEffect(() => { setRefresh(refetch); }, [refetch]);
  useEffect(() => { const i = setInterval(refetch, 15000); return () => clearInterval(i); }, [refetch]);

  const live = (deployments ?? []).filter((d) => d.status !== "stopped" && d.status !== "error");

  return (
    <div className="space-y-6">
      {experimental && (
        <ExperimentalBanner
          title={MULTI_NODE_TITLE}
          reason={MULTI_NODE_REASON}
          items={MULTI_NODE_UNPROVEN}
        />
      )}

      {/* Header */}
      <div>
        <h2 className="text-2xl font-bold">Cluster Orchestration</h2>
        <p className="text-text-muted mt-1">
          The machines Spark Pulse knows about, and what is running on them. Deploy from the
          Recipes page — a cluster is a deployment with more than one node.
        </p>
      </div>

      {/* The node registry — what used to be two free-text IP boxes. */}
      <NodeRegistry />

      {/* Deployments, which is what "cluster status" became. */}
      <div className="rounded-xl bg-surface border border-border p-4 space-y-3" data-testid="cluster-deployments">
        <div className="flex items-center gap-2">
          <Server size={18} className="text-primary" />
          <h3 className="text-lg font-semibold">Deployments</h3>
        </div>

        {loading && (
          <div className="flex justify-center py-10">
            <Loader2 className="animate-spin text-primary" size={28} />
          </div>
        )}
        {error && (
          <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3">
            <AlertCircle size={20} />
            <span>{error}</span>
          </div>
        )}

        {!loading && !error && live.length === 0 && (
          <p className="text-sm text-text-muted py-6 text-center">
            Nothing is running. Deploy a recipe to start one.
          </p>
        )}

        {live.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-text-muted">
                  <th className="py-2 pr-4 font-medium">Deployment</th>
                  <th className="py-2 pr-4 font-medium">Nodes</th>
                  <th className="py-2 pr-4 font-medium">Placement</th>
                  <th className="py-2 pr-4 font-medium">Engine</th>
                  <th className="py-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {live.map((deployment) => (
                  <tr key={deployment.id} className="border-t border-border">
                    <td className="py-2 pr-4">
                      <p className="font-medium">{deployment.name}</p>
                      <p className="text-xs text-text-muted">{deployment.recipe_id}</p>
                    </td>
                    <td className="py-2 pr-4">
                      <span className="inline-flex items-center gap-1.5">
                        {nodeCount(deployment)}
                        {nodeCount(deployment) > 1 && (
                          <ExperimentalBadge title={MULTI_NODE_BADGE_TITLE} />
                        )}
                      </span>
                    </td>
                    <td className="py-2 pr-4 font-mono text-xs">{placement(deployment)}</td>
                    <td className="py-2 pr-4">
                      {deployment.engine ? `${deployment.engine}/${deployment.variant ?? "default"}` : "—"}
                    </td>
                    <td className="py-2">
                      <StatusBadge status={deployment.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* The launch-script analyser kept its endpoint when the cluster form
          lost its dialog, so it stands on its own rather than disappearing. */}
      <LaunchScriptAnalyzer />
    </div>
  );
}
