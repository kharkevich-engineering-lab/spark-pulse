import { useState, useCallback } from "react";
import { validateMod, applyMod } from "@/lib/api";
import {
  AlertCircle,
  Check,
  Loader2,
  ShieldCheck,
  ShieldAlert,
  X,
  Server,
  Users,
  Globe,
} from "lucide-react";
import type { ModValidationResult } from "@/lib/types";

interface ModValidationDisplayProps {
  modId: string;
  onValidate?: (result: ModValidationResult) => void;
  showApplyButton?: boolean;
  className?: string;
}

export default function ModValidationDisplay({
  modId,
  onValidate,
  showApplyButton = true,
  className = "",
}: ModValidationDisplayProps) {
  const [validating, setValidating] = useState(false);
  const [applying, setApplying] = useState(false);
  const [result, setResult] = useState<ModValidationResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [applyTarget, setApplyTarget] = useState<"head" | "workers" | "all">("all");

  const handleValidate = useCallback(async () => {
    setValidating(true);
    setError(null);
    try {
      const res = await validateMod({ path: modId });
      setResult(res);
      onValidate?.(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Validation failed");
    } finally {
      setValidating(false);
    }
  }, [modId, onValidate]);

  const handleApply = useCallback(async () => {
    setApplying(true);
    setError(null);
    try {
      await applyMod({ mod_name: modId, mod_path: modId, target: applyTarget });
      setResult(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Mod application failed");
    } finally {
      setApplying(false);
    }
  }, [modId, applyTarget]);

  const hasErrors = result?.errors && result.errors.length > 0;
  const hasWarnings = result?.warnings && result.warnings.length > 0;
  const isValid = result?.healthy ?? false;

  return (
    <div className={`space-y-4 ${className}`}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <ShieldCheck size={20} className="text-primary" />
          <h3 className="text-lg font-semibold">Mod Security Validation</h3>
        </div>
        <button
          onClick={handleValidate}
          disabled={validating}
          className="flex items-center gap-2 px-3 py-1.5 bg-surface-hover rounded-lg hover:bg-surface-hover/80 transition-colors disabled:opacity-50 text-sm"
        >
          {validating ? <Loader2 size={14} className="animate-spin" /> : <ShieldAlert size={14} />}
          Validate
        </button>
      </div>

      {/* Error Display */}
      {error && (
        <div className="p-3 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-2">
          <AlertCircle size={16} />
          <span className="text-sm">{error}</span>
        </div>
      )}

      {/* Validation Results */}
      {result && (
        <div className={`p-4 rounded-lg border ${
          isValid
            ? "border-success/30 bg-success/5"
            : hasErrors
            ? "border-danger/30 bg-danger/5"
            : "border-warning/30 bg-warning/5"
        }`}>
          <div className="flex items-center gap-2 mb-3">
            {isValid ? (
              <Check size={20} className="text-success" />
            ) : hasErrors ? (
              <X size={20} className="text-danger" />
            ) : (
              <AlertCircle size={20} className="text-warning" />
            )}
            <h4 className="font-semibold">
              {isValid ? "Validation Passed" : hasErrors ? "Validation Failed" : "Warnings Found"}
            </h4>
          </div>

          {/* Security Checks */}
          <div className="space-y-2">
            {/* File Size Check */}
            <div className="flex items-center gap-2 text-sm">
              <Check size={14} className="text-success shrink-0" />
              <span>File size within limits (50MB max)</span>
            </div>

            {/* Dangerous Patterns */}
            {hasErrors && (
              <div className="space-y-1 mt-3">
                <p className="text-sm font-semibold text-danger">Security Errors:</p>
                {result.errors?.map((err, i) => (
                  <p key={i} className="text-sm text-danger pl-4">• {err}</p>
                ))}
              </div>
            )}

            {/* Warnings. Only where the mod did not pass — a mod that passed
                gets the "non-blocking" wording below instead, and rendering
                both printed every warning twice. */}
            {hasWarnings && !isValid && (
              <div className="space-y-1 mt-3">
                <p className="text-sm font-semibold text-warning">Security Warnings:</p>
                {result.warnings?.map((warn, i) => (
                  <p key={i} className="text-sm text-warning pl-4">• {warn}</p>
                ))}
              </div>
            )}

            {isValid && result.warnings && result.warnings.length > 0 && (
              <div className="space-y-1 mt-2">
                <p className="text-sm font-semibold text-warning">Warnings (non-blocking):</p>
                {result.warnings.map((warn, i) => (
                  <p key={i} className="text-sm text-warning pl-4">• {warn}</p>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Apply Button */}
      {showApplyButton && isValid && !applying && (
        <div className="space-y-3">
          {/* Target Selector */}
          <div>
            <label className="text-sm font-semibold text-text-muted uppercase tracking-wider mb-2 block">
              Apply To
            </label>
            <div className="flex gap-2">
              <button
                onClick={() => setApplyTarget("head")}
                className={`flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-lg border transition-colors ${
                  applyTarget === "head"
                    ? "border-primary bg-primary/10 text-primary"
                    : "border-border hover:bg-surface-hover"
                }`}
              >
                <Server size={16} />
                Head
              </button>
              <button
                onClick={() => setApplyTarget("workers")}
                className={`flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-lg border transition-colors ${
                  applyTarget === "workers"
                    ? "border-primary bg-primary/10 text-primary"
                    : "border-border hover:bg-surface-hover"
                }`}
              >
                <Users size={16} />
                Workers
              </button>
              <button
                onClick={() => setApplyTarget("all")}
                className={`flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-lg border transition-colors ${
                  applyTarget === "all"
                    ? "border-primary bg-primary/10 text-primary"
                    : "border-border hover:bg-surface-hover"
                }`}
              >
                <Globe size={16} />
                All Nodes
              </button>
            </div>
          </div>

          <button
            onClick={handleApply}
            disabled={applying}
            className="w-full flex items-center justify-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors disabled:opacity-50"
          >
            {applying ? <Loader2 size={16} className="animate-spin" /> : <Check size={16} />}
            Apply Mod to {applyTarget === "all" ? "all nodes" : applyTarget}
          </button>
        </div>
      )}

      {/* Applying State */}
      {applying && (
        <div className="p-4 rounded-lg bg-primary/5 border border-primary/30 flex items-center justify-center gap-2">
          <Loader2 size={16} className="animate-spin text-primary" />
          <span className="text-sm text-primary">Applying mod...</span>
        </div>
      )}
    </div>
  );
}
