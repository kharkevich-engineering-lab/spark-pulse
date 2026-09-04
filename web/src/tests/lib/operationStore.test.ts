import { describe, it, expect, beforeEach, vi } from "vitest";
import { act } from "@testing-library/react";
import {
  useAuditStore,
  useDryRunStore,
  useEventStore,
  useHealthStore,
  useLockStore,
  useOperationStore,
  useSSEStore,
  useSSHErrorStore,
} from "@/lib/operationStore";
import {
  EventType,
  HealthStatus,
  LockType,
  OperationState,
  SSEConnectionState,
  SSHErrorType,
  type AuditEntry,
  type DeploymentEvent,
  type DryRunResult,
  type LockInfo,
  type OperationStatus,
} from "@/lib/operations";

describe("useOperationStore", () => {
  beforeEach(() => {
    const store = useOperationStore.getState();
    store.operations.clear();
  });

  it("starts with empty operations", () => {
    const { operations } = useOperationStore.getState();
    expect(operations.size).toBe(0);
  });

  it("adds an operation", () => {
    act(() => {
      useOperationStore.getState().addOperation({
        operation_id: "op-1",
        resource: "cluster-1",
        resource_type: "cluster",
        state: OperationState.PENDING,
        started_at: new Date().toISOString(),
      });
    });

    const op = useOperationStore.getState().getOperation("op-1");
    expect(op).toBeDefined();
    expect(op!.resource).toBe("cluster-1");
  });

  it("updates operation state", () => {
    act(() => {
      useOperationStore.getState().addOperation({
        operation_id: "op-1",
        resource: "cluster-1",
        resource_type: "cluster",
        state: OperationState.PENDING,
        started_at: new Date().toISOString(),
      });
      useOperationStore.getState().updateState("op-1", OperationState.RUNNING);
    });

    const op = useOperationStore.getState().getOperation("op-1");
    expect(op!.state).toBe(OperationState.RUNNING);
  });

  it("updates operation progress", () => {
    act(() => {
      useOperationStore.getState().addOperation({
        operation_id: "op-1",
        resource: "cluster-1",
        resource_type: "cluster",
        state: OperationState.RUNNING,
        started_at: new Date().toISOString(),
      });
      useOperationStore.getState().updateProgress("op-1", 75, "Deploying...");
    });

    const op = useOperationStore.getState().getOperation("op-1");
    expect(op!.progress).toBe(75);
    expect(op!.current_step).toBe("Deploying...");
  });

  it("completes operation successfully", () => {
    act(() => {
      useOperationStore.getState().addOperation({
        operation_id: "op-1",
        resource: "cluster-1",
        resource_type: "cluster",
        state: OperationState.RUNNING,
        started_at: new Date().toISOString(),
      });
      useOperationStore.getState().completeOperation("op-1", true);
    });

    const op = useOperationStore.getState().getOperation("op-1");
    expect(op!.state).toBe(OperationState.SUCCESS);
    expect(op!.completed_at).toBeDefined();
  });

  it("completes operation with failure", () => {
    act(() => {
      useOperationStore.getState().addOperation({
        operation_id: "op-1",
        resource: "cluster-1",
        resource_type: "cluster",
        state: OperationState.RUNNING,
        started_at: new Date().toISOString(),
      });
      useOperationStore.getState().completeOperation("op-1", false, "Error occurred");
    });

    const op = useOperationStore.getState().getOperation("op-1");
    expect(op!.state).toBe(OperationState.FAILED);
    expect(op!.error).toBe("Error occurred");
  });

  it("cancels operation successfully", () => {
    act(() => {
      useOperationStore.getState().addOperation({
        operation_id: "op-1",
        resource: "cluster-1",
        resource_type: "cluster",
        state: OperationState.RUNNING,
        started_at: new Date().toISOString(),
      });
      useOperationStore.getState().cancelOperation("op-1");
    });

    const op = useOperationStore.getState().getOperation("op-1");
    expect(op!.state).toBe(OperationState.CANCELLED);
  });

  it("cannot cancel from SUCCESS state", () => {
    let success = false;
    act(() => {
      useOperationStore.getState().addOperation({
        operation_id: "op-1",
        resource: "cluster-1",
        resource_type: "cluster",
        state: OperationState.SUCCESS,
        started_at: new Date().toISOString(),
      });
      success = useOperationStore.getState().cancelOperation("op-1");
    });

    expect(success).toBe(false);
    const op = useOperationStore.getState().getOperation("op-1");
    expect(op!.state).toBe(OperationState.SUCCESS);
  });

  it("clears an operation", () => {
    act(() => {
      useOperationStore.getState().addOperation({
        operation_id: "op-1",
        resource: "cluster-1",
        resource_type: "cluster",
        state: OperationState.RUNNING,
        started_at: new Date().toISOString(),
      });
      useOperationStore.getState().clearOperation("op-1");
    });

    expect(useOperationStore.getState().getOperation("op-1")).toBeUndefined();
  });

  it("clears all idle operations", () => {
    act(() => {
      useOperationStore.getState().addOperation({
        operation_id: "op-1",
        resource: "cluster-1",
        resource_type: "cluster",
        state: OperationState.SUCCESS,
        started_at: new Date().toISOString(),
      });
      useOperationStore.getState().addOperation({
        operation_id: "op-2",
        resource: "cluster-2",
        resource_type: "deployment",
        state: OperationState.RUNNING,
        started_at: new Date().toISOString(),
      });
      useOperationStore.getState().addOperation({
        operation_id: "op-3",
        resource: "cluster-3",
        resource_type: "cluster",
        state: OperationState.CANCELLED,
        started_at: new Date().toISOString(),
      });
      useOperationStore.getState().clearAllIdle();
    });

    expect(useOperationStore.getState().getOperation("op-1")).toBeUndefined();
    expect(useOperationStore.getState().getOperation("op-2")).toBeDefined();
    expect(useOperationStore.getState().getOperation("op-3")).toBeUndefined();
  });

  it("gets operations by resource", () => {
    act(() => {
      useOperationStore.getState().addOperation({
        operation_id: "op-1",
        resource: "cluster-1",
        resource_type: "cluster",
        state: OperationState.RUNNING,
        started_at: new Date().toISOString(),
      });
      useOperationStore.getState().addOperation({
        operation_id: "op-2",
        resource: "cluster-1",
        resource_type: "deployment",
        state: OperationState.RUNNING,
        started_at: new Date().toISOString(),
      });
      useOperationStore.getState().addOperation({
        operation_id: "op-3",
        resource: "cluster-2",
        resource_type: "cluster",
        state: OperationState.RUNNING,
        started_at: new Date().toISOString(),
      });
    });

    const ops = useOperationStore.getState().getOperationsByResource("cluster-1");
    expect(ops).toHaveLength(2);
  });
});

describe("useSSEStore", () => {
  it("starts with empty connections", () => {
    const { connections } = useSSEStore.getState();
    expect(connections.size).toBe(0);
  });

  it("updates connection status", () => {
    act(() => {
      useSSEStore.getState().updateConnection("/sse/health", {
        state: SSEConnectionState.CONNECTED,
        reconnect_attempts: 0,
      });
    });

    const conn = useSSEStore.getState().getConnection("/sse/health");
    expect(conn).toBeDefined();
    expect(conn!.state).toBe(SSEConnectionState.CONNECTED);
  });

  it("removes connection", () => {
    act(() => {
      useSSEStore.getState().updateConnection("/sse/health", {
        state: SSEConnectionState.CONNECTED,
      });
      useSSEStore.getState().removeConnection("/sse/health");
    });

    expect(useSSEStore.getState().getConnection("/sse/health")).toBeUndefined();
  });

  it("merges partial updates", () => {
    act(() => {
      useSSEStore.getState().updateConnection("/sse/health", {
        state: SSEConnectionState.CONNECTED,
        reconnect_attempts: 2,
      });
      useSSEStore.getState().updateConnection("/sse/health", {
        last_connected_at: new Date().toISOString(),
      });
    });

    const conn = useSSEStore.getState().getConnection("/sse/health");
    expect(conn!.state).toBe(SSEConnectionState.CONNECTED);
    expect(conn!.reconnect_attempts).toBe(2);
    expect(conn!.last_connected_at).toBeDefined();
  });

  // A stream that has never connected still has to have a status to render,
  // or the indicator is blank rather than saying "disconnected".
  it("starts an unknown stream disconnected with no attempts made", () => {
    act(() => {
      useSSEStore.getState().updateConnection("/sse/deployments", { error: "refused" });
    });

    const conn = useSSEStore.getState().getConnection("/sse/deployments")!;
    expect(conn.state).toBe(SSEConnectionState.DISCONNECTED);
    expect(conn.reconnect_attempts).toBe(0);
    expect(conn.error).toBe("refused");
  });
});

// ── The rest of the stores ───────────────────────────────────────────────────
//
// Everything below this line was untested. These stores are what the SSE
// handlers write into and what the event panes, the lock indicator and the
// audit trail read out of, so the properties worth pinning are the ones with
// a rule behind them: retention caps, filters, and a lock a second claimant
// cannot take.

describe("useOperationStore, the paths a failed call takes", () => {
  beforeEach(() => {
    useOperationStore.getState().operations.clear();
  });

  const running = (id: string, resource = "cluster-1"): OperationStatus => ({
    operation_id: id,
    resource,
    resource_type: "deployment",
    state: OperationState.RUNNING,
    started_at: new Date().toISOString(),
  });

  it("ignores progress for an operation it has never heard of", () => {
    act(() => {
      useOperationStore.getState().updateProgress("ghost", 50, "step");
    });
    expect(useOperationStore.getState().operations.size).toBe(0);
  });

  it("ignores a completion for an operation it has never heard of", () => {
    act(() => {
      useOperationStore.getState().completeOperation("ghost", true);
    });
    expect(useOperationStore.getState().operations.size).toBe(0);
  });

  it("refuses to cancel an operation it has never heard of", () => {
    let cancelled = true;
    act(() => {
      cancelled = useOperationStore.getState().cancelOperation("ghost");
    });
    expect(cancelled).toBe(false);
  });

  // A progress event without a step name is the common case: the percentage
  // moved but the phase did not, and the pane must keep showing the phase.
  it("keeps the current step when a progress update does not name one", () => {
    act(() => {
      useOperationStore.getState().addOperation(running("op-1"));
      useOperationStore.getState().updateProgress("op-1", 10, "pulling image");
      useOperationStore.getState().updateProgress("op-1", 40);
    });

    const op = useOperationStore.getState().getOperation("op-1")!;
    expect(op.progress).toBe(40);
    expect(op.current_step).toBe("pulling image");
  });

  it("narrows operations by resource type when asked", () => {
    act(() => {
      const store = useOperationStore.getState();
      store.addOperation({ ...running("op-1"), resource_type: "deployment" });
      store.addOperation({ ...running("op-2"), resource_type: "mod" });
    });

    const store = useOperationStore.getState();
    expect(store.getOperationsByResource("cluster-1", "mod").map((o) => o.operation_id)).toEqual([
      "op-2",
    ]);
    expect(store.getOperationsByResource("cluster-1")).toHaveLength(2);
    expect(store.getOperationsByResource("nothing-here")).toEqual([]);
  });

  // "Idle" means finished. Anything still moving — running, pending, rolling
  // back, or failed and awaiting a decision — has to survive the sweep.
  it("clears only finished operations, leaving the ones still in flight", () => {
    act(() => {
      const store = useOperationStore.getState();
      store.addOperation({ ...running("done"), state: OperationState.SUCCESS });
      store.addOperation({ ...running("rolled"), state: OperationState.ROLLED_BACK });
      store.addOperation({ ...running("cancelled"), state: OperationState.CANCELLED });
      store.addOperation({ ...running("failed"), state: OperationState.FAILED });
      store.addOperation({ ...running("rolling"), state: OperationState.ROLLING_BACK });
      store.addOperation({ ...running("pending"), state: OperationState.PENDING });
      store.clearAllIdle();
    });

    expect([...useOperationStore.getState().operations.keys()].sort()).toEqual([
      "failed",
      "pending",
      "rolling",
    ]);
  });

  // The panes subscribe to the store; a write nothing is notified about is a
  // pane that silently stops updating.
  it("notifies subscribers when an operation is added, advanced or cleared", () => {
    const seen = vi.fn();
    const unsubscribe = useOperationStore.subscribe(seen);
    try {
      act(() => {
        useOperationStore.getState().addOperation(running("op-1"));
        useOperationStore.getState().updateProgress("op-1", 50);
        useOperationStore.getState().completeOperation("op-1", true);
        useOperationStore.getState().clearOperation("op-1");
      });
    } finally {
      unsubscribe();
    }
    expect(seen).toHaveBeenCalledTimes(4);
  });

  // `updateState` and `cancelOperation` used to write through the existing Map
  // and return `true` regardless. Both halves mattered: zustand compares the
  // reference, so a pane subscribed to the store never re-rendered, and a
  // caller was told a transition succeeded that the state machine forbids.
  it("notifies subscribers when an operation changes state or is cancelled", () => {
    act(() => {
      useOperationStore.getState().addOperation(running("op-1", "cluster-1"));
    });

    const seen = vi.fn();
    const unsubscribe = useOperationStore.subscribe(seen);
    try {
      act(() => {
        useOperationStore.getState().updateState("op-1", OperationState.CANCELLED);
        useOperationStore.getState().addOperation(running("op-2"));
        useOperationStore.getState().cancelOperation("op-2");
      });
    } finally {
      unsubscribe();
    }

    expect(seen).toHaveBeenCalledTimes(3);
  });

  it("hands subscribers a new Map rather than the one they already hold", () => {
    let before: Map<string, OperationStatus> | undefined;
    act(() => {
      useOperationStore.getState().addOperation({
        ...running("op-1"),
        state: OperationState.PENDING,
      });
    });
    before = useOperationStore.getState().operations;

    act(() => {
      useOperationStore.getState().updateState("op-1", OperationState.RUNNING);
    });

    expect(useOperationStore.getState().operations).not.toBe(before);
    expect(before!.get("op-1")!.state).toBe(OperationState.PENDING);
  });

  // VALID_TRANSITIONS says SUCCESS is terminal. A finished deployment that
  // walks back into RUNNING makes the jobs list show a spinner forever.
  it("refuses a transition the state machine forbids, loudly and without effect", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    let allowed = true;
    let warnings: unknown[][] = [];
    try {
      act(() => {
        useOperationStore.getState().addOperation({
          ...running("op-1"),
          state: OperationState.SUCCESS,
        });
        allowed = useOperationStore.getState().updateState("op-1", OperationState.RUNNING);
      });
      warnings = warn.mock.calls.map((c) => [...c]);
    } finally {
      warn.mockRestore();
    }

    expect(allowed).toBe(false);
    expect(useOperationStore.getState().getOperation("op-1")!.state).toBe(
      OperationState.SUCCESS,
    );
    expect(warnings.flat().join(" ")).toContain("refused success -> running");
  });

  it("refuses a state change for an operation it has never heard of", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    let allowed = true;
    try {
      act(() => {
        allowed = useOperationStore.getState().updateState("ghost", OperationState.RUNNING);
      });
    } finally {
      warn.mockRestore();
    }

    expect(allowed).toBe(false);
    expect(useOperationStore.getState().operations.size).toBe(0);
  });

  it("allows the transitions the state machine does permit", () => {
    let toRunning = false;
    let toFailed = false;
    let retried = false;
    act(() => {
      const store = useOperationStore.getState();
      store.addOperation({ ...running("op-1"), state: OperationState.PENDING });
      toRunning = store.updateState("op-1", OperationState.RUNNING);
      toFailed = store.updateState("op-1", OperationState.FAILED);
      retried = store.updateState("op-1", OperationState.PENDING);
    });

    expect([toRunning, toFailed, retried]).toEqual([true, true, true]);
    expect(useOperationStore.getState().getOperation("op-1")!.state).toBe(
      OperationState.PENDING,
    );
  });

  // Only a terminal state means "finished", so only a terminal state stamps a
  // completion time — and a non-terminal move must not wipe one that is there.
  it("stamps a completion time only on the states an operation cannot leave", () => {
    act(() => {
      const store = useOperationStore.getState();
      store.addOperation({ ...running("op-1"), state: OperationState.PENDING });
      store.updateState("op-1", OperationState.RUNNING);
    });
    expect(useOperationStore.getState().getOperation("op-1")!.completed_at).toBeUndefined();

    act(() => {
      useOperationStore.getState().updateState("op-1", OperationState.FAILED);
    });
    const failedAt = useOperationStore.getState().getOperation("op-1")!.completed_at;
    expect(Date.parse(failedAt!)).not.toBeNaN();

    // FAILED -> PENDING is a retry; it must not erase when the failure landed.
    act(() => {
      useOperationStore.getState().updateState("op-1", OperationState.PENDING);
    });
    expect(useOperationStore.getState().getOperation("op-1")!.completed_at).toBe(failedAt);
  });

  it("stamps a completion time on a finished operation and an error on a failed one", () => {
    act(() => {
      useOperationStore.getState().addOperation(running("op-1"));
      useOperationStore.getState().completeOperation("op-1", false, "container exited 1");
    });

    const op = useOperationStore.getState().getOperation("op-1")!;
    expect(op.state).toBe(OperationState.FAILED);
    expect(op.error).toBe("container exited 1");
    expect(Date.parse(op.completed_at!)).not.toBeNaN();
  });
});

describe("useEventStore", () => {
  beforeEach(() => {
    act(() => {
      useEventStore.setState({ events: new Map() });
    });
  });

  const event = (over: Partial<DeploymentEvent> = {}): DeploymentEvent => ({
    event_id: "e1",
    timestamp: "2026-09-04T12:00:00Z",
    event_type: EventType.DEPLOYMENT_START,
    message: "starting",
    resource: "abc123",
    resource_type: "deployment",
    ...over,
  });

  it("keeps each resource's events apart", () => {
    act(() => {
      useEventStore.getState().addEvent("abc123", event());
      useEventStore.getState().addEvent("def456", event({ event_id: "e2", resource: "def456" }));
    });

    expect(useEventStore.getState().getEvents("abc123")).toHaveLength(1);
    expect(useEventStore.getState().getEvents("def456")).toHaveLength(1);
    expect(useEventStore.getState().getEvents("never-deployed")).toEqual([]);
  });

  // A chatty deployment can emit events all day; the pane holds the newest
  // and the store must not grow without bound behind it.
  it("retains at most the newest 1000 events for a resource", () => {
    act(() => {
      for (let i = 0; i < 1010; i++) {
        useEventStore.getState().addEvent("abc123", event({ event_id: `e${i}` }));
      }
    });

    const events = useEventStore.getState().getEvents("abc123", 2000);
    expect(events).toHaveLength(1000);
    expect(events[0].event_id).toBe("e10");
    expect(events[events.length - 1].event_id).toBe("e1009");
  });

  it("hands back the newest events up to the limit asked for", () => {
    act(() => {
      for (let i = 0; i < 5; i++) {
        useEventStore.getState().addEvent("abc123", event({ event_id: `e${i}` }));
      }
    });

    expect(useEventStore.getState().getEvents("abc123", 2).map((e) => e.event_id)).toEqual([
      "e3",
      "e4",
    ]);
  });

  // Which rank an event came from is the question a multi-node failure gets
  // debugged with, so filtering by node has to work alongside severity.
  it("filters by severity and by node, together and apart", () => {
    act(() => {
      const store = useEventStore.getState();
      store.addEvent("abc123", event({ event_id: "a", severity: "error", node: "rank-0" }));
      store.addEvent("abc123", event({ event_id: "b", severity: "info", node: "rank-0" }));
      store.addEvent("abc123", event({ event_id: "c", severity: "error", node: "rank-1" }));
    });

    const store = useEventStore.getState();
    expect(store.filterEvents("abc123", "error").map((e) => e.event_id)).toEqual(["a", "c"]);
    expect(store.filterEvents("abc123", undefined, "rank-0").map((e) => e.event_id)).toEqual([
      "a",
      "b",
    ]);
    expect(store.filterEvents("abc123", "error", "rank-1").map((e) => e.event_id)).toEqual(["c"]);
    expect(store.filterEvents("abc123")).toHaveLength(3);
    expect(store.filterEvents("never-deployed", "error")).toEqual([]);
  });

  it("forgets one resource's events without touching another's", () => {
    act(() => {
      useEventStore.getState().addEvent("abc123", event());
      useEventStore.getState().addEvent("def456", event({ resource: "def456" }));
      useEventStore.getState().clearEvents("abc123");
    });

    expect(useEventStore.getState().getEvents("abc123")).toEqual([]);
    expect(useEventStore.getState().getEvents("def456")).toHaveLength(1);
  });
});

describe("useLockStore", () => {
  beforeEach(() => {
    act(() => {
      useLockStore.setState({ locks: new Map() });
    });
  });

  const lock = (over: Partial<LockInfo> = {}): LockInfo => ({
    lock_id: "l1",
    lock_type: LockType.DEPLOYMENT_START,
    resource: "abc123",
    acquired_at: new Date().toISOString(),
    ...over,
  });

  // The whole point of the lock: the second claimant is told no, rather than
  // both starting the same deployment.
  it("refuses a second claim on a lock somebody already holds", () => {
    let first = false;
    let second = true;
    act(() => {
      first = useLockStore.getState().acquireLock(lock({ holder: "ada" }));
      second = useLockStore.getState().acquireLock(lock({ lock_id: "l2", holder: "grace" }));
    });

    expect(first).toBe(true);
    expect(second).toBe(false);
    expect(useLockStore.getState().getLock("abc123", LockType.DEPLOYMENT_START)!.holder).toBe(
      "ada",
    );
  });

  // Locks are keyed by resource *and* type: stopping one deployment must not
  // be blocked by the lock held to start a different one.
  it("keys locks by resource and type, so different work does not collide", () => {
    act(() => {
      const store = useLockStore.getState();
      store.acquireLock(lock());
      store.acquireLock(lock({ lock_id: "l2", lock_type: LockType.DEPLOYMENT_STOP }));
      store.acquireLock(lock({ lock_id: "l3", resource: "def456" }));
    });

    const store = useLockStore.getState();
    expect(store.hasLock("abc123", LockType.DEPLOYMENT_START)).toBe(true);
    expect(store.hasLock("abc123", LockType.DEPLOYMENT_STOP)).toBe(true);
    expect(store.hasLock("def456", LockType.DEPLOYMENT_START)).toBe(true);
    expect(store.hasLock("def456", LockType.DEPLOYMENT_STOP)).toBe(false);
  });

  // Same defect as the operation store had: writing through the Map left every
  // subscriber holding the reference it already had, so the lock indicator
  // never lit up.
  it("notifies subscribers when a lock is taken", () => {
    const seen = vi.fn();
    const before = useLockStore.getState().locks;
    const unsubscribe = useLockStore.subscribe(seen);
    try {
      act(() => {
        useLockStore.getState().acquireLock(lock());
      });
    } finally {
      unsubscribe();
    }

    expect(seen).toHaveBeenCalledTimes(1);
    expect(useLockStore.getState().locks).not.toBe(before);
    expect(before.size).toBe(0);
  });

  it("frees a lock so the next claimant can take it", () => {
    let retaken = false;
    act(() => {
      const store = useLockStore.getState();
      store.acquireLock(lock());
      store.releaseLock("abc123", LockType.DEPLOYMENT_START);
      retaken = store.acquireLock(lock({ lock_id: "l2" }));
    });

    expect(retaken).toBe(true);
  });

  it("reports no lock for a resource nothing has claimed", () => {
    const store = useLockStore.getState();
    expect(store.hasLock("abc123", LockType.MOD_APPLY)).toBe(false);
    expect(store.getLock("abc123", LockType.MOD_APPLY)).toBeUndefined();
  });
});

describe("useAuditStore", () => {
  beforeEach(() => {
    act(() => {
      useAuditStore.getState().clear();
    });
  });

  const entry = (over: Partial<AuditEntry> = {}): AuditEntry => ({
    entry_id: "a1",
    timestamp: "2026-09-04T12:00:00Z",
    actor: "ada",
    action: "deployment_start",
    resource_type: "deployment",
    resource: "abc123",
    outcome: "success",
    ...over,
  });

  it("keeps entries in the order they happened", () => {
    act(() => {
      useAuditStore.getState().addEntry(entry({ entry_id: "a1" }));
      useAuditStore.getState().addEntry(entry({ entry_id: "a2" }));
    });

    expect(useAuditStore.getState().getEntries().map((e) => e.entry_id)).toEqual(["a1", "a2"]);
  });

  it("hands back the most recent entries up to the limit asked for", () => {
    act(() => {
      for (let i = 0; i < 5; i++) {
        useAuditStore.getState().addEntry(entry({ entry_id: `a${i}` }));
      }
    });

    expect(useAuditStore.getState().getEntries(2).map((e) => e.entry_id)).toEqual(["a3", "a4"]);
  });

  // The trail exists to answer "who stopped it" and "what happened to this
  // deployment", which are the two filters.
  it("filters by actor and by action", () => {
    act(() => {
      const store = useAuditStore.getState();
      store.addEntry(entry({ entry_id: "a1", actor: "ada" }));
      store.addEntry(entry({ entry_id: "a2", actor: "grace", action: "deployment_stop" }));
    });

    const store = useAuditStore.getState();
    expect(store.filterByActor("ada").map((e) => e.entry_id)).toEqual(["a1"]);
    expect(store.filterByAction("deployment_stop").map((e) => e.entry_id)).toEqual(["a2"]);
    expect(store.filterByActor("nobody")).toEqual([]);
  });

  it("empties on clear", () => {
    act(() => {
      useAuditStore.getState().addEntry(entry());
      useAuditStore.getState().clear();
    });
    expect(useAuditStore.getState().getEntries()).toEqual([]);
  });
});

describe("useDryRunStore", () => {
  it("holds the last dry run and lets it be discarded", () => {
    const result = { warnings: ["slow"], errors: [] } as unknown as DryRunResult;

    act(() => {
      useDryRunStore.getState().setLastResult(result);
    });
    expect(useDryRunStore.getState().getLastResult()).toBe(result);

    act(() => {
      useDryRunStore.getState().setLastResult(null);
    });
    expect(useDryRunStore.getState().getLastResult()).toBeNull();
  });
});

describe("useSSHErrorStore", () => {
  beforeEach(() => {
    act(() => {
      useSSHErrorStore.getState().clearErrors();
    });
  });

  // An SSH failure is only useful with its suggestion attached — "permission
  // denied on 10.0.0.11, copy your key with ssh-copy-id" — so the store keeps
  // the classified error rather than a string.
  it("accumulates classified errors with their suggestions", () => {
    act(() => {
      useSSHErrorStore.getState().addError({
        error_type: SSHErrorType.PERMISSION_DENIED,
        node: "10.0.0.11",
        message: "Permission denied (publickey)",
        suggestion: "Copy your key with ssh-copy-id",
      });
    });

    const errors = useSSHErrorStore.getState().getErrors();
    expect(errors).toHaveLength(1);
    expect(errors[0].node).toBe("10.0.0.11");
    expect(errors[0].suggestion).toContain("ssh-copy-id");
  });

  it("empties on clear", () => {
    act(() => {
      useSSHErrorStore.getState().addError({
        error_type: SSHErrorType.TIMEOUT,
        node: "10.0.0.11",
        message: "timed out",
        suggestion: "check the link",
      });
      useSSHErrorStore.getState().clearErrors();
    });
    expect(useSSHErrorStore.getState().getErrors()).toEqual([]);
  });
});

describe("useHealthStore", () => {
  beforeEach(() => {
    act(() => {
      useHealthStore.setState({ deploymentHealth: new Map(), clusterHealth: new Map() });
    });
  });

  it("tracks deployment and cluster health separately", () => {
    act(() => {
      useHealthStore.getState().updateDeploymentHealth({
        deployment_id: "abc123",
        status: HealthStatus.DEGRADED,
        gpu_errors: 1,
        restart_count: 2,
        last_check: "2026-09-04T12:00:00Z",
        warnings: ["one rank restarted"],
        errors: [],
      });
      useHealthStore.getState().updateClusterHealth({
        cluster_name: "spark",
        status: HealthStatus.HEALTHY,
        nodes: { "spark-01": HealthStatus.HEALTHY },
        last_check: "2026-09-04T12:00:00Z",
        warnings: [],
        errors: [],
      });
    });

    const store = useHealthStore.getState();
    expect(store.getDeploymentHealth("abc123")!.status).toBe(HealthStatus.DEGRADED);
    expect(store.getClusterHealth("spark")!.nodes["spark-01"]).toBe(HealthStatus.HEALTHY);
    expect(store.getDeploymentHealth("never-deployed")).toBeUndefined();
    expect(store.getClusterHealth("no-such-cluster")).toBeUndefined();
  });

  it("replaces a resource's health rather than accumulating reports", () => {
    const base = {
      deployment_id: "abc123",
      gpu_errors: 0,
      restart_count: 0,
      last_check: "2026-09-04T12:00:00Z",
      warnings: [],
      errors: [],
    };
    act(() => {
      useHealthStore.getState().updateDeploymentHealth({ ...base, status: HealthStatus.HEALTHY });
      useHealthStore
        .getState()
        .updateDeploymentHealth({ ...base, status: HealthStatus.UNHEALTHY });
    });

    expect(useHealthStore.getState().deploymentHealth.size).toBe(1);
    expect(useHealthStore.getState().getDeploymentHealth("abc123")!.status).toBe(
      HealthStatus.UNHEALTHY,
    );
  });

  it("lists what it is tracking", () => {
    act(() => {
      useHealthStore.getState().updateDeploymentHealth({
        deployment_id: "abc123",
        status: HealthStatus.HEALTHY,
        gpu_errors: 0,
        restart_count: 0,
        last_check: "2026-09-04T12:00:00Z",
        warnings: [],
        errors: [],
      });
    });

    expect(useHealthStore.getState().getTrackedResources()).toEqual({
      deployments: ["abc123"],
      clusters: [],
    });
  });
});
