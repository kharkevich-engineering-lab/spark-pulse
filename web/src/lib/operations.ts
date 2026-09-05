// ── Operation State Machine (AF-1) ───────────────────────────────────────────
// Canonical lifecycle for all long-running workflows: deployments of any node
// count, mod applications, and reconciliation.

export enum OperationState {
  IDLE = "idle",
  PENDING = "pending",
  RUNNING = "running",
  SUCCESS = "success",
  FAILED = "failed",
  ROLLING_BACK = "rolling_back",
  ROLLED_BACK = "rolled_back",
  CANCELLED = "cancelled",
}

export type OperationResourceType = "cluster" | "deployment" | "mod" | "reconciliation";

export interface OperationStatus {
  operation_id: string;
  resource: string;               // cluster name, deployment id, mod name
  resource_type: OperationResourceType;
  state: OperationState;
  started_at: string;
  completed_at?: string;
  progress?: number;              // 0-100
  current_step?: string;          // "ensure_ray_workers()", "validate_cluster()"
  error?: string;
  actor?: string;                 // Audit trail: who triggered the action
  correlation_id?: string;        // Log correlation across deployment/docker/ray logs
}

// ── State Transitions ────────────────────────────────────────────────────────
// IDLE → PENDING → RUNNING → SUCCESS
//                       ↘ FAILED → ROLLING_BACK → ROLLED_BACK
//                       ↘ FAILED → (retry) → RUNNING
//                       ↘ CANCELLED (from RUNNING or PENDING)

export const VALID_TRANSITIONS: Record<OperationState, OperationState[]> = {
  [OperationState.IDLE]:       [OperationState.PENDING],
  [OperationState.PENDING]:    [OperationState.RUNNING, OperationState.CANCELLED],
  [OperationState.RUNNING]:    [OperationState.SUCCESS, OperationState.FAILED, OperationState.CANCELLED],
  [OperationState.SUCCESS]:    [],
  [OperationState.FAILED]:     [OperationState.ROLLING_BACK, OperationState.PENDING], // retry
  [OperationState.ROLLING_BACK]: [OperationState.ROLLED_BACK],
  [OperationState.ROLLED_BACK]: [],
  [OperationState.CANCELLED]:  [],
};

export function canTransition(from: OperationState, to: OperationState): boolean {
  return VALID_TRANSITIONS[from]?.includes(to) ?? false;
}

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

// ── Audit Trail (AF-7) ───────────────────────────────────────────────────────

export interface AuditEntry {
  entry_id: string;
  timestamp: string;
  actor: string;
  action: string;               // "cluster_start", "mod_apply", "rollback"
  resource_type: string;
  resource: string;
  outcome: "success" | "failure" | "cancelled";
  details?: Record<string, unknown>;
  correlation_id?: string;
}

// ── Dry Run (AF-9) ───────────────────────────────────────────────────────────

export interface DryRunResult {
  script_analysis: {
    path: string;
    command_line: string | null;
    parallelism: { tp: number; pp: number; dp: number };
    backend: string | null;
    has_model_flag: boolean;
    is_valid: boolean;
    validation: { healthy: boolean; warnings: string[]; errors: string[] } | null;
  };
  parallelism: { tp: number; pp: number; dp: number };
  capacity_check: { valid: boolean; message: string };
  mod_validation: { healthy: boolean; warnings: string[]; errors: string[] }[];
  network_validation: ValidationResult;
  estimated_duration_seconds: number;
  warnings: string[];
  errors: string[];
}

export interface ValidationResult {
  valid: boolean;
  message: string;
  details?: Record<string, unknown>;
}

// ── Lock Manager (Phase 6.2) ─────────────────────────────────────────────────

export enum LockType {
  CLUSTER_START = "cluster_start",
  CLUSTER_STOP = "cluster_stop",
  MOD_APPLY = "mod_apply",
  DEPLOYMENT_START = "deployment_start",
  DEPLOYMENT_STOP = "deployment_stop",
  RECONCILIATION = "reconciliation",
}

export interface LockInfo {
  lock_id: string;
  lock_type: LockType;
  resource: string;
  holder?: string;              // User who holds the lock
  acquired_at: string;
  expires_at?: string;
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

// ── SSH Error Classification (Phase 6.4) ─────────────────────────────────────

export enum SSHErrorType {
  CONNECTION_REFUSED = "connection_refused",
  AUTHENTICATION_FAILED = "authentication_failed",
  TIMEOUT = "timeout",
  COMMAND_FAILED = "command_failed",
  HOST_UNKNOWN = "host_unknown",
  PERMISSION_DENIED = "permission_denied",
}

export interface SSHError {
  error_type: SSHErrorType;
  node: string;
  message: string;
  suggestion: string;
  original_error?: string;
}

// ── Mod Security (Phase 6.5) ─────────────────────────────────────────────────

export enum NetworkAccessPolicy {
  ALLOW_ALL = "allow_all",
  DENY_ALL = "deny_all",
  ALLOW_LIST = "allow_list",
  DENY_LIST = "deny_list",
}

export interface ModSecurityConfig {
  network_policy: NetworkAccessPolicy;
  allowed_domains?: string[];
  denied_domains?: string[];
  max_mod_size_mb: number;      // Default: 50
}
