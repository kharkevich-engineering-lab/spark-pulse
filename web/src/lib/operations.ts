// ── Shared deployment/event/health types ─────────────────────────────────────
//
// This file used to also hold an operation-lifecycle state machine
// (OperationState/OperationStatus/canTransition), a lock manager, an audit
// trail, a dry-run result shape and an SSH error classification — an entire
// unbuilt "operation" subsystem with zero production imports, kept "covered"
// only by its own dedicated tests. It is deleted rather than kept as an
// aspiration; what remains below is what `useSSEConnection`, `EventStreamViewer`
// and `HealthBadge` actually use.

export type OperationResourceType = "cluster" | "deployment" | "mod" | "reconciliation";

// ── SSE Connection State (AF-3) ──────────────────────────────────────────────

export enum SSEConnectionState {
  CONNECTED = "connected",
  RECONNECTING = "reconnecting",
  DISCONNECTED = "disconnected",
}

export interface SSEConnectionStatus {
  state: SSEConnectionState;
  reconnect_attempts: number;
  last_connected_at?: string;
  error?: string;
}

// ── Event Types (Phase 6.6) ─────────────────────────────────────────────────

export enum EventType {
  // Deployment lifecycle
  DEPLOYMENT_START = "deployment_start",
  DEPLOYMENT_STOP = "deployment_stop",
  DEPLOYMENT_SUCCESS = "deployment_success",
  DEPLOYMENT_FAILURE = "deployment_failure",
  DEPLOYMENT_CANCELLED = "deployment_cancelled",
  DEPLOYMENT_ROLLBACK = "deployment_rollback",

  // Container events
  CONTAINER_START = "container_start",
  CONTAINER_STOP = "container_stop",
  CONTAINER_RESTART = "container_restart",
  CONTAINER_CRASH = "container_crash",

  // Ray cluster events
  RAY_HEAD_START = "ray_head_start",
  RAY_HEAD_STOP = "ray_head_stop",
  RAY_WORKER_CONNECT = "ray_worker_connect",
  RAY_WORKER_DISCONNECT = "ray_worker_disconnect",
  RAY_CLUSTER_READY = "ray_cluster_ready",

  // Health check events
  HEALTH_CHECK_PASS = "health_check_pass",
  HEALTH_CHECK_FAIL = "health_check_fail",
  HEALTH_CHECK_WARNING = "health_check_warning",

  // Network events
  NCCL_ERROR = "nccl_error",
  NETWORK_VALIDATION_START = "network_validation_start",
  NETWORK_VALIDATION_SUCCESS = "network_validation_success",
  NETWORK_VALIDATION_FAILURE = "network_validation_failure",

  // Mod events
  MOD_VALIDATION_START = "mod_validation_start",
  MOD_VALIDATION_SUCCESS = "mod_validation_success",
  MOD_VALIDATION_FAILURE = "mod_validation_failure",
  MOD_APPLY_START = "mod_apply_start",
  MOD_APPLY_SUCCESS = "mod_apply_success",
  MOD_APPLY_FAILURE = "mod_apply_failure",
  MOD_ROLLBACK_START = "mod_rollback_start",
  MOD_ROLLBACK_SUCCESS = "mod_rollback_success",

  // Cluster events
  CLUSTER_START = "cluster_start",
  CLUSTER_STOP = "cluster_stop",
  CLUSTER_READY = "cluster_ready",
  CLUSTER_RECONCILED = "cluster_reconciled",

  // Script events
  SCRIPT_DISTRIBUTION_START = "script_distribution_start",
  SCRIPT_DISTRIBUTION_SUCCESS = "script_distribution_success",
  SCRIPT_DISTRIBUTION_FAILURE = "script_distribution_failure",
}

export interface DeploymentEvent {
  event_id: string;
  timestamp: string;
  event_type: EventType;
  message: string;
  resource: string;
  resource_type: OperationResourceType;
  node?: string;                // head, worker-0, worker-1, etc.
  actor?: string;
  correlation_id?: string;
  severity?: "info" | "warning" | "error";
}

// ── Health ───────────────────────────────────────────────────────────────────
//
// The four words a deployment's status badge can say. This is derived from the
// deployment's own status, not from a health check: nothing runs one.
//
// `DeploymentHealth` and `ClusterHealth` used to live here, declaring
// `status`, `gpu_errors`, `restart_count`, `last_check`, `warnings` and
// `errors`. The backend never produced a single one of those field names —
// they were where the interface's promise of "restarts" and a "check success
// rate" came from, and `restart_count` could not have been produced, because
// the deployment record keeps one `started_at` and overwrites it on every
// transition. They are deleted rather than left as an aspiration.

export enum HealthStatus {
  HEALTHY = "healthy",
  DEGRADED = "degraded",
  UNHEALTHY = "unhealthy",
  UNKNOWN = "unknown",
}
