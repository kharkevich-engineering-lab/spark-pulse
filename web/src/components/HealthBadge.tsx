import { HealthStatus, type DeploymentHealth } from "@/lib/operations";

interface HealthBadgeProps {
  status: HealthStatus;
  size?: "sm" | "md" | "lg";
  showLabel?: boolean;
  className?: string;
}

const statusConfig = {
  [HealthStatus.HEALTHY]: {
    color: "var(--color-success)",
    bg: "bg-success/10",
    border: "border-success/30",
    label: "Healthy",
    icon: "●",
  },
  [HealthStatus.DEGRADED]: {
    color: "var(--color-warning)",
    bg: "bg-warning/10",
    border: "border-warning/30",
    label: "Degraded",
    icon: "●",
  },
  [HealthStatus.UNHEALTHY]: {
    color: "var(--color-danger)",
    bg: "bg-danger/10",
    border: "border-danger/30",
    label: "Unhealthy",
    icon: "●",
  },
  [HealthStatus.UNKNOWN]: {
    color: "var(--color-text-muted)",
    bg: "bg-surface-hover",
    border: "border-border",
    label: "Unknown",
    icon: "●",
  },
};

export default function HealthBadge({
  status,
  size = "md",
  showLabel = true,
  className = "",
}: HealthBadgeProps) {
  const config = statusConfig[status];
  const sizeClasses = {
    sm: "w-2 h-2",
    md: "w-3 h-3",
    lg: "w-4 h-4",
  };

  return (
    <span className={`inline-flex items-center gap-1.5 ${className}`}>
      <span
        className={`rounded-full ${sizeClasses[size]}`}
        style={{ backgroundColor: config.color }}
      />
      {showLabel && (
        <span className={`text-xs font-medium ${
          status === HealthStatus.HEALTHY ? "text-success" :
          status === HealthStatus.DEGRADED ? "text-warning" :
          status === HealthStatus.UNHEALTHY ? "text-danger" :
          "text-text-muted"
        }`}>
          {config.label}
        </span>
      )}
    </span>
  );
}

// ── Health Alert Component ───────────────────────────────────────────────────

interface HealthAlertProps {
  health: DeploymentHealth;
  className?: string;
}

export function HealthAlert({ health, className = "" }: HealthAlertProps) {
  return (
    <div className={`space-y-2 ${className}`}>
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">{health.deployment_id}</span>
        <HealthBadge status={health.status} size="sm" />
      </div>

      {health.errors.length > 0 && (
        <div className="space-y-1">
          {health.errors.map((err, i) => (
            <p key={i} className="text-xs text-danger">• {err}</p>
          ))}
        </div>
      )}

      {health.warnings.length > 0 && (
        <div className="space-y-1">
          {health.warnings.map((warn, i) => (
            <p key={i} className="text-xs text-warning">• {warn}</p>
          ))}
        </div>
      )}

      <div className="flex gap-4 text-xs text-text-muted">
        {health.restart_count > 0 && (
          <span>Restarts: {health.restart_count}</span>
        )}
        {health.gpu_errors > 0 && (
          <span>GPU Errors: {health.gpu_errors}</span>
        )}
        <span>Last check: {new Date(health.last_check).toLocaleTimeString()}</span>
      </div>
    </div>
  );
}

// ── Health Monitor Controls ──────────────────────────────────────────────────

interface HealthMonitorControlsProps {
  isMonitoring: boolean;
  onToggle: () => void;
  className?: string;
}

export function HealthMonitorControls({
  isMonitoring,
  onToggle,
  className = "",
}: HealthMonitorControlsProps) {
  return (
    <div className={`flex items-center gap-3 ${className}`}>
      <span className="text-sm font-medium">Health Monitoring:</span>
      <button
        onClick={onToggle}
        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
          isMonitoring ? "bg-primary" : "bg-surface-hover"
        }`}
      >
        <span
          className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
            isMonitoring ? "translate-x-6" : "translate-x-1"
          }`}
        />
      </button>
      <span className="text-sm text-text-muted">
        {isMonitoring ? "Active" : "Inactive"}
      </span>
    </div>
  );
}

// ── Health History Chart Placeholder ─────────────────────────────────────────

export function HealthHistoryChart(_deploymentId: string, className = "") {
  // Placeholder for future time-series visualization
  return (
    <div className={`p-8 rounded-lg border border-dashed border-border flex flex-col items-center justify-center ${className}`}>
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="text-text-muted mb-2">
        <path d="M3 3v18h18" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M7 16l4-8 4 4 4-6" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span className="text-sm text-text-muted">Health history chart</span>
      <span className="text-xs text-text-muted/70 mt-1">GPU utilization, restarts, check success rate</span>
    </div>
  );
}
