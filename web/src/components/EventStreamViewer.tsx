import { useState, useMemo } from "react";
import {
  EventType,
  type DeploymentEvent,
} from "@/lib/operations";
import {
  X,
  Clock,
  AlertCircle,
  Check,
  Play,
  Square,
  RotateCcw,
  Server,
  Wifi,
  Activity,
  Database,
  ShieldCheck,
  Wrench,
  Trash2,
} from "lucide-react";

// ── Event Type Icons ─────────────────────────────────────────────────────────

const eventIcons: Record<EventType, typeof Activity> = {
  [EventType.DEPLOYMENT_START]: Play,
  [EventType.DEPLOYMENT_STOP]: Square,
  [EventType.DEPLOYMENT_SUCCESS]: Check,
  [EventType.DEPLOYMENT_FAILURE]: X,
  [EventType.DEPLOYMENT_CANCELLED]: X,
  [EventType.DEPLOYMENT_ROLLBACK]: RotateCcw,
  [EventType.CONTAINER_START]: Play,
  [EventType.CONTAINER_STOP]: Square,
  [EventType.CONTAINER_RESTART]: RotateCcw,
  [EventType.CONTAINER_CRASH]: X,
  [EventType.RAY_HEAD_START]: Server,
  [EventType.RAY_HEAD_STOP]: Square,
  [EventType.RAY_WORKER_CONNECT]: Wifi,
  [EventType.RAY_WORKER_DISCONNECT]: Wifi,
  [EventType.RAY_CLUSTER_READY]: Check,
  [EventType.HEALTH_CHECK_PASS]: Check,
  [EventType.HEALTH_CHECK_FAIL]: X,
  [EventType.HEALTH_CHECK_WARNING]: AlertCircle,
  [EventType.NCCL_ERROR]: AlertCircle,
  [EventType.NETWORK_VALIDATION_START]: Wifi,
  [EventType.NETWORK_VALIDATION_SUCCESS]: Check,
  [EventType.NETWORK_VALIDATION_FAILURE]: X,
  [EventType.MOD_VALIDATION_START]: ShieldCheck,
  [EventType.MOD_VALIDATION_SUCCESS]: ShieldCheck,
  [EventType.MOD_VALIDATION_FAILURE]: X,
  [EventType.MOD_APPLY_START]: Wrench,
  [EventType.MOD_APPLY_SUCCESS]: Check,
  [EventType.MOD_APPLY_FAILURE]: X,
  [EventType.MOD_ROLLBACK_START]: RotateCcw,
  [EventType.MOD_ROLLBACK_SUCCESS]: Check,
  [EventType.CLUSTER_START]: Play,
  [EventType.CLUSTER_STOP]: Square,
  [EventType.CLUSTER_READY]: Check,
  [EventType.CLUSTER_RECONCILED]: Database,
  [EventType.SCRIPT_DISTRIBUTION_START]: Wrench,
  [EventType.SCRIPT_DISTRIBUTION_SUCCESS]: Check,
  [EventType.SCRIPT_DISTRIBUTION_FAILURE]: X,
};

const eventColors: Record<EventType, string> = {
  [EventType.DEPLOYMENT_FAILURE]: "text-danger",
  [EventType.CONTAINER_CRASH]: "text-danger",
  [EventType.HEALTH_CHECK_FAIL]: "text-danger",
  [EventType.NCCL_ERROR]: "text-danger",
  [EventType.MOD_VALIDATION_FAILURE]: "text-danger",
  [EventType.MOD_APPLY_FAILURE]: "text-danger",
  [EventType.DEPLOYMENT_START]: "text-primary",
  [EventType.CLUSTER_START]: "text-primary",
  [EventType.RAY_CLUSTER_READY]: "text-success",
  [EventType.DEPLOYMENT_SUCCESS]: "text-success",
  [EventType.CLUSTER_READY]: "text-success",
  [EventType.MOD_VALIDATION_SUCCESS]: "text-success",
  [EventType.MOD_APPLY_SUCCESS]: "text-success",
  [EventType.SCRIPT_DISTRIBUTION_SUCCESS]: "text-success",
  [EventType.NETWORK_VALIDATION_SUCCESS]: "text-success",
  [EventType.HEALTH_CHECK_WARNING]: "text-warning",
  [EventType.HEALTH_CHECK_PASS]: "text-text-muted",
  [EventType.DEPLOYMENT_STOP]: "text-text-muted",
  [EventType.DEPLOYMENT_CANCELLED]: "text-text-muted",
  [EventType.DEPLOYMENT_ROLLBACK]: "text-text-muted",
  [EventType.CONTAINER_START]: "text-text-muted",
  [EventType.CONTAINER_STOP]: "text-text-muted",
  [EventType.CONTAINER_RESTART]: "text-text-muted",
  [EventType.RAY_HEAD_START]: "text-text-muted",
  [EventType.RAY_HEAD_STOP]: "text-text-muted",
  [EventType.RAY_WORKER_CONNECT]: "text-text-muted",
  [EventType.RAY_WORKER_DISCONNECT]: "text-text-muted",
  [EventType.MOD_VALIDATION_START]: "text-text-muted",
  [EventType.MOD_APPLY_START]: "text-text-muted",
  [EventType.MOD_ROLLBACK_START]: "text-text-muted",
  [EventType.MOD_ROLLBACK_SUCCESS]: "text-text-muted",
  [EventType.CLUSTER_STOP]: "text-text-muted",
  [EventType.CLUSTER_RECONCILED]: "text-text-muted",
  [EventType.SCRIPT_DISTRIBUTION_START]: "text-text-muted",
  [EventType.SCRIPT_DISTRIBUTION_FAILURE]: "text-text-muted",
  [EventType.NETWORK_VALIDATION_FAILURE]: "text-text-muted",
  [EventType.NETWORK_VALIDATION_START]: "text-text-muted",
};

function getEventColor(eventType: EventType): string {
  return eventColors[eventType] ?? "text-text-muted";
}

function getEventLabel(eventType: EventType): string {
  return eventType.replace(/_/g, " ").toLowerCase()
    .split(" ")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

// ── Event Stream Viewer ──────────────────────────────────────────────────────

interface EventStreamViewerProps {
  events: DeploymentEvent[];
  resource: string;
  onClear?: () => void;
  className?: string;
}

export default function EventStreamViewer({
  events,
  onClear,
  className = "",
}: EventStreamViewerProps) {
  const [filterSeverity, setFilterSeverity] = useState<string>("all");
  const [filterNode, setFilterNode] = useState<string>("all");

  const filteredEvents = useMemo(() => {
    return events.filter((e) => {
      if (filterSeverity !== "all" && e.severity !== filterSeverity) return false;
      if (filterNode !== "all" && e.node !== filterNode) return false;
      return true;
    });
  }, [events, filterSeverity, filterNode]);

  const uniqueNodes = useMemo(() => {
    const nodes = new Set(events.map((e) => e.node).filter(Boolean));
    return Array.from(nodes).sort();
  }, [events]);

  return (
    <div className={`space-y-4 ${className}`}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Activity size={20} className="text-primary" />
          <h3 className="text-lg font-semibold">Event Stream</h3>
          <span className="text-xs text-text-muted bg-surface-hover px-2 py-0.5 rounded-full">
            {filteredEvents.length} events
          </span>
        </div>
        {onClear && events.length > 0 && (
          <button
            onClick={onClear}
            className="flex items-center gap-1 px-2 py-1 text-sm rounded-lg hover:bg-surface-hover transition-colors"
          >
            <Trash2 size={14} />
            Clear
          </button>
        )}
      </div>

      {/* Filters */}
      <div className="flex gap-2">
        <select
          value={filterSeverity}
          onChange={(e) => setFilterSeverity(e.target.value)}
          className="px-2 py-1 text-sm rounded-lg border border-border bg-surface focus:outline-none focus:ring-2 focus:ring-primary/50"
        >
          <option value="all">All Severities</option>
          <option value="error">Errors</option>
          <option value="warning">Warnings</option>
          <option value="info">Info</option>
        </select>

        <select
          value={filterNode}
          onChange={(e) => setFilterNode(e.target.value)}
          className="px-2 py-1 text-sm rounded-lg border border-border bg-surface focus:outline-none focus:ring-2 focus:ring-primary/50"
        >
          <option value="all">All Nodes</option>
          {uniqueNodes.map((node) => (
            <option key={node} value={node}>{node}</option>
          ))}
        </select>
      </div>

      {/* Event Timeline */}
      <div className="space-y-1 max-h-96 overflow-y-auto">
        {filteredEvents.length === 0 ? (
          <div className="p-8 text-center text-text-muted text-sm">
            No events to display
          </div>
        ) : (
          filteredEvents.map((event) => {
            const Icon = eventIcons[event.event_type] ?? Activity;
            const color = getEventColor(event.event_type);

            return (
              <div
                key={event.event_id}
                className="flex items-start gap-3 p-2 rounded-lg hover:bg-surface-hover transition-colors"
              >
                <Icon size={14} className={`shrink-0 mt-0.5 ${color}`} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{getEventLabel(event.event_type)}</span>
                    {event.node && (
                      <span className="text-xs text-text-muted bg-surface-hover px-1.5 py-0.5 rounded">
                        {event.node}
                      </span>
                    )}
                    {event.actor && (
                      <span className="text-xs text-text-muted/70">
                        by {event.actor}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-text-muted mt-0.5">{event.message}</p>
                  <div className="flex items-center gap-2 mt-1">
                    <Clock size={10} className="text-text-muted/50" />
                    <span className="text-xs text-text-muted/50">
                      {new Date(event.timestamp).toLocaleTimeString()}
                    </span>
                    {event.correlation_id && (
                      <span className="text-xs text-text-muted/50 font-mono">
                        {event.correlation_id.slice(0, 8)}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* Retention Policy */}
      <div className="text-xs text-text-muted/50 border-t border-border pt-2">
        Events older than 30 days are automatically archived. Maximum 1000 events per resource.
      </div>
    </div>
  );
}

// ── Event Timeline Visual ────────────────────────────────────────────────────

interface EventTimelineProps {
  events: DeploymentEvent[];
  className?: string;
}

export function EventTimeline({ events, className = "" }: EventTimelineProps) {
  const lifecycleEvents = events.filter((e) =>
    [
      EventType.SCRIPT_DISTRIBUTION_START,
      EventType.SCRIPT_DISTRIBUTION_SUCCESS,
      EventType.RAY_HEAD_START,
      EventType.RAY_CLUSTER_READY,
      EventType.DEPLOYMENT_START,
      EventType.DEPLOYMENT_SUCCESS,
      EventType.DEPLOYMENT_FAILURE,
    ].includes(e.event_type)
  );

  if (lifecycleEvents.length === 0) return null;

  return (
    <div className={`space-y-3 ${className}`}>
      <h3 className="text-sm font-semibold text-text-muted uppercase tracking-wider">
        Deployment Timeline
      </h3>
      <div className="relative pl-6 border-l-2 border-border space-y-4">
        {lifecycleEvents.map((event) => {
          const Icon = eventIcons[event.event_type] ?? Activity;
          const color = getEventColor(event.event_type);

          return (
            <div key={event.event_id} className="relative">
              <div className={`absolute -left-[25px] p-1 rounded-full bg-surface border ${color.replace("text-", "border-")}`}>
                <Icon size={12} className={color} />
              </div>
              <div>
                <span className="text-sm font-medium">{getEventLabel(event.event_type)}</span>
                <p className="text-xs text-text-muted mt-0.5">{event.message}</p>
                <span className="text-xs text-text-muted/50">
                  {new Date(event.timestamp).toLocaleTimeString()}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
