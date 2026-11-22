/** Nodes: every machine, what it is doing, and the cables between them.
 *
 * One full-width table of nodes with their live memory, GPU and agent; a
 * node's name opens its own page (`/cluster/:id`) with its stats, history,
 * GPU processes, fabric ports and the doctor. Monitoring used to be a page of
 * its own describing the same machines a second time, and the two disagreed
 * (a node "Unknown" on one and "Healthy" on the other); `/monitoring` now
 * opens this page.
 */

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Plus } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { fetchNodes } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { useNodeStats } from "@/hooks/useNodeStats";
import { isLiveRun, useDeployments } from "@/hooks/useDeployments";
import { memoryLayout, runColors, type MemoryLayout } from "@/lib/memoryLayout";
import type { ClusterNode } from "@/lib/types";
import { Button, ErrorLine, PageHeader } from "@/ui";
import NodeRegistry from "@/components/NodeRegistry";
import NetworkDiscovery from "@/components/NetworkDiscovery";
import FabricCard from "@/components/FabricCard";

export default function FleetPage() {
  const { t, plural } = useI18n();
  const { data: nodes } = useQuery<ClusterNode[]>(fetchNodes);
  const stats = useNodeStats();
  const { deployments } = useDeployments();
  const [addOpen, setAddOpen] = useState(false);

  const live = useMemo(() => (deployments ?? []).filter(isLiveRun), [deployments]);
  const layouts = useMemo(() => {
    const colors = runColors(live);
    const out: Record<string, MemoryLayout | null> = {};
    for (const s of stats.nodes) out[s.id] = memoryLayout(s, live, colors);
    return out;
  }, [stats.nodes, live]);

  const healthy = (nodes ?? []).filter((n) => n.state === "healthy").length;

  return (
    <div className="space-y-3">
      <PageHeader
        eyebrow={t("nav.nodes")}
        title={t("cluster.heading")}
        description={
          <>
            {nodes && (
              <>
                {plural("fleet.nodes", nodes.length)} · {t("nodesPage.healthy", { count: healthy })} ·{" "}
              </>
            )}
            {t("fleet.runsPrefix")} <Link to="/jobs">{t("nav.runs")}</Link>
          </>
        }
        actions={
          <Button variant="primary" icon={Plus} onClick={() => setAddOpen(true)}>
            {t("nodes.addNode")}
          </Button>
        }
      />

      <ErrorLine>{stats.error}</ErrorLine>

      <NodeRegistry
        addOpen={addOpen}
        onAddOpenChange={setAddOpen}
        stats={stats.nodes}
        layouts={layouts}
      />

      <FabricCard />

      {/* How a machine not yet registered is found: last, and folded. */}
      <NetworkDiscovery />
    </div>
  );
}
