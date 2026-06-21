import {
  Server,
  Cpu,
  AlertTriangle,
  CheckCircle2,
} from "lucide-react";
import type { ClusterCapacity, NodeCapacity } from "@/lib/operations";

// ── Allocation Bar ───────────────────────────────────────────────────────────

interface AllocationBarProps {
  label: string;
  allocated: number;
  total: number;
  unit: string;
  warningThreshold?: number;
  className?: string;
}

function AllocationBar({
  label,
  allocated,
  total,
  unit,
  warningThreshold = 90,
  className = "",
}: AllocationBarProps) {
  const percentage = total > 0 ? (allocated / total) * 100 : 0;
  const isWarning = percentage >= warningThreshold;
  const isCritical = percentage >= 95;

  return (
    <div className={`space-y-1 ${className}`}>
      <div className="flex items-center justify-between text-xs">
        <span className="text-text-muted">{label}</span>
        <span className={isCritical ? "text-danger" : isWarning ? "text-warning" : "text-text-muted"}>
          {allocated}/{total} {unit} ({Math.round(percentage)}%)
        </span>
      </div>
      <div className="h-2 rounded-full bg-surface-hover overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-300 ${
            isCritical ? "bg-danger" : isWarning ? "bg-warning" : "bg-primary"
          }`}
          style={{ width: `${Math.min(percentage, 100)}%` }}
        />
      </div>
    </div>
  );
}

// ── Node Capacity Card ───────────────────────────────────────────────────────

interface NodeCapacityCardProps {
  node: NodeCapacity;
  className?: string;
}

function NodeCapacityCard({ node, className = "" }: NodeCapacityCardProps) {
  return (
    <div className={`p-4 rounded-lg border border-border bg-surface ${className}`}>
      <div className="flex items-center gap-2 mb-3">
        <Server size={16} className="text-primary" />
        <div>
          <span className="text-sm font-semibold">{node.node_ip}</span>
          <span className="text-xs text-text-muted ml-2">
            ({node.role})
          </span>
        </div>
      </div>

      <div className="space-y-3">
        <AllocationBar
          label="GPU"
          allocated={node.allocated_gpus}
          total={node.total_gpus}
          unit="GPU"
        />
        <AllocationBar
          label="RAM"
          allocated={node.allocated_ram_gb}
          total={node.total_ram_gb}
          unit="GB"
        />
        <AllocationBar
          label="CPU"
          allocated={node.allocated_cpu_cores}
          total={node.total_cpu_cores}
          unit="cores"
        />
      </div>

      {/* Active Deployments */}
      {node.active_deployments.length > 0 && (
        <div className="mt-3 pt-3 border-t border-border">
          <span className="text-xs text-text-muted">Deployments:</span>
          <div className="flex flex-wrap gap-1 mt-1">
            {node.active_deployments.map((dep) => (
              <span
                key={dep}
                className="text-xs px-2 py-0.5 rounded-full bg-primary/10 text-primary"
              >
                {dep}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Cluster Capacity Panel ───────────────────────────────────────────────────

interface ClusterCapacityPanelProps {
  capacity: ClusterCapacity;
  className?: string;
}

export default function ClusterCapacityPanel({
  capacity,
  className = "",
}: ClusterCapacityPanelProps) {
  const isWarning = capacity.utilization_percent >= 90;
  const isCritical = capacity.utilization_percent >= 95;

  return (
    <div className={`space-y-4 ${className}`}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Cpu size={20} className="text-primary" />
          <h3 className="text-lg font-semibold">Cluster Capacity</h3>
        </div>
        <div className={`flex items-center gap-1 text-sm ${
          isCritical ? "text-danger" : isWarning ? "text-warning" : "text-success"
        }`}>
          {isCritical || isWarning ? (
            <AlertTriangle size={16} />
          ) : (
            <CheckCircle2 size={16} />
          )}
          <span className="font-medium">
            {Math.round(capacity.utilization_percent)}% utilized
          </span>
        </div>
      </div>

      {/* Summary */}
      <div className="grid grid-cols-3 gap-3">
        <div className="p-3 rounded-lg bg-surface text-center">
          <span className="text-xs text-text-muted block">Total GPUs</span>
          <span className="text-xl font-bold">{capacity.total_gpus}</span>
        </div>
        <div className="p-3 rounded-lg bg-surface text-center">
          <span className="text-xs text-text-muted block">Allocated</span>
          <span className={`text-xl font-bold ${
            isCritical ? "text-danger" : isWarning ? "text-warning" : "text-primary"
          }`}>
            {capacity.allocated_gpus}
          </span>
        </div>
        <div className="p-3 rounded-lg bg-surface text-center">
          <span className="text-xs text-text-muted block">Free</span>
          <span className="text-xl font-bold text-success">
            {capacity.free_gpus}
          </span>
        </div>
      </div>

      {/* Warning */}
      {isCritical && (
        <div className="p-3 rounded-lg bg-danger/5 border border-danger/30 flex items-center gap-2">
          <AlertTriangle size={16} className="text-danger" />
          <span className="text-sm text-danger">
            Cluster is critically overloaded ({Math.round(capacity.utilization_percent)}% GPU utilization). Consider removing deployments or adding nodes.
          </span>
        </div>
      )}

      {isWarning && !isCritical && (
        <div className="p-3 rounded-lg bg-warning/5 border border-warning/30 flex items-center gap-2">
          <AlertTriangle size={16} className="text-warning" />
          <span className="text-sm text-warning">
            Cluster is nearly at capacity ({Math.round(capacity.utilization_percent)}% GPU utilization).
          </span>
        </div>
      )}

      {/* Node Details */}
      <div>
        <h4 className="text-sm font-semibold text-text-muted uppercase tracking-wider mb-3">
          Node Details
        </h4>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {capacity.nodes.map((node) => (
            <NodeCapacityCard key={node.node_ip} node={node} />
          ))}
        </div>
      </div>
    </div>
  );
}
