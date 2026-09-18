/** The Fleet page: the machines, their fabric, and how one is added.
 *
 * What this page used to be was a front end for the cluster orchestrator —
 * start, stop, validate, rollback, and a list of clusters read off container
 * labels. That orchestrator is gone. A cluster is a deployment of size N, so
 * this page answers one question — *which machines do we have?* — from
 * `/api/nodes`, and the other one moved: what is running on them is the Runs
 * page's list, which is the only one now. This page carried a second table of
 * the live deployments on its own fifteen-second poll, so an operator reading
 * the two could watch them disagree about the same endpoint.
 *
 * Deploying is not done from here either. It is one deploy form on the
 * Recipes page, whatever the node count, which is the whole point of the
 * convergence.
 */

import { Link } from "react-router-dom";
import { useI18n } from "@/lib/i18n";
import { PageHeader } from "@/ui";
import NodeRegistry from "@/components/NodeRegistry";
import NetworkDiscovery from "@/components/NetworkDiscovery";
import FabricCard from "@/components/FabricCard";
import { ExperimentalNote } from "@/components/Experimental";
import { useConfig } from "@/lib/config";

export default function ClusterPage() {
  const { t } = useI18n();
  const { config } = useConfig();
  const experimental = config?.cluster_experimental ?? true;

  return (
    <div className="space-y-6">
      {/* One line, not the full banner. This page is read rather than acted
          on; what is unproven and why belongs where an operator is about to
          deploy across machines, which is the deploy form and the expanded
          row on Runs. */}
      <PageHeader
        eyebrow={t("nav.fleet")}
        title={t("cluster.heading")}
        description={
          <>
            {t("cluster.subtitle")}{" "}
            <Link to="/jobs" className="text-blue2 hover:underline">
              {t("runs.seeAll")}
            </Link>
            {experimental && (
              <ExperimentalNote className="mt-2" text={t("cluster.experimental")} />
            )}
          </>
        }
      />

      {/* The node registry — what used to be two free-text IP boxes. */}
      <NodeRegistry />

      {/* Every node's ConnectX ports as its agent reports them, and the netplan
          file each should hold. Configuring them is done from here. */}
      <FabricCard />

      {/* What this host's fabric actually looks like, and what NCCL will be
          told about it. It was a card in Settings under a Cluster tab; the
          machines are here. */}
      <NetworkDiscovery />
    </div>
  );
}
