import { describe, it, expect, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useOperation, useOperationProgress } from "@/hooks/useOperation";
import { useOperationStore } from "@/lib/operationStore";
import { OperationState } from "@/lib/operations";

describe("useOperation", () => {
  beforeEach(() => {
    // Clear the store before each test
    const store = useOperationStore.getState();
    store.operations.clear();
  });

  it("returns operation management functions", () => {
    const { result } = renderHook(() => useOperation());
    expect(result.current.startOperation).toBeDefined();
    expect(result.current.updateProgress).toBeDefined();
    expect(result.current.completeOperation).toBeDefined();
    expect(result.current.cancel).toBeDefined();
    expect(result.current.getOperation).toBeDefined();
    expect(result.current.getOperationsByResource).toBeDefined();
    expect(result.current.clearOperation).toBeDefined();
    expect(result.current.OperationState).toBe(OperationState);
  });

  it("starts an operation with PENDING then RUNNING state", () => {
    const { result } = renderHook(() => useOperation());
    const opId = "test-op-1";

    act(() => {
      result.current.startOperation(opId, "cluster-1", "cluster");
    });

    const op = result.current.getOperation(opId);
    expect(op).toBeDefined();
    expect(op!.state).toBe(OperationState.RUNNING);
    expect(op!.resource).toBe("cluster-1");
    expect(op!.resource_type).toBe("cluster");
  });

  it("updates operation progress", () => {
    const { result } = renderHook(() => useOperation());
    const opId = "test-op-2";

    act(() => {
      result.current.startOperation(opId, "cluster-1", "cluster");
      result.current.updateProgress(opId, 50, "Validating...");
    });

    const op = result.current.getOperation(opId);
    expect(op!.progress).toBe(50);
    expect(op!.current_step).toBe("Validating...");
  });

  it("completes operation successfully", () => {
    const { result } = renderHook(() => useOperation());
    const opId = "test-op-3";

    act(() => {
      result.current.startOperation(opId, "cluster-1", "cluster");
      result.current.completeOperation(opId, true);
    });

    const op = result.current.getOperation(opId);
    expect(op!.state).toBe(OperationState.SUCCESS);
    expect(op!.completed_at).toBeDefined();
  });

  it("completes operation with failure", () => {
    const { result } = renderHook(() => useOperation());
    const opId = "test-op-4";

    act(() => {
      result.current.startOperation(opId, "cluster-1", "cluster");
      result.current.completeOperation(opId, false, "Connection timeout");
    });

    const op = result.current.getOperation(opId);
    expect(op!.state).toBe(OperationState.FAILED);
    expect(op!.error).toBe("Connection timeout");
  });

  it("cancels an operation", () => {
    const { result } = renderHook(() => useOperation());
    const opId = "test-op-5";

    act(() => {
      result.current.startOperation(opId, "cluster-1", "cluster");
      result.current.cancel(opId);
    });

    const op = result.current.getOperation(opId);
    expect(op!.state).toBe(OperationState.CANCELLED);
  });

  it("gets operations by resource", () => {
    const { result } = renderHook(() => useOperation());

    act(() => {
      result.current.startOperation("op-1", "cluster-1", "cluster");
      result.current.startOperation("op-2", "cluster-1", "deployment");
      result.current.startOperation("op-3", "cluster-2", "cluster");
    });

    const ops = result.current.getOperationsByResource("cluster-1", "cluster");
    expect(ops).toHaveLength(1);
    expect(ops[0].operation_id).toBe("op-1");
  });

  it("clears an operation", () => {
    const { result } = renderHook(() => useOperation());
    const opId = "test-op-6";

    act(() => {
      result.current.startOperation(opId, "cluster-1", "cluster");
      result.current.clearOperation(opId);
    });

    expect(result.current.getOperation(opId)).toBeUndefined();
  });
});

describe("useOperationProgress", () => {
  beforeEach(() => {
    const store = useOperationStore.getState();
    store.operations.clear();
  });

  it("returns empty state when no operations", () => {
    const { result } = renderHook(() => useOperationProgress());
    expect(result.current.isRunning).toBe(false);
    expect(result.current.hasFailed).toBe(false);
    expect(result.current.activeOperations).toHaveLength(0);
  });

  it("shows active operations", () => {
    const opHook = renderHook(() => useOperation());
    const { result } = renderHook(() => useOperationProgress());

    act(() => {
      opHook.result.current.startOperation("op-1", "cluster-1", "cluster");
    });

    expect(result.current.isRunning).toBe(true);
    expect(result.current.activeOperations).toHaveLength(1);
  });

  it("shows failed operations", () => {
    const opHook = renderHook(() => useOperation());
    const { result } = renderHook(() => useOperationProgress());

    act(() => {
      opHook.result.current.startOperation("op-1", "cluster-1", "cluster");
      opHook.result.current.completeOperation("op-1", false, "Error");
    });

    expect(result.current.hasFailed).toBe(true);
    expect(result.current.failedOperations).toHaveLength(1);
  });

  it("filters by resource and type", () => {
    const opHook = renderHook(() => useOperation());
    const { result } = renderHook(
      () => useOperationProgress("cluster-1", "cluster"),
    );

    act(() => {
      opHook.result.current.startOperation("op-1", "cluster-1", "cluster");
      opHook.result.current.startOperation("op-2", "cluster-1", "deployment");
      opHook.result.current.startOperation("op-3", "cluster-2", "cluster");
    });

    expect(result.current.activeOperations).toHaveLength(1);
  });
});
