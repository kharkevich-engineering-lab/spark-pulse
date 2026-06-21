import type { DryRunResult } from "@/lib/operations";
import {
  Check,
  X,
  Loader2,
  AlertCircle,
  Clock,
  RotateCcw,
  Play,
  ShieldCheck,
  Server,
  Wrench,
  Activity,
} from "lucide-react";

// ── Deployment Pipeline Steps ────────────────────────────────────────────────

export interface PipelineStep {
  id: string;
  label: string;
  icon: typeof Check;
  status: "pending" | "running" | "success" | "failed" | "skipped";
  error?: string;
  duration?: number;
}

export interface DeploymentPipelineProps {
  steps: PipelineStep[];
  activeStep?: number;
  onCancel?: () => void;
  onRetry?: () => void;
  className?: string;
}

export default function DeploymentPipeline({
  steps,
  activeStep,
  onCancel,
  onRetry,
  className = "",
}: DeploymentPipelineProps) {
  const runningStep = steps.findIndex((s) => s.status === "running");
  const completedSteps = steps.filter((s) => s.status === "success").length;
  const totalSteps = steps.length;
  const progress = totalSteps > 0 ? (completedSteps / totalSteps) * 100 : 0;

  const isComplete = steps.every(
    (s) => s.status === "success" || s.status === "skipped"
  );
  const hasFailed = steps.some((s) => s.status === "failed");

  return (
    <div className={`space-y-4 ${className}`}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Activity size={20} className="text-primary" />
          <h3 className="text-lg font-semibold">Deployment Pipeline</h3>
        </div>
        {(onCancel || hasFailed) && (
          <div className="flex gap-2">
            {onCancel && (
              <button
                onClick={onCancel}
                disabled={isComplete}
                className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg border border-border hover:bg-surface-hover transition-colors disabled:opacity-50"
              >
                <X size={14} />
                Cancel
              </button>
            )}
            {hasFailed && onRetry && (
              <button
                onClick={onRetry}
                className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
              >
                <RotateCcw size={14} />
                Retry
              </button>
            )}
          </div>
        )}
      </div>

      {/* Progress Bar */}
      <div className="h-2 rounded-full bg-surface-hover overflow-hidden">
        <div
          className="h-full bg-primary transition-all duration-300 rounded-full"
          style={{ width: `${progress}%` }}
        />
      </div>
      <div className="flex justify-between text-xs text-text-muted">
        <span>
          {completedSteps}/{totalSteps} steps completed
        </span>
        {runningStep >= 0 && (
          <span className="text-primary">
            Running: {steps[runningStep].label}
          </span>
        )}
      </div>

      {/* Steps */}
      <div className="space-y-2">
        {steps.map((step, index) => {
          const isActive = activeStep === index;
          return (
          <div
            key={step.id}
            className={`flex items-start gap-3 p-3 rounded-lg border transition-colors ${
              isActive ? "ring-2 ring-primary/30" : ""
            } ${
              step.status === "running"
                ? "border-primary bg-primary/5"
                : step.status === "success"
                ? "border-success/30 bg-success/5"
                : step.status === "failed"
                ? "border-danger/30 bg-danger/5"
                : "border-border"
            }`}
          >
            {/* Step Icon */}
            <div className="shrink-0 mt-0.5">
              {step.status === "pending" && (
                <Clock size={18} className="text-text-muted" />
              )}
              {step.status === "running" && (
                <Loader2 size={18} className="text-primary animate-spin" />
              )}
              {step.status === "success" && (
                <Check size={18} className="text-success" />
              )}
              {step.status === "failed" && (
                <X size={18} className="text-danger" />
              )}
              {step.status === "skipped" && (
                <AlertCircle size={18} className="text-text-muted" />
              )}
            </div>

            {/* Step Content */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between">
                <span className={`text-sm font-medium ${
                  step.status === "running"
                    ? "text-primary"
                    : step.status === "success"
                    ? "text-success"
                    : step.status === "failed"
                    ? "text-danger"
                    : "text-text-muted"
                }`}>
                  {step.label}
                </span>
                {step.duration !== undefined && (
                  <span className="text-xs text-text-muted">
                    {step.duration}s
                  </span>
                )}
              </div>

              {/* Error Message */}
              {step.error && (
                <p className="text-sm text-danger mt-1">{step.error}</p>
              )}
            </div>
          </div>
          );
        })}
      </div>

      {/* Complete State */}
      {isComplete && !hasFailed && (
        <div className="p-4 rounded-lg bg-success/5 border border-success/30 flex items-center gap-2">
          <Check size={20} className="text-success" />
          <span className="text-sm text-success font-medium">
            Deployment completed successfully
          </span>
        </div>
      )}
    </div>
  );
}

// ── Pre-Flight Checklist ─────────────────────────────────────────────────────

export interface PreFlightCheck {
  id: string;
  label: string;
  status: "pass" | "fail" | "warning" | "pending";
  message?: string;
}

export interface PreFlightChecklistProps {
  checks: PreFlightCheck[];
  onProceed?: () => void;
  className?: string;
}

export function PreFlightChecklist({
  checks,
  onProceed,
  className = "",
}: PreFlightChecklistProps) {
  const allPassed = checks.every((c) => c.status === "pass");
  const hasFailures = checks.some((c) => c.status === "fail");

  return (
    <div className={`space-y-4 ${className}`}>
      <div className="flex items-center gap-2">
        <ShieldCheck size={20} className="text-primary" />
        <h3 className="text-lg font-semibold">Pre-Flight Checklist</h3>
      </div>

      <div className="space-y-2">
        {checks.map((check) => (
          <div
            key={check.id}
            className={`flex items-start gap-3 p-3 rounded-lg border ${
              check.status === "pass"
                ? "border-success/30 bg-success/5"
                : check.status === "fail"
                ? "border-danger/30 bg-danger/5"
                : check.status === "warning"
                ? "border-warning/30 bg-warning/5"
                : "border-border"
            }`}
          >
            {check.status === "pass" && (
              <Check size={16} className="text-success shrink-0 mt-0.5" />
            )}
            {check.status === "fail" && (
              <X size={16} className="text-danger shrink-0 mt-0.5" />
            )}
            {check.status === "warning" && (
              <AlertCircle size={16} className="text-warning shrink-0 mt-0.5" />
            )}
            {check.status === "pending" && (
              <Clock size={16} className="text-text-muted shrink-0 mt-0.5" />
            )}
            <div className="flex-1">
              <span className="text-sm font-medium">{check.label}</span>
              {check.message && (
                <p className="text-xs text-text-muted mt-1">{check.message}</p>
              )}
            </div>
          </div>
        ))}
      </div>

      {hasFailures && (
        <p className="text-sm text-danger">
          Fix the errors above before proceeding with deployment.
        </p>
      )}

      {allPassed && onProceed && (
        <button
          onClick={onProceed}
          className="w-full flex items-center justify-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors"
        >
          <Play size={16} />
          Proceed with Deployment
        </button>
      )}
    </div>
  );
}

// ── Dry Run Result Display ───────────────────────────────────────────────────

export interface DryRunDisplayProps {
  result: DryRunResult;
  onDeploy?: () => void;
  className?: string;
}

export function DryRunDisplay({
  result,
  onDeploy,
  className = "",
}: DryRunDisplayProps) {
  const hasErrors = result.errors.length > 0;
  const hasWarnings = result.warnings.length > 0;

  return (
    <div className={`space-y-4 ${className}`}>
      <div className="flex items-center gap-2">
        <Wrench size={20} className="text-primary" />
        <h3 className="text-lg font-semibold">Dry Run Results</h3>
      </div>

      {/* Summary */}
      <div className={`p-4 rounded-lg border ${
        hasErrors
          ? "border-danger/30 bg-danger/5"
          : hasWarnings
          ? "border-warning/30 bg-warning/5"
          : "border-success/30 bg-success/5"
      }`}>
        <div className="flex items-center gap-2">
          {hasErrors ? (
            <X size={20} className="text-danger" />
          ) : (
            <Check size={20} className="text-success" />
          )}
          <span className="font-semibold">
            {hasErrors ? "Validation Failed" : "Ready to Deploy"}
          </span>
        </div>
      </div>

      {/* Script Analysis */}
      <div>
        <h4 className="text-sm font-semibold text-text-muted uppercase tracking-wider mb-2">
          Script Analysis
        </h4>
        <div className="space-y-1 text-sm">
          <p>
            <span className="text-text-muted">Path:</span>{" "}
            <code className="px-1 py-0.5 rounded bg-surface-hover">{result.script_analysis.path}</code>
          </p>
          {result.script_analysis.command_line && (
            <p>
              <span className="text-text-muted">Command:</span>{" "}
              <code className="px-1 py-0.5 rounded bg-surface-hover text-xs break-all">
                {result.script_analysis.command_line}
              </code>
            </p>
          )}
        </div>
      </div>

      {/* Parallelism */}
      <div>
        <h4 className="text-sm font-semibold text-text-muted uppercase tracking-wider mb-2">
          Parallelism Configuration
        </h4>
        <div className="flex gap-3">
          <div className="px-3 py-2 rounded-lg bg-surface-hover text-center">
            <span className="text-xs text-text-muted block">TP</span>
            <span className="text-lg font-bold">{result.parallelism.tp}</span>
          </div>
          <div className="px-3 py-2 rounded-lg bg-surface-hover text-center">
            <span className="text-xs text-text-muted block">PP</span>
            <span className="text-lg font-bold">{result.parallelism.pp}</span>
          </div>
          <div className="px-3 py-2 rounded-lg bg-surface-hover text-center">
            <span className="text-xs text-text-muted block">DP</span>
            <span className="text-lg font-bold">{result.parallelism.dp}</span>
          </div>
        </div>
      </div>

      {/* Capacity Check */}
      <div>
        <h4 className="text-sm font-semibold text-text-muted uppercase tracking-wider mb-2">
          Capacity Check
        </h4>
        <div className={`flex items-center gap-2 p-2 rounded-lg ${
          result.capacity_check.valid ? "bg-success/5" : "bg-danger/5"
        }`}>
          {result.capacity_check.valid ? (
            <Check size={16} className="text-success" />
          ) : (
            <AlertCircle size={16} className="text-danger" />
          )}
          <span className="text-sm">{result.capacity_check.message}</span>
        </div>
      </div>

      {/* Warnings */}
      {hasWarnings && (
        <div>
          <h4 className="text-sm font-semibold text-text-muted uppercase tracking-wider mb-2">
            Warnings
          </h4>
          <div className="space-y-1">
            {result.warnings.map((warn, i) => (
              <p key={i} className="text-sm text-warning flex items-center gap-2">
                <AlertCircle size={14} />
                {warn}
              </p>
            ))}
          </div>
        </div>
      )}

      {/* Errors */}
      {hasErrors && (
        <div>
          <h4 className="text-sm font-semibold text-text-muted uppercase tracking-wider mb-2">
            Errors
          </h4>
          <div className="space-y-1">
            {result.errors.map((err, i) => (
              <p key={i} className="text-sm text-danger flex items-center gap-2">
                <X size={14} />
                {err}
              </p>
            ))}
          </div>
        </div>
      )}

      {/* Estimated Duration */}
      <div className="flex items-center gap-2 text-sm text-text-muted">
        <Clock size={14} />
        <span>Estimated duration: ~{Math.round(result.estimated_duration_seconds)}s</span>
      </div>

      {/* Deploy Button */}
      {!hasErrors && onDeploy && (
        <button
          onClick={onDeploy}
          className="w-full flex items-center justify-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors"
        >
          <Server size={16} />
          Deploy Cluster
        </button>
      )}
    </div>
  );
}
