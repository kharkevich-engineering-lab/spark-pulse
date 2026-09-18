/** Fleet: the machines, and what they are doing.
 *
 * Two questions about the same set of hardware, so one page with two tabs
 * rather than two entries in a nav. **Nodes** is the registry, the ConnectX
 * fabric and the doctor — everything about whether a machine is there and
 * answering. **Monitoring** is what each of them is doing right now, which
 * used to be `/monitoring`, a separate page with no way to tell from it which
 * machine you were looking at.
 *
 * Both routes still exist and both still work: `/cluster` opens on Nodes,
 * `/monitoring` opens on Monitoring, and switching tabs rewrites the address
 * so a tab is a link somebody can send.
 *
 * What is *not* here any more is the deployments table. "What is running on
 * my machines" is a question about runs, and it is answered on Runs — once,
 * where the controls to act on it are, rather than twice with half the
 * actions in one of the two places.
 */

import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Plus } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { fetchNodes } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import type { ClusterNode } from "@/lib/types";
import { Button, PageHeader, Tabs } from "@/ui";
import NodeRegistry from "@/components/NodeRegistry";
import NetworkDiscovery from "@/components/NetworkDiscovery";
import FabricCard from "@/components/FabricCard";
import MonitoringTab from "@/components/MonitoringTab";
import { ExperimentalNote } from "@/components/Experimental";
import { useConfig } from "@/lib/config";

type FleetTab = "nodes" | "monitoring";

/** The route each tab is reachable at, in both directions. */
const PATH_FOR: Record<FleetTab, string> = {
  nodes: "/cluster",
  monitoring: "/monitoring",
};

export function tabForPath(pathname: string): FleetTab {
  return pathname === "/monitoring" ? "monitoring" : "nodes";
}

export default function FleetPage() {
  const { t, plural } = useI18n();
  const { config } = useConfig();
  const location = useLocation();
  const navigate = useNavigate();
  const experimental = config?.cluster_experimental ?? true;

  const tab = tabForPath(location.pathname);
  /** How many machines, for the tab count and the one-line description.
   *
   *  The page asks rather than taking the number from the registry below it:
   *  the registry is only mounted on the Nodes tab, and a header that said
   *  "0 nodes" the moment you opened Monitoring would be a lie about the very
   *  thing the page is named for. */
  const { data: nodes } = useQuery<ClusterNode[]>(fetchNodes);
  const nodeCount = nodes?.length ?? null;
  const [addOpen, setAddOpen] = useState(false);

  // A tab is an address. Replace rather than push: flipping between two tabs
  // should not leave a back button that walks through every flip.
  const select = (next: string) => {
    const path = PATH_FOR[next as FleetTab] ?? PATH_FOR.nodes;
    if (path !== location.pathname) navigate(path, { replace: true });
  };

  // Opening Add from the header only makes sense on the tab that holds the
  // registry, so going to Monitoring closes it rather than leaving a dialog
  // attached to a section that is no longer rendered.
  useEffect(() => {
    if (tab !== "nodes") setAddOpen(false);
  }, [tab]);

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t("nav.fleet")}
        title={t("cluster.heading")}
        description={
          <>
            {plural("fleet.nodes", nodeCount ?? 0)} · {t("fleet.runsPrefix")}{" "}
            <Link to="/jobs">{t("nav.runs")}</Link>
          </>
        }
        actions={
          <Button
            variant="primary"
            icon={Plus}
            onClick={() => {
              if (tab !== "nodes") navigate(PATH_FOR.nodes, { replace: true });
              setAddOpen(true);
            }}
          >
            {t("nodes.addNode")}
          </Button>
        }
      />

      {/* One line, not the full banner. What is unproven and why belongs where
          an operator is about to deploy across machines, which is the deploy
          form and the expanded row on Runs. */}
      {experimental && <ExperimentalNote text={t("cluster.experimental")} />}

      <Tabs
        label={t("nav.fleet")}
        value={tab}
        onChange={select}
        tabs={[
          { id: "nodes", label: t("fleet.tabNodes"), count: nodeCount ?? undefined },
          { id: "monitoring", label: t("fleet.tabMonitoring") },
        ]}
      />

      {tab === "nodes" ? (
        <div className="space-y-0">
          {/* The machines this control plane knows about — what used to be two
              free-text IP boxes. */}
          <NodeRegistry addOpen={addOpen} onAddOpenChange={setAddOpen} />

          {/* Every node's ConnectX ports as its agent reports them, and the
              addresses each should hold. Configuring them is done from here. */}
          <FabricCard />

          {/* Browsing the LAN is the last thing on the page, collapsed: it is
              how you find a machine you have not registered, not something an
              operator reads on the way past. */}
          <NetworkDiscovery />
        </div>
      ) : (
        <MonitoringTab />
      )}
    </div>
  );
}
