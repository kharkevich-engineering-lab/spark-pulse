import React from "react";
import { LockType, type LockInfo } from "@/lib/operations";
import {
  Lock,
  AlertCircle,
  Clock,
  User,
  Wrench,
  Activity,
} from "lucide-react";

// ── Lock Type Icons ──────────────────────────────────────────────────────────

const lockTypeIcons: Record<LockType, typeof Lock> = {
  [LockType.CLUSTER_START]: Activity,
  [LockType.CLUSTER_STOP]: Activity,
  [LockType.MOD_APPLY]: Wrench,
  [LockType.DEPLOYMENT_START]: Activity,
  [LockType.DEPLOYMENT_STOP]: Activity,
  [LockType.RECONCILIATION]: Clock,
};

function getLockTypeLabel(type: LockType): string {
  return type.replace(/_/g, " ").toLowerCase()
    .split(" ")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

// ── Lock Contention Alert ────────────────────────────────────────────────────

interface LockContentionAlertProps {
  lock: LockInfo;
  onRetry?: () => void;
  estimatedWaitSeconds?: number;
  className?: string;
}

export default function LockContentionAlert({
  lock,
  onRetry,
  estimatedWaitSeconds,
  className = "",
}: LockContentionAlertProps) {

  return (
    <div className={`p-4 rounded-lg bg-warning/5 border border-warning/30 ${className}`}>
      <div className="flex items-start gap-3">
        <AlertCircle size={20} className="text-warning shrink-0 mt-0.5" />
        <div className="flex-1">
          <h4 className="text-sm font-semibold text-warning">Resource Locked</h4>
          <p className="text-sm text-text-muted mt-1">
            <span className="font-medium">{lock.resource}</span> is currently being modified.
          </p>

          <div className="flex flex-wrap gap-4 mt-2 text-xs text-text-muted">
            <span className="flex items-center gap-1">
              {React.createElement(lockTypeIcons[lock.lock_type] ?? Lock, { size: 12 })}
              {getLockTypeLabel(lock.lock_type)}
            </span>
            {lock.holder && (
              <span className="flex items-center gap-1">
                <User size={12} />
                {lock.holder}
              </span>
            )}
            {estimatedWaitSeconds && (
              <span className="flex items-center gap-1">
                <Clock size={12} />
                Est. wait: ~{estimatedWaitSeconds}s
              </span>
            )}
          </div>

          {onRetry && (
            <button
              onClick={onRetry}
              className="mt-3 px-3 py-1.5 text-sm rounded-lg bg-warning text-warning-foreground hover:bg-warning/90 transition-colors"
            >
              Try Again
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Lock Status Indicator ────────────────────────────────────────────────────

interface LockStatusIndicatorProps {
  lock: LockInfo;
  className?: string;
}

export function LockStatusIndicator({ lock, className = "" }: LockStatusIndicatorProps) {
  const Icon = lockTypeIcons[lock.lock_type] ?? Lock;

  return (
    <span className={`inline-flex items-center gap-1.5 text-xs ${className}`}>
      <Icon size={12} className="text-warning" />
      <span className="text-text-muted">
        {getLockTypeLabel(lock.lock_type)}
      </span>
      {lock.holder && (
        <span className="text-text-muted/70">
          by {lock.holder}
        </span>
      )}
    </span>
  );
}

// ── Active Locks List ────────────────────────────────────────────────────────

interface ActiveLocksListProps {
  locks: LockInfo[];
  className?: string;
}

export function ActiveLocksList({ locks, className = "" }: ActiveLocksListProps) {
  if (locks.length === 0) {
    return (
      <div className={`text-xs text-text-muted/50 ${className}`}>
        No active locks
      </div>
    );
  }

  return (
    <div className={`space-y-2 ${className}`}>
      <h4 className="text-xs font-semibold text-text-muted uppercase tracking-wider">
        Active Locks ({locks.length})
      </h4>
      {locks.map((lock) => {
        const Icon = lockTypeIcons[lock.lock_type] ?? Lock;
        return (
          <div
            key={`${lock.resource}:${lock.lock_type}`}
            className="flex items-center gap-2 p-2 rounded-lg bg-surface-hover text-xs"
          >
            <Icon size={14} className="text-warning" />
            <span className="font-medium">{lock.resource}</span>
            <span className="text-text-muted/70">— {getLockTypeLabel(lock.lock_type)}</span>
            {lock.holder && (
              <span className="text-text-muted/50 ml-auto">
                {lock.holder}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Lock Info for Cluster Card ───────────────────────────────────────────────

interface ClusterLockInfoProps {
  clusterName: string;
  locks: LockInfo[];
  className?: string;
}

export function ClusterLockInfo({ clusterName, locks, className = "" }: ClusterLockInfoProps) {
  const relevantLocks = locks.filter((l) => l.resource === clusterName);

  if (relevantLocks.length === 0) return null;

  return (
    <div className={`space-y-1 ${className}`}>
      {relevantLocks.map((lock) => (
        <LockStatusIndicator key={`${lock.lock_type}`} lock={lock} />
      ))}
    </div>
  );
}
