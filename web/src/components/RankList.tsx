/** The ranks of one native deployment: one container per rank.
 *
 * A native deployment is a gang, not a single process — `node_count` ranks,
 * each its own container, rank 0 the head everything rendezvous through. Two
 * things this view exists to make visible:
 *
 * * **A solo deployment stays quiet.** World size one, on the control node,
 *   healthy, is exactly what every deployment looked like before ranks
 *   existed. Listing it as "rank 0 (head) · this node · <container>" turns a
 *   single-node install into something that reads like a cluster for no
 *   reason, so nothing rank-specific renders unless there is more than one
 *   rank or a rank is unhealthy — an unhealthy solo rank still says so.
 * * **Orphans are surfaced, not footnoted.** A rank we asked to stop and
 *   could not confirm gone leaves its container possibly still running and
 *   its node's ports held — the exact bug class every orphan record exists
 *   to catch. It is shown whenever there is one, independent of whether the
 *   rank list itself is quiet.
 */

import { AlertTriangle } from "lucide-react";
import type { ContainerStatus, DeploymentOrphan, DeploymentRank } from "@/lib/types";

export interface RankListProps {
  ranks?: DeploymentRank[];
  orphans?: DeploymentOrphan[];
  className?: string;
}

/** True when a rank's live container is known and is not running. */
function isUnhealthy(rank: DeploymentRank): boolean {
  return !!rank.container && !rank.container.running;
}

function containerTone(container: ContainerStatus): string {
  if (container.running) return "text-success";
  if (container.status === "missing") return "text-danger";
  return "text-warning";
}

function ContainerState({ container }: { container: ContainerStatus }) {
  return (
    <span className={`font-mono ${containerTone(container)}`} title={container.error || undefined}>
      {container.status}
    </span>
  );
}

export default function RankList({ ranks, orphans, className = "" }: RankListProps) {
  const list = ranks ?? [];
  const orphanList = orphans ?? [];
  const anyUnhealthy = list.some(isUnhealthy);
  const showRanks = list.length >= 2 || anyUnhealthy;

  if (!showRanks && orphanList.length === 0) return null;

  return (
    <div className={`space-y-2 ${className}`} data-testid="rank-list">
      {orphanList.length > 0 && (
        <ul className="space-y-1.5" data-testid="rank-orphans">
          {orphanList.map((orphan) => {
            const node = orphan.node || "this node";
            return (
              <li
                key={orphan.rank}
                data-testid="rank-orphan"
                className="flex items-start gap-2 p-2 rounded-lg bg-danger/5 border border-danger/30 text-xs"
              >
                <AlertTriangle size={14} className="shrink-0 mt-0.5 text-danger" />
                <div className="min-w-0 space-y-0.5">
                  <p>
                    <span className="font-medium">Rank {orphan.rank} could not be confirmed stopped</span>
                    <span className="text-text-muted"> · </span>
                    <span className="font-mono text-text-muted">{node}</span>
                    <span className="text-text-muted"> · </span>
                    <span className="font-mono text-text-muted">{orphan.container_name}</span>
                  </p>
                  <p className="text-text-muted">{orphan.reason}.</p>
                  <p className="text-text-muted">
                    Its container may still be running and holding {node}&apos;s ports until this
                    clears.
                  </p>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {showRanks && (
        <ul className="space-y-1" data-testid="rank-rows">
          {list.map((rank) => {
            const unhealthy = isUnhealthy(rank);
            return (
              <li
                key={rank.rank}
                data-testid={`rank-row-${rank.rank}`}
                className={`flex items-center gap-2 px-2 py-1 rounded-lg text-xs ${
                  unhealthy ? "bg-danger/5 border border-danger/30" : "bg-surface border border-border"
                }`}
              >
                {unhealthy && <AlertTriangle size={12} className="shrink-0 text-danger" />}
                <span className="font-mono shrink-0">rank {rank.rank}</span>
                {rank.is_head && (
                  <span className="shrink-0 px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wide bg-primary/15 text-primary border border-primary/30">
                    head
                  </span>
                )}
                <span className="text-text-muted">·</span>
                <span className="font-mono truncate">{rank.node || "this node"}</span>
                <span className="text-text-muted">·</span>
                <span className="font-mono truncate">{rank.container_name}</span>
                {rank.container && (
                  <>
                    <span className="text-text-muted">·</span>
                    <ContainerState container={rank.container} />
                  </>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
