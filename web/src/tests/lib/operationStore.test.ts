import { describe, it, expect, beforeEach } from "vitest";
import { act } from "@testing-library/react";
import { useOperationStore, useSSEStore } from "@/lib/operationStore";
import {
  OperationState,
  SSEConnectionState,
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
});
