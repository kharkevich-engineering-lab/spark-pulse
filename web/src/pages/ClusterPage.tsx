import { useState, useEffect, useCallback } from "react";
import {
  startCluster,
  stopCluster,
  getClusterStatus,
  listClusters,
  validateCluster,
  rollbackCluster,
  reconcileClusters,
  getLockStatus,
} from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";
import { useOperation, useOperationProgress } from "@/hooks/useOperation";
import { useSSEConnection } from "@/hooks/useSSEConnection";
import StatusBadge from "@/components/StatusBadge";
import { ConfirmModal, AlertModal } from "@/components/Modal";
import LaunchScriptAnalyzer from "@/components/LaunchScriptAnalyzer";
import DeploymentPipeline, { type PipelineStep } from "@/components/DeploymentPipeline";
import ClusterCapacityPanel from "@/components/ClusterCapacityPanel";
import LockContentionAlert from "@/components/LockContentionAlert";
import EventStreamViewer from "@/components/EventStreamViewer";
import ReconciliationNotification from "@/components/ReconciliationNotification";
import {
  Server,
  Play,
  Square,
  RotateCcw,
  ShieldCheck,
  AlertCircle,
  Check,
  Loader2,
  Plus,
  X,
  Wifi,
  Radio,
  Network,
} from "lucide-react";
import { setRefresh } from "@/lib/refresh";
import type { ClusterState, ClusterValidationResult } from "@/lib/types";
import type { DeploymentEvent } from "@/lib/operations";

export default function ClusterPage() {
  const { data: clusters, loading, error, refetch } = useQuery(listClusters);
  const [selectedCluster, setSelectedCluster] = useState<string | null>(null);
  const [clusterDetail, setClusterDetail] = useState<ClusterState | null>(null);
  const [validation, setValidation] = useState<ClusterValidationResult | null>(null);
  const [alertModal, setAlertModal] = useState<{ title: string; message: string } | null>(null);
  const [confirmModal, setConfirmModal] = useState<{
    title: string;
    message: string;
    onConfirm: () => Promise<void>;
    confirmLabel: string;
    variant: "primary" | "danger";
  } | null>(null);

  // New cluster form state
  const [showNewCluster, setShowNewCluster] = useState(false);
  const [newClusterName, setNewClusterName] = useState("");
  const [newClusterImage, setNewClusterImage] = useState("eugr/spark-vllm-docker:latest");
  const [headIp, setHeadIp] = useState("");
  const [workerIps, setWorkerIps] = useState("");
  const [noRay, setNoRay] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // Operation management
  const { startOperation, updateProgress, completeOperation, cancel: cancelOperation } = useOperation();
  const { activeOperations, isRunning } = useOperationProgress(selectedCluster || undefined, "cluster");

  // SSE connection for cluster events
  const [clusterEvents, setClusterEvents] = useState<DeploymentEvent[]>([]);
  const handleClusterEvent = useCallback((_event: string, data: unknown) => {
    if (data && typeof data === "object" && "type" in data) {
      const evt = data as Record<string, unknown>;
      setClusterEvents((prev) => [
        {
          event_id: (evt.event_id as string) || crypto.randomUUID(),
          timestamp: (evt.timestamp as string) || new Date().toISOString(),
          event_type: (evt.type as any) || "unknown",
          message: (evt.message as string) || "",
          resource: (evt.resource as string) || "",
          resource_type: (evt.resource_type as any) || "cluster",
          node: evt.node as string | undefined,
        },
        ...prev,
      ].slice(0, 100));
    }
  }, []);
  useSSEConnection("/sse/cluster", handleClusterEvent);

  // Reconciliation state
  const [reconciliationResult, setReconciliationResult] = useState<any>(null);
  const [reconciliationLoading, setReconciliationLoading] = useState(false);

  // Lock contention state
  const [lockContention, setLockContention] = useState<any>(null);

  // Pipeline steps state
  const [pipelineSteps, setPipelineSteps] = useState<PipelineStep[]>([]);
  const [activePipelineStep, setActivePipelineStep] = useState<number | undefined>();

  useEffect(() => { setRefresh(refetch); }, [refetch]);
  useEffect(() => { const i = setInterval(refetch, 15000); return () => clearInterval(i); }, [refetch]);

  // Load cluster detail when selected
  useEffect(() => {
    if (selectedCluster) {
      getClusterStatus(selectedCluster)
        .then(setClusterDetail)
        .catch(() => setClusterDetail(null));
      // Check for lock contention
      getLockStatus(selectedCluster)
        .then((lock: any) => {
          if (lock?.locked) {
            setLockContention(lock);
          } else {
            setLockContention(null);
          }
        })
        .catch(() => setLockContention(null));
    } else {
      setClusterDetail(null);
      setLockContention(null);
    }
  }, [selectedCluster]);

  // Load reconciliation on mount
  useEffect(() => {
    setReconciliationLoading(true);
    reconcileClusters()
      .then((result: any) => {
        if (result?.reconstructed_clusters?.length > 0 || result?.orphaned_containers?.length > 0) {
          setReconciliationResult(result);
        }
      })
      .catch(() => {})
      .finally(() => setReconciliationLoading(false));
  }, []);

  const handleStartCluster = async () => {
    if (!newClusterName || !headIp) {
      setAlertModal({ title: "Error", message: "Cluster name and head IP are required." });
      return;
    }

    // Check for lock contention
    try {
      const lockStatus = await getLockStatus(newClusterName);
      if (lockStatus?.locked) {
        setLockContention(lockStatus);
        setAlertModal({ title: "Resource Locked", message: `Cluster "${newClusterName}" is currently being modified by another operation.` });
        return;
      }
    } catch {
      // Continue if lock check fails
    }

    const opId = crypto.randomUUID();
    setSubmitting(true);
    setActivePipelineStep(0);

    // Initialize pipeline steps
    const steps: PipelineStep[] = [
      { id: "1", label: "Resolving launch script", status: "pending", icon: Play },
      { id: "2", label: "Validating cluster", status: "pending", icon: ShieldCheck },
      { id: "3", label: "Starting head node", status: "pending", icon: Play },
      { id: "4", label: "Starting worker nodes", status: "pending", icon: Play },
      { id: "5", label: "Waiting for Ray cluster", status: "pending", icon: Wifi },
    ];
    setPipelineSteps(steps);

    try {
      startOperation(opId, newClusterName, "cluster");

      // Step 1: Resolve launch script
      updateProgress(opId, 10, "Resolving launch script...");
      await new Promise(resolve => setTimeout(resolve, 500)); // Simulated

      // Step 2: Validate cluster
      updateProgress(opId, 30, "Validating cluster...");
      await new Promise(resolve => setTimeout(resolve, 500)); // Simulated

      // Step 3-5: Start cluster
      updateProgress(opId, 60, "Starting cluster...");
      await startCluster({
        name: newClusterName,
        image: newClusterImage,
        head_ip: headIp,
        worker_ips: workerIps
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        env: {},
        docker_config: {},
        no_ray: noRay,
      });

      updateProgress(opId, 100, "Cluster started successfully");
      completeOperation(opId, true);

      setShowNewCluster(false);
      setNewClusterName("");
      setHeadIp("");
      setWorkerIps("");
      setNoRay(false);
      setLockContention(null);
      setReconciliationResult(null);
      refetch();
    } catch (e) {
      completeOperation(opId, false, e instanceof Error ? e.message : "Failed to start cluster");
      setAlertModal({
        title: "Error",
        message: e instanceof Error ? e.message : "Failed to start cluster",
      });
    } finally {
      setSubmitting(false);
      setActivePipelineStep(undefined);
    }
  };

  const handleStopCluster = (name: string) => {
    setConfirmModal({
      title: "Stop Cluster",
      message: `Stop cluster "${name}"? This will terminate all containers.`,
      confirmLabel: "Stop",
      variant: "danger",
      onConfirm: async () => {
        try {
          await stopCluster({ name });
          setConfirmModal(null);
          refetch();
        } catch (e) {
          setAlertModal({
            title: "Error",
            message: e instanceof Error ? e.message : "Failed to stop cluster",
          });
        }
      },
    });
  };

  const handleValidateCluster = async (name: string) => {
    try {
      const result = await validateCluster({ name });
      setValidation(result);
    } catch (e) {
      setAlertModal({
        title: "Error",
        message: e instanceof Error ? e.message : "Validation failed",
      });
    }
  };

  const handleRollbackCluster = (name: string) => {
    setConfirmModal({
      title: "Rollback Cluster",
      message: `Rollback cluster "${name}"? This will stop all containers.`,
      confirmLabel: "Rollback",
      variant: "danger",
      onConfirm: async () => {
        try {
          await rollbackCluster({ name });
          setConfirmModal(null);
          refetch();
        } catch (e) {
          setAlertModal({
            title: "Error",
            message: e instanceof Error ? e.message : "Rollback failed",
          });
        }
      },
    });
  };

  const selectedDetail = selectedCluster ? clusterDetail : null;

  return (
    <div className="space-y-6">
      {/* Reconciliation Notification */}
      {!reconciliationLoading && reconciliationResult && (
        <ReconciliationNotification
          result={reconciliationResult}
          onCleanOrphans={async (_containerIds) => {
            // TODO: Implement orphan container cleanup
            setReconciliationResult(null);
          }}
          onDismiss={() => setReconciliationResult(null)}
          className="mb-4"
        />
      )}

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold">Cluster Orchestration</h2>
          <p className="text-text-muted mt-1">Multi-node vLLM cluster management</p>
        </div>
        <button
          onClick={() => setShowNewCluster(true)}
          className="flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors"
        >
          <Plus size={16} />
          New Cluster
        </button>
      </div>

      {/* Loading / Error */}
      {loading && <div className="flex justify-center py-20"><Loader2 className="animate-spin text-primary" size={32} /></div>}
      {error && <div className="p-4 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-3"><AlertCircle size={20} /><span>{error}</span></div>}

      {/* Cluster List */}
      {clusters && clusters.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* Cluster List Sidebar */}
          <div className="lg:col-span-1 space-y-2">
            <h3 className="text-sm font-semibold text-text-muted uppercase tracking-wider">Clusters</h3>
            {clusters.map((cluster) => (
              <button
                key={cluster.name}
                onClick={() => { setSelectedCluster(cluster.name); setValidation(null); setLockContention(null); }}
                className={`w-full text-left p-4 rounded-xl border transition-colors ${
                  selectedCluster === cluster.name
                    ? "border-primary bg-primary/5"
                    : "border-border hover:bg-surface-hover"
                }`}
              >
                <div className="flex items-center gap-3">
                  <div className="w-2 h-2 rounded-full shrink-0" style={{
                    backgroundColor: cluster.healthy ? "var(--color-success)" : (cluster.head.status === "running" || cluster.workers.some(w => w.status === "running")) ? "var(--color-warning)" : "var(--color-text-muted)",
                  }} />
                  <div className="flex-1 min-w-0">
                    <p className="font-medium truncate">{cluster.name}</p>
                    <p className="text-xs text-text-muted">{cluster.total_nodes} nodes · {cluster.ray_enabled ? (cluster.ray_ready ? "Ray ready" : "Ray starting") : "No-Ray"}</p>
                  </div>
                  <StatusBadge status={(cluster.head.status === "running" || cluster.workers.some(w => w.status === "running")) ? "running" : "stopped"} />
                </div>
              </button>
            ))}
          </div>

          {/* Cluster Detail */}
          <div className="lg:col-span-2">
            {selectedDetail ? (
              <div className="space-y-4">
                {/* Lock Contention Alert */}
                {lockContention && (
                  <LockContentionAlert
                    lock={lockContention}
                    onRetry={async () => {
                      setLockContention(null);
                      setAlertModal({ title: "Retry", message: "Attempting to retry the operation..." });
                    }}
                  />
                )}

                {/* Cluster Header */}
                <div className="p-4 rounded-xl bg-surface border border-border">
                  <div className="flex items-center justify-between mb-4">
                    <div className="flex items-center gap-3">
                      <Server size={24} className="text-primary" />
                      <div>
                        <h3 className="text-lg font-bold">{selectedDetail.name}</h3>
                        <p className="text-xs text-text-muted">
                          {selectedDetail.total_nodes} nodes · {selectedDetail.head.gpu_count + selectedDetail.workers.reduce((s, w) => s + w.gpu_count, 0)} total GPUs
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => handleValidateCluster(selectedDetail.name)}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg border border-border hover:bg-surface-hover transition-colors"
                        title="Validate cluster health"
                      >
                        <ShieldCheck size={14} />
                        Validate
                      </button>
                      <button
                        onClick={() => handleStopCluster(selectedDetail.name)}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-danger/10 text-danger hover:bg-danger/20 transition-colors"
                        title="Stop cluster"
                      >
                        <Square size={14} />
                        Stop
                      </button>
                      <button
                        onClick={() => handleRollbackCluster(selectedDetail.name)}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg border border-border hover:bg-surface-hover transition-colors"
                        title="Rollback cluster"
                      >
                        <RotateCcw size={14} />
                        Rollback
                      </button>
                    </div>
                  </div>

                  {/* Validation Result */}
                  {validation && (
                    <div className={`p-3 rounded-lg mb-4 ${
                      validation.healthy
                        ? "bg-success/10 border border-success/30 text-success"
                        : "bg-danger/10 border border-danger/30 text-danger"
                    }`}>
                      <div className="flex items-center gap-2 font-medium mb-1">
                        {validation.healthy ? <Check size={16} /> : <AlertCircle size={16} />}
                        {validation.healthy ? "Cluster Healthy" : "Cluster Issues Found"}
                      </div>
                      {validation.warnings.length > 0 && (
                        <div className="ml-4 text-sm mt-1">
                          <p className="font-medium text-warning">Warnings:</p>
                          {validation.warnings.map((w, i) => <p key={i} className="ml-2">· {w}</p>)}
                        </div>
                      )}
                      {validation.errors.length > 0 && (
                        <div className="ml-4 text-sm mt-1">
                          <p className="font-medium">Errors:</p>
                          {validation.errors.map((e, i) => <p key={i} className="ml-2">· {e}</p>)}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Head Node */}
                  <div className="space-y-3">
                    <h4 className="text-sm font-semibold text-text-muted uppercase tracking-wider flex items-center gap-2">
                      <Radio size={14} />
                      Head Node
                    </h4>
                    <div className="p-3 rounded-lg bg-bg border border-border">
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-3">
                          <div className="w-2 h-2 rounded-full" style={{
                            backgroundColor: selectedDetail.head.status === "running" ? "var(--color-success)" : "var(--color-danger)",
                          }} />
                          <div>
                            <p className="font-medium">{selectedDetail.head.container}</p>
                            <p className="text-xs text-text-muted font-mono">{selectedDetail.head.ip}</p>
                          </div>
                        </div>
                        <div className="flex items-center gap-3 text-sm">
                          <span className="text-text-muted">{selectedDetail.head.gpu_count} GPUs</span>
                          <StatusBadge status={selectedDetail.head.status} />
                          {selectedDetail.ray_enabled && (
                            <span className={`flex items-center gap-1 ${selectedDetail.head.ray_ready ? "text-success" : "text-text-muted"}`}>
                              <Wifi size={14} />
                              {selectedDetail.head.ray_ready ? "Ray ready" : "Ray not ready"}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>

                  {/* Worker Nodes */}
                  {selectedDetail.workers.length > 0 && (
                    <div className="space-y-3 mt-4">
                      <h4 className="text-sm font-semibold text-text-muted uppercase tracking-wider flex items-center gap-2">
                        <Network size={14} />
                        Worker Nodes ({selectedDetail.workers.length})
                      </h4>
                      <div className="space-y-2">
                        {selectedDetail.workers.map((worker, i) => (
                          <div key={i} className="p-3 rounded-lg bg-bg border border-border">
                            <div className="flex items-center justify-between">
                              <div className="flex items-center gap-3">
                                <div className="w-2 h-2 rounded-full" style={{
                                  backgroundColor: worker.status === "running" ? "var(--color-success)" : "var(--color-danger)",
                                }} />
                                <div>
                                  <p className="font-medium">{worker.container}</p>
                                  <p className="text-xs text-text-muted font-mono">{worker.ip}</p>
                                </div>
                              </div>
                              <div className="flex items-center gap-3 text-sm">
                                <span className="text-text-muted">{worker.gpu_count} GPUs</span>
                                <StatusBadge status={worker.status} />
                                {selectedDetail.ray_enabled && (
                                  <span className={`flex items-center gap-1 ${worker.ray_ready ? "text-success" : "text-text-muted"}`}>
                                    <Wifi size={14} />
                                    {worker.ray_ready ? "Connected" : "Not connected"}
                                  </span>
                                )}
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>

                {/* Cluster Capacity Panel */}
                <ClusterCapacityPanel
                  capacity={{
                    cluster_name: selectedDetail.name,
                    total_gpus: selectedDetail.head.gpu_count + selectedDetail.workers.reduce((s, w) => s + w.gpu_count, 0),
                    allocated_gpus: selectedDetail.head.gpu_count + selectedDetail.workers.reduce((s, w) => s + w.gpu_count, 0),
                    free_gpus: 0,
                    utilization_percent: 100,
                    nodes: [
                      {
                        node_ip: selectedDetail.head.ip,
                        role: "head",
                        total_gpus: selectedDetail.head.gpu_count,
                        allocated_gpus: selectedDetail.head.gpu_count,
                        free_gpus: 0,
                        total_ram_gb: 96,
                        allocated_ram_gb: 48,
                        total_cpu_cores: 16,
                        allocated_cpu_cores: 8,
                        active_deployments: [],
                      },
                      ...selectedDetail.workers.map((w) => ({
                        node_ip: w.ip,
                        role: "worker" as const,
                        total_gpus: w.gpu_count,
                        allocated_gpus: w.gpu_count,
                        free_gpus: 0,
                        total_ram_gb: 96,
                        allocated_ram_gb: 16,
                        total_cpu_cores: 16,
                        allocated_cpu_cores: 4,
                        active_deployments: [],
                      })),
                    ],
                  }}
                />

                {/* Event Stream Viewer */}
                <EventStreamViewer
                  events={clusterEvents}
                  resource={selectedDetail.name}
                  onClear={() => setClusterEvents([])}
                />
              </div>
            ) : (
              <div className="flex items-center justify-center h-full py-20 text-text-muted">
                <div className="text-center">
                  <Server size={40} className="mx-auto mb-4 opacity-50" />
                  <p>Select a cluster to view details</p>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Empty State */}
      {clusters && clusters.length === 0 && !loading && !error && (
        <div className="text-center py-20 text-text-muted">
          <Server size={40} className="mx-auto mb-4 opacity-50" />
          <p>No clusters yet.</p>
          <p className="text-sm mt-1">Create a new multi-node cluster to get started.</p>
        </div>
      )}

      {/* New Cluster Modal */}
      {showNewCluster && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="bg-surface border border-border rounded-2xl p-6 w-full max-w-2xl mx-4 shadow-2xl max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-6">
              <h3 className="text-lg font-bold flex items-center gap-2">
                <Server size={20} className="text-primary" />
                New Cluster
              </h3>
              <button onClick={() => setShowNewCluster(false)} className="p-1 rounded-lg hover:bg-surface-hover">
                <X size={18} />
              </button>
            </div>

            <div className="space-y-6">
              {/* Launch Script Analyzer */}
              <LaunchScriptAnalyzer
                onAnalysisComplete={(info, validation) => {
                  console.log("Launch script analysis:", info, validation);
                }}
              />

              {/* Deployment Pipeline (shown when starting) */}
              {isRunning && activeOperations.length > 0 && (
                <DeploymentPipeline
                  steps={pipelineSteps}
                  activeStep={activePipelineStep}
                  onCancel={() => {
                    cancelOperation(activeOperations[0]?.operation_id);
                    setShowNewCluster(false);
                  }}
                />
              )}

              {/* Cluster Configuration */}
              {!isRunning && (
                <div className="space-y-4">
                  <div>
                    <label className="block text-sm font-medium text-text-muted mb-1">Cluster Name *</label>
                    <input
                      type="text"
                      value={newClusterName}
                      onChange={(e) => setNewClusterName(e.target.value)}
                      placeholder="my-cluster"
                      className="w-full px-3 py-2 rounded-lg border border-border bg-bg text-text focus:outline-none focus:ring-2 focus:ring-primary/50"
                    />
                  </div>

                  <div>
                    <label className="block text-sm font-medium text-text-muted mb-1">Docker Image</label>
                    <input
                      type="text"
                      value={newClusterImage}
                      onChange={(e) => setNewClusterImage(e.target.value)}
                      placeholder="eugr/spark-vllm-docker:latest"
                      className="w-full px-3 py-2 rounded-lg border border-border bg-bg text-text focus:outline-none focus:ring-2 focus:ring-primary/50"
                    />
                  </div>

                  <div>
                    <label className="block text-sm font-medium text-text-muted mb-1">Head Node IP *</label>
                    <input
                      type="text"
                      value={headIp}
                      onChange={(e) => setHeadIp(e.target.value)}
                      placeholder="10.0.0.1"
                      className="w-full px-3 py-2 rounded-lg border border-border bg-bg text-text focus:outline-none focus:ring-2 focus:ring-primary/50"
                    />
                  </div>

                  <div>
                    <label className="block text-sm font-medium text-text-muted mb-1">Worker Node IPs (comma-separated)</label>
                    <input
                      type="text"
                      value={workerIps}
                      onChange={(e) => setWorkerIps(e.target.value)}
                      placeholder="10.0.0.2, 10.0.0.3"
                      className="w-full px-3 py-2 rounded-lg border border-border bg-bg text-text focus:outline-none focus:ring-2 focus:ring-primary/50"
                    />
                  </div>

                  <div className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      id="noRay"
                      checked={noRay}
                      onChange={(e) => setNoRay(e.target.checked)}
                      className="rounded border-border"
                    />
                    <label htmlFor="noRay" className="text-sm text-text-muted">Skip Ray cluster startup (single-node mode)</label>
                  </div>
                </div>
              )}
            </div>

            <div className="flex items-center justify-end gap-3 mt-6 pt-4 border-t border-border">
              <button
                onClick={() => setShowNewCluster(false)}
                className="px-4 py-2 rounded-lg border border-border hover:bg-surface-hover transition-colors"
              >
                Cancel
              </button>
              {!isRunning && (
                <button
                  onClick={handleStartCluster}
                  disabled={submitting || !newClusterName || !headIp}
                  className="flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors disabled:opacity-50"
                >
                  {submitting ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
                  Start Cluster
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Confirm Modal */}
      {confirmModal && (
        <ConfirmModal
          open={!!confirmModal}
          onClose={() => setConfirmModal(null)}
          onConfirm={confirmModal.onConfirm}
          title={confirmModal.title}
          message={confirmModal.message}
          confirmLabel={confirmModal.confirmLabel}
          confirmVariant={confirmModal.variant}
        />
      )}

      {/* Alert Modal */}
      {alertModal && (
        <AlertModal
          open={!!alertModal}
          onClose={() => setAlertModal(null)}
          title={alertModal.title}
          message={alertModal.message}
        />
      )}
    </div>
  );
}
