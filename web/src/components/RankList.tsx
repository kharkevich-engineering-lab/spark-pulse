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
import { StatusBadge } from "@/ui";
import type { DeploymentOrphan, DeploymentRank } from "@/lib/types";

export interface RankListProps {
  ranks?: DeploymentRank[];
  orphans?: DeploymentOrphan[];
  className?: string;
}

/** True when a rank's live container is known and is not running. */
function isUnhealthy(rank: DeploymentRank): boolean {
  return !!rank.container && !rank.container.running;
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
                className="flex items-start gap-2 p-2 rounded-sm bg-bad/5 border border-bad/30 text-[13px]"
              >
                <AlertTriangle size={14} className="shrink-0 mt-0.5 text-bad" />
                <div className="min-w-0 space-y-0.5">
                  <p>
                    <span className="font-medium">Rank {orphan.rank} could not be confirmed stopped</span>
                    <span className="text-muted"> · </span>
                    <span className="font-mono text-muted">{node}</span>
                    <span className="text-muted"> · </span>
                    <span className="font-mono text-muted">{orphan.container_name}</span>
                  </p>
                  <p className="text-muted">{orphan.reason}.</p>
                  <p className="text-muted">
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
                className={`flex items-center gap-2 px-2 py-1 rounded-md text-[13px] ${
                  unhealthy ? "bg-bad/5 border border-bad/30" : "bg-surface border border-line"
                }`}
              >
                {unhealthy && <AlertTriangle size={12} className="shrink-0 text-bad" />}
                <span className="font-mono shrink-0">rank {rank.rank}</span>
                {rank.is_head && (
                  <span className="shrink-0 px-1.5 py-0.5 rounded-full text-[11px] font-semibold uppercase tracking-[0.14em] bg-blue/15 text-blue2 border border-blue/30">
                    head
                  </span>
                )}
                <span className="text-muted">·</span>
                <span className="font-mono truncate">{rank.node || "this node"}</span>
                <span className="text-muted">·</span>
                <span className="font-mono truncate">{rank.container_name}</span>
                {rank.container && (
                  <>
                    <span className="text-muted">·</span>
                    {/* One status vocabulary: the same dot and word the run's
                        own badge uses, rather than a third set of colours. */}
                    <span title={rank.container.error || undefined}>
                      <StatusBadge status={rank.container.status} />
                    </span>
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
