import { create } from "zustand";
import {
  OperationState,
  type OperationStatus,
  type SSEConnectionStatus,
  SSEConnectionState,
  type DeploymentEvent,
  type LockInfo,
  type AuditEntry,
  type DryRunResult,
  type SSHError,
  type DeploymentHealth,
  type ClusterHealth,
  type OperationResourceType,
} from "@/lib/operations";
import { canTransition } from "@/lib/operations";

// ── Operation Store (AF-1) ───────────────────────────────────────────────────

interface OperationStore {
  operations: Map<string, OperationStatus>;
  addOperation: (op: OperationStatus) => void;
  updateState: (operationId: string, newState: OperationState) => boolean;
  updateProgress: (operationId: string, progress: number, step?: string) => void;
  completeOperation: (operationId: string, success: boolean, error?: string) => void;
  cancelOperation: (operationId: string) => boolean;
  getOperation: (operationId: string) => OperationStatus | undefined;
  getOperationsByResource: (resource: string, resourceType?: OperationResourceType) => OperationStatus[];
  clearOperation: (operationId: string) => void;
  clearAllIdle: () => void;
}

export const useOperationStore = create<OperationStore>((set, get) => ({
  operations: new Map(),

  addOperation: (op) =>
    set((state) => {
      const next = new Map(state.operations);
      next.set(op.operation_id, op);
      return { operations: next };
    }),

  updateState: (operationId, newState) => {
    get().operations.set(operationId, {
      ...get().operations.get(operationId)!,
      state: newState,
      completed_at: newState === OperationState.SUCCESS || newState === OperationState.FAILED || newState === OperationState.CANCELLED || newState === OperationState.ROLLED_BACK ? new Date().toISOString() : undefined,
    });
    return true;
  },

  updateProgress: (operationId, progress, step) =>
    set((state) => {
      const op = state.operations.get(operationId);
      if (!op) return state;
      const next = new Map(state.operations);
      next.set(operationId, {
        ...op,
        progress,
        current_step: step ?? op.current_step,
      });
      return { operations: next };
    }),

  completeOperation: (operationId, success, error) =>
    set((state) => {
      const op = state.operations.get(operationId);
      if (!op) return state;
      const newState = success ? OperationState.SUCCESS : OperationState.FAILED;
      const next = new Map(state.operations);
      next.set(operationId, {
        ...op,
        state: newState,
        error,
        completed_at: new Date().toISOString(),
      });
      return { operations: next };
    }),

  cancelOperation: (operationId) => {
    const op = get().operations.get(operationId);
    if (!op || !canTransition(op.state, OperationState.CANCELLED)) return false;
    get().operations.set(operationId, {
      ...op,
      state: OperationState.CANCELLED,
      completed_at: new Date().toISOString(),
    });
    return true;
  },

  getOperation: (operationId) => get().operations.get(operationId),

  getOperationsByResource: (resource, resourceType) => {
    const ops = Array.from(get().operations.values());
    return ops.filter((op) => {
      if (op.resource !== resource) return false;
      if (resourceType && op.resource_type !== resourceType) return false;
      return true;
    });
  },

  clearOperation: (operationId) =>
    set((state) => {
      const next = new Map(state.operations);
      next.delete(operationId);
      return { operations: next };
    }),

  clearAllIdle: () =>
    set((state) => {
      const next = new Map(state.operations);
      for (const [id, op] of next) {
        if (op.state === OperationState.SUCCESS || op.state === OperationState.ROLLED_BACK || op.state === OperationState.CANCELLED) {
          next.delete(id);
        }
      }
      return { operations: next };
    }),
}));

// ── SSE Connection Store (AF-3) ──────────────────────────────────────────────

interface SSEStore {
  connections: Map<string, SSEConnectionStatus>;
  updateConnection: (url: string, status: Partial<SSEConnectionStatus>) => void;
  removeConnection: (url: string) => void;
  getConnection: (url: string) => SSEConnectionStatus | undefined;
}

export const useSSEStore = create<SSEStore>((set, get) => ({
  connections: new Map(),

  updateConnection: (url, status) =>
    set((state) => {
      const current = state.connections.get(url) ?? {
        state: SSEConnectionState.DISCONNECTED,
        reconnect_attempts: 0,
      };
      const next = new Map(state.connections);
      next.set(url, { ...current, ...status });
      return { connections: next };
    }),

  removeConnection: (url) =>
    set((state) => {
      const next = new Map(state.connections);
      next.delete(url);
      return { connections: next };
    }),

  getConnection: (url) => get().connections.get(url),
}));

// ── Event Stream Store ───────────────────────────────────────────────────────

interface EventStore {
  events: Map<string, DeploymentEvent[]>;  // keyed by resource
  addEvent: (resource: string, event: DeploymentEvent) => void;
  getEvents: (resource: string, limit?: number) => DeploymentEvent[];
  clearEvents: (resource: string) => void;
  filterEvents: (resource: string, severity?: string, node?: string) => DeploymentEvent[];
}

export const useEventStore = create<EventStore>((set, get) => ({
  events: new Map(),

  addEvent: (resource, event) =>
    set((state) => {
      const existing = state.events.get(resource) ?? [];
      const next = new Map(state.events);
      next.set(resource, [...existing, event].slice(-1000)); // retention: max 1000 events
      return { events: next };
    }),

  getEvents: (resource, limit = 100) => {
    const events = get().events.get(resource) ?? [];
    return events.slice(-limit);
  },

  clearEvents: (resource) =>
    set((state) => {
      const next = new Map(state.events);
      next.delete(resource);
      return { events: next };
    }),

  filterEvents: (resource, severity, node) => {
    const events = get().events.get(resource) ?? [];
    return events.filter((e) => {
      if (severity && e.severity !== severity) return false;
      if (node && e.node !== node) return false;
      return true;
    });
  },
}));

// ── Lock Store (Phase 6.2) ───────────────────────────────────────────────────

interface LockStore {
  locks: Map<string, LockInfo>;  // keyed by "resource:lock_type"
  acquireLock: (lock: LockInfo) => boolean;
  releaseLock: (resource: string, lockType: string) => void;
  getLock: (resource: string, lockType: string) => LockInfo | undefined;
  hasLock: (resource: string, lockType: string) => boolean;
}

export const useLockStore = create<LockStore>((set, get) => ({
  locks: new Map(),

  acquireLock: (lock) => {
    const key = `${lock.resource}:${lock.lock_type}`;
    if (get().locks.has(key)) return false; // Already locked
    get().locks.set(key, lock);
    return true;
  },

  releaseLock: (resource, lockType) =>
    set((state) => {
      const key = `${resource}:${lockType}`;
      const next = new Map(state.locks);
      next.delete(key);
      return { locks: next };
    }),

  getLock: (resource, lockType) => {
    const key = `${resource}:${lockType}`;
    return get().locks.get(key);
  },

  hasLock: (resource, lockType) => {
    const key = `${resource}:${lockType}`;
    return get().locks.has(key);
  },
}));

// ── Audit Trail Store (AF-7) ─────────────────────────────────────────────────

interface AuditStore {
  entries: AuditEntry[];
  addEntry: (entry: AuditEntry) => void;
  getEntries: (limit?: number) => AuditEntry[];
  filterByActor: (actor: string) => AuditEntry[];
  filterByAction: (action: string) => AuditEntry[];
  clear: () => void;
}

export const useAuditStore = create<AuditStore>((set, get) => ({
  entries: [],

  addEntry: (entry) =>
    set((state) => ({ entries: [...state.entries, entry] })),

  getEntries: (limit = 100) => {
    const entries = get().entries;
    return entries.slice(-limit);
  },

  filterByActor: (actor) => get().entries.filter((e) => e.actor === actor),

  filterByAction: (action) => get().entries.filter((e) => e.action === action),

  clear: () => set({ entries: [] }),
}));

// ── Dry Run Store ────────────────────────────────────────────────────────────

interface DryRunStore {
  lastResult: DryRunResult | null;
  setLastResult: (result: DryRunResult | null) => void;
  getLastResult: () => DryRunResult | null;
}

export const useDryRunStore = create<DryRunStore>((set, get) => ({
  lastResult: null,
  setLastResult: (result) => set({ lastResult: result }),
  getLastResult: () => get().lastResult,
}));

// ── SSH Error Store ──────────────────────────────────────────────────────────

interface SSHErrorStore {
  errors: SSHError[];
  addError: (error: SSHError) => void;
  getErrors: () => SSHError[];
  clearErrors: () => void;
}

export const useSSHErrorStore = create<SSHErrorStore>((set, get) => ({
  errors: [],

  addError: (error) =>
    set((state) => ({ errors: [...state.errors, error] })),

  getErrors: () => get().errors,

  clearErrors: () => set({ errors: [] }),
}));

// ── Health Tracking Store (Phase 5.1) ────────────────────────────────────────

interface HealthStore {
  deploymentHealth: Map<string, DeploymentHealth>;
  clusterHealth: Map<string, ClusterHealth>;
  updateDeploymentHealth: (health: DeploymentHealth) => void;
  updateClusterHealth: (health: ClusterHealth) => void;
  getDeploymentHealth: (id: string) => DeploymentHealth | undefined;
  getClusterHealth: (name: string) => ClusterHealth | undefined;
  getTrackedResources: () => { deployments: string[]; clusters: string[] };
}

export const useHealthStore = create<HealthStore>((set, get) => ({
  deploymentHealth: new Map(),
  clusterHealth: new Map(),

  updateDeploymentHealth: (health) =>
    set((state) => {
      const next = new Map(state.deploymentHealth);
      next.set(health.deployment_id, health);
      return { deploymentHealth: next };
    }),

  updateClusterHealth: (health) =>
    set((state) => {
      const next = new Map(state.clusterHealth);
      next.set(health.cluster_name, health);
      return { clusterHealth: next };
    }),

  getDeploymentHealth: (id) => get().deploymentHealth.get(id),

  getClusterHealth: (name) => get().clusterHealth.get(name),

  getTrackedResources: () => ({
    deployments: Array.from(get().deploymentHealth.keys()),
    clusters: Array.from(get().clusterHealth.keys()),
  }),
}));
