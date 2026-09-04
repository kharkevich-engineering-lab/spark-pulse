import { useCallback } from "react";
import {
  OperationState,
  type OperationResourceType,
} from "@/lib/operations";
import { useOperationStore } from "@/lib/operationStore";

/**
 * Hook for managing operations (AF-1).
 *
 * Usage:
 * ```ts
 * const { addOperation, updateProgress, completeOperation } = useOperation();
 *
 * const handleDeploy = async () => {
 *   const opId = crypto.randomUUID();
 *   addOperation({
 *     operation_id: opId,
 *     resource: "my-cluster",
 *     resource_type: "cluster",
 *     state: OperationState.PENDING,
 *     started_at: new Date().toISOString(),
 *   });
 *
 *   try {
 *     updateProgress(opId, 25, "Planning...");
 *     await planDeployment(...);
 *
 *     updateProgress(opId, 50, "Starting...");
 *     await createDeployment(...);
 *
 *     completeOperation(opId, true);
 *   } catch (error) {
 *     completeOperation(opId, false, error.message);
 *   }
 * };
 * ```
 */
export function useOperation() {
  const addOperation = useOperationStore((s) => s.addOperation);
  const updateState = useOperationStore((s) => s.updateState);
  const updateProgress = useOperationStore((s) => s.updateProgress);
  const completeOperation = useOperationStore((s) => s.completeOperation);
  const cancelOperation = useOperationStore((s) => s.cancelOperation);
  const getOperation = useOperationStore((s) => s.getOperation);
  const getOperationsByResource = useOperationStore((s) => s.getOperationsByResource);
  const clearOperation = useOperationStore((s) => s.clearOperation);

  const startOperation = useCallback(
    (
      operationId: string,
      resource: string,
      resourceType: OperationResourceType,
      actor?: string
    ) => {
      addOperation({
        operation_id: operationId,
        resource,
        resource_type: resourceType,
        state: OperationState.PENDING,
        started_at: new Date().toISOString(),
        actor,
      });
      updateState(operationId, OperationState.RUNNING);
    },
    [addOperation, updateState]
  );

  const cancel = useCallback(
    (operationId: string) => cancelOperation(operationId),
    [cancelOperation]
  );

  return {
    startOperation,
    updateProgress,
    completeOperation,
    cancel,
    getOperation,
    getOperationsByResource,
    clearOperation,
    OperationState,
  };
}

/**
 * Hook for displaying operation progress in the UI.
 *
 * Usage:
 * ```ts
 * const { activeOperations, isRunning } = useOperationProgress("dep-123", "deployment");
 *
 * if (isRunning) {
 *   return <ProgressBar operations={activeOperations} />;
 * }
 * ```
 */
export function useOperationProgress(
  resource?: string,
  resourceType?: OperationResourceType
) {
  const operations = useOperationStore((s) => s.operations);

  const activeOperations = resource
    ? Array.from(operations.values()).filter((op) => {
        if (resource && op.resource !== resource) return false;
        if (resourceType && op.resource_type !== resourceType) return false;
        return op.state === OperationState.RUNNING || op.state === OperationState.PENDING;
      })
    : Array.from(operations.values()).filter(
        (op) => op.state === OperationState.RUNNING || op.state === OperationState.PENDING
      );

  const failedOperations = resource
    ? Array.from(operations.values()).filter((op) => {
        if (resource && op.resource !== resource) return false;
        if (resourceType && op.resource_type !== resourceType) return false;
        return op.state === OperationState.FAILED;
      })
    : Array.from(operations.values()).filter(
        (op) => op.state === OperationState.FAILED
      );

  const isRunning = activeOperations.length > 0;
  const hasFailed = failedOperations.length > 0;

  return {
    activeOperations,
    failedOperations,
    isRunning,
    hasFailed,
    operations: Array.from(operations.values()),
  };
}
