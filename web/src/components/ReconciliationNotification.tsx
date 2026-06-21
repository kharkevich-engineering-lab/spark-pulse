import { useState, useCallback } from "react";
import {
  AlertCircle,
  Check,
  Loader2,
  X,
  Database,
  Trash2,
} from "lucide-react";

// ── Reconciliation Result Types ──────────────────────────────────────────────

export interface ReconciliationResult {
  reconstructed_clusters: ReconstructedCluster[];
  orphaned_containers: OrphanedContainer[];
  last_reconciliation?: string;
}

export interface ReconstructedCluster {
  name: string;
  head_ip: string;
  worker_ips: string[];
  source: "docker_labels";
}

export interface OrphanedContainer {
  container_id: string;
  container_name: string;
  reason: string;
}

// ── Reconciliation Notification ──────────────────────────────────────────────

interface ReconciliationNotificationProps {
  result: ReconciliationResult | null;
  onCleanOrphans?: (containerIds: string[]) => Promise<void>;
  onDismiss?: () => void;
  className?: string;
}

export default function ReconciliationNotification({
  result,
  onCleanOrphans,
  onDismiss,
  className = "",
}: ReconciliationNotificationProps) {
  const [cleaning, setCleaning] = useState<string | null>(null);
  const [selectedOrphans, setSelectedOrphans] = useState<Set<string>>(new Set());

  if (!result) return null;

  const hasReconstructed = result.reconstructed_clusters.length > 0;
  const hasOrphans = result.orphaned_containers.length > 0;

  if (!hasReconstructed && !hasOrphans) return null;

  const handleCleanSelected = useCallback(async () => {
    if (!onCleanOrphans || selectedOrphans.size === 0) return;
    setCleaning("all");
    try {
      await onCleanOrphans(Array.from(selectedOrphans));
      setSelectedOrphans(new Set());
    } catch {
      // Error handling via parent
    } finally {
      setCleaning(null);
    }
  }, [onCleanOrphans, selectedOrphans]);

  const handleCleanOne = useCallback(async (containerId: string) => {
    if (!onCleanOrphans) return;
    setCleaning(containerId);
    try {
      await onCleanOrphans([containerId]);
      setSelectedOrphans((prev) => {
        const next = new Set(prev);
        next.delete(containerId);
        return next;
      });
    } catch {
      // Error handling via parent
    } finally {
      setCleaning(null);
    }
  }, [onCleanOrphans]);

  return (
    <div className={`space-y-3 ${className}`}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Database size={18} className="text-warning" />
          <h4 className="text-sm font-semibold">Reconciliation Complete</h4>
        </div>
        <button
          onClick={onDismiss}
          className="p-1 rounded hover:bg-surface-hover transition-colors"
        >
          <X size={14} />
        </button>
      </div>

      {/* Reconstructed Clusters */}
      {hasReconstructed && (
        <div className="p-3 rounded-lg bg-success/5 border border-success/30">
          <div className="flex items-center gap-2 mb-2">
            <Check size={14} className="text-success" />
            <span className="text-sm font-medium text-success">
              {result.reconstructed_clusters.length} cluster(s) reconstructed from Docker labels
            </span>
          </div>
          <div className="space-y-1">
            {result.reconstructed_clusters.map((cluster) => (
              <div key={cluster.name} className="text-xs text-text-muted pl-6">
                <span className="font-medium">{cluster.name}</span>
                <span className="ml-2">head: {cluster.head_ip}</span>
                {cluster.worker_ips.length > 0 && (
                  <span className="ml-2">workers: {cluster.worker_ips.join(", ")}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Orphaned Containers */}
      {hasOrphans && (
        <div className="p-3 rounded-lg bg-warning/5 border border-warning/30">
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-2">
              <AlertCircle size={14} className="text-warning" />
              <span className="text-sm font-medium text-warning">
                {result.orphaned_containers.length} orphaned container(s) detected
              </span>
            </div>
            {result.orphaned_containers.length > 0 && (
              <button
                onClick={handleCleanSelected}
                disabled={cleaning !== null}
                className="flex items-center gap-1 px-2 py-1 text-xs rounded-lg bg-danger/10 text-danger hover:bg-danger/20 transition-colors disabled:opacity-50"
              >
                <Trash2 size={12} />
                Clean All
              </button>
            )}
          </div>

          <div className="space-y-1">
            {result.orphaned_containers.map((container) => (
              <div
                key={container.container_id}
                className="flex items-center justify-between text-xs pl-6"
              >
                <div className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={selectedOrphans.has(container.container_id)}
                    onChange={(e) => {
                      setSelectedOrphans((prev) => {
                        const next = new Set(prev);
                        if (e.target.checked) {
                          next.add(container.container_id);
                        } else {
                          next.delete(container.container_id);
                        }
                        return next;
                      });
                    }}
                    className="rounded border-border"
                  />
                  <span className="font-mono">{container.container_name}</span>
                  <span className="text-text-muted/70">({container.reason})</span>
                </div>
                <button
                  onClick={() => handleCleanOne(container.container_id)}
                  disabled={cleaning === container.container_id}
                  className="flex items-center gap-1 text-danger hover:underline disabled:opacity-50"
                >
                  {cleaning === container.container_id ? (
                    <Loader2 size={12} className="animate-spin" />
                  ) : (
                    <Trash2 size={12} />
                  )}
                  Remove
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Last Reconciliation Time */}
      {result.last_reconciliation && (
        <p className="text-xs text-text-muted/50">
          Last reconciliation: {new Date(result.last_reconciliation).toLocaleString()}
        </p>
      )}
    </div>
  );
}

// ── Reconciliation Status Badge ──────────────────────────────────────────────

interface ReconciliationStatusBadgeProps {
  lastReconciliation?: string;
  className?: string;
}

export function ReconciliationStatusBadge({
  lastReconciliation,
  className = "",
}: ReconciliationStatusBadgeProps) {
  if (!lastReconciliation) {
    return (
      <span className={`text-xs text-text-muted/50 ${className}`}>
        Not reconciled
      </span>
    );
  }

  const timeAgo = getTimeAgo(new Date(lastReconciliation));

  return (
    <span className={`inline-flex items-center gap-1 text-xs text-text-muted ${className}`}>
      <Database size={12} />
      Reconciled {timeAgo}
    </span>
  );
}

function getTimeAgo(date: Date): string {
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
