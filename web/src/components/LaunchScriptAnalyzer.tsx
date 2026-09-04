import { useState, useCallback } from "react";
import {
  resolveLaunchScript,
  analyzeLaunchScript,
  validateLaunchScript,
} from "@/lib/api";
import {
  AlertCircle,
  Check,
  Loader2,
  Search,
  FileText,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import type {
  LaunchScriptResolveResult,
  LaunchScriptInfo,
  LaunchScriptValidation,
} from "@/lib/types";

interface LaunchScriptAnalyzerProps {
  onAnalysisComplete?: (info: LaunchScriptInfo, validation: LaunchScriptValidation) => void;
  className?: string;
}

export default function LaunchScriptAnalyzer({
  onAnalysisComplete,
  className = "",
}: LaunchScriptAnalyzerProps) {
  const [scriptPath, setScriptPath] = useState("");
  const [resolving, setResolving] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [resolveResult, setResolveResult] = useState<LaunchScriptResolveResult | null>(null);
  const [analysis, setAnalysis] = useState<LaunchScriptInfo | null>(null);
  const [validation, setValidation] = useState<LaunchScriptValidation | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleResolve = useCallback(async () => {
    if (!scriptPath) return;
    setResolving(true);
    setError(null);
    try {
      const result = await resolveLaunchScript({ path: scriptPath });
      setResolveResult(result);
      if (!result.exists) {
        setError(`Script not found: ${result.path}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to resolve script");
    } finally {
      setResolving(false);
    }
  }, [scriptPath]);

  const handleAnalyze = useCallback(async () => {
    const path = resolveResult?.path ?? scriptPath;
    if (!path) return;
    setAnalyzing(true);
    setError(null);
    try {
      const info = await analyzeLaunchScript({ path });
      setAnalysis(info);

      // Auto-validate after analysis
      const val = await validateLaunchScript({ path });
      setValidation(val);

      onAnalysisComplete?.(info, val);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to analyze script");
    } finally {
      setAnalyzing(false);
    }
  }, [scriptPath, resolveResult, onAnalysisComplete]);

  const hasErrors = validation?.errors && validation.errors.length > 0;
  const isValid = validation?.healthy ?? false;

  return (
    <div className={`space-y-4 ${className}`}>
      {/* Header */}
      <div className="flex items-center gap-2">
        <FileText size={20} className="text-primary" />
        <h3 className="text-lg font-semibold">Launch Script Analysis</h3>
      </div>

      {/* Script Path Input */}
      <div className="flex gap-2">
        <input
          type="text"
          value={scriptPath}
          onChange={(e) => setScriptPath(e.target.value)}
          placeholder="Path to launch script (e.g., /opt/spark-vllm-docker/examples/launch_vllm.sh)"
          className="flex-1 px-3 py-2 rounded-lg border border-border bg-surface text-text placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-primary/50"
        />
        <button
          onClick={handleResolve}
          disabled={resolving || !scriptPath}
          className="flex items-center gap-2 px-4 py-2 bg-surface-hover rounded-lg hover:bg-surface-hover/80 transition-colors disabled:opacity-50"
        >
          {resolving ? <Loader2 size={16} className="animate-spin" /> : <Search size={16} />}
          Resolve
        </button>
      </div>

      {/* Resolve Result */}
      {resolveResult && (
        <div className={`p-3 rounded-lg border ${
          resolveResult.exists
            ? "border-success/30 bg-success/5"
            : "border-danger/30 bg-danger/5"
        }`}>
          <div className="flex items-center gap-2">
            {resolveResult.exists ? (
              <Check size={16} className="text-success" />
            ) : (
              <AlertCircle size={16} className="text-danger" />
            )}
            <span className="text-sm font-medium">{resolveResult.path}</span>
          </div>
          {!resolveResult.exists && (
            <p className="text-sm text-danger mt-1">Script not found at resolved path</p>
          )}
        </div>
      )}

      {/* Analyze Button */}
      {resolveResult?.exists && (
        <button
          onClick={handleAnalyze}
          disabled={analyzing}
          className="w-full flex items-center justify-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg hover:bg-primary/90 transition-colors disabled:opacity-50"
        >
          {analyzing ? <Loader2 size={16} className="animate-spin" /> : <Wrench size={16} />}
          {analysis ? "Re-analyze" : "Analyze Script"}
        </button>
      )}

      {/* Error Display */}
      {error && (
        <div className="p-3 rounded-lg bg-danger/10 border border-danger/30 text-danger flex items-center gap-2">
          <AlertCircle size={16} />
          <span className="text-sm">{error}</span>
        </div>
      )}

      {/* Analysis Results */}
      {analysis && (
        <div className="space-y-3">
          {/* Command Line */}
          {analysis.command_line && (
            <div>
              <h4 className="text-sm font-semibold text-text-muted uppercase tracking-wider mb-1">Command</h4>
              <code className="block p-3 rounded-lg bg-surface-hover text-sm font-mono break-all">
                {analysis.command_line}
              </code>
            </div>
          )}

          {/* Parallelism */}
          <div>
            <h4 className="text-sm font-semibold text-text-muted uppercase tracking-wider mb-1">Parallelism</h4>
            <div className="flex gap-4">
              <div className="px-3 py-2 rounded-lg bg-surface-hover">
                <span className="text-xs text-text-muted">TP</span>
                <p className="text-lg font-bold">{analysis.parallelism.tp}</p>
              </div>
              <div className="px-3 py-2 rounded-lg bg-surface-hover">
                <span className="text-xs text-text-muted">PP</span>
                <p className="text-lg font-bold">{analysis.parallelism.pp}</p>
              </div>
              <div className="px-3 py-2 rounded-lg bg-surface-hover">
                <span className="text-xs text-text-muted">DP</span>
                <p className="text-lg font-bold">{analysis.parallelism.dp}</p>
              </div>
            </div>
          </div>

          {/* Backend */}
          {analysis.backend && (
            <div>
              <h4 className="text-sm font-semibold text-text-muted uppercase tracking-wider mb-1">Backend</h4>
              <span className="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-surface-hover text-sm">
                {analysis.backend}
              </span>
            </div>
          )}

          {/* Model Flag */}
          <div>
            <h4 className="text-sm font-semibold text-text-muted uppercase tracking-wider mb-1">Model Flag</h4>
            <div className="flex items-center gap-2">
              {analysis.has_model_flag ? (
                <>
                  <Check size={16} className="text-success" />
                  <span className="text-sm text-success">--model flag detected</span>
                </>
              ) : (
                <>
                  <AlertCircle size={16} className="text-warning" />
                  <span className="text-sm text-warning">Missing --model flag</span>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Validation Results */}
      {validation && (
        <div className={`p-4 rounded-lg border ${
          isValid
            ? "border-success/30 bg-success/5"
            : hasErrors
            ? "border-danger/30 bg-danger/5"
            : "border-warning/30 bg-warning/5"
        }`}>
          <div className="flex items-center gap-2 mb-3">
            <ShieldCheck size={20} className={isValid ? "text-success" : hasErrors ? "text-danger" : "text-warning"} />
            <h4 className="font-semibold">Validation {isValid ? "Passed" : hasErrors ? "Failed" : "Warnings"}</h4>
          </div>

          {/* Errors */}
          {validation.errors && validation.errors.length > 0 && (
            <div className="space-y-1 mb-3">
              <p className="text-sm font-semibold text-danger">Errors:</p>
              {validation.errors.map((err, i) => (
                <p key={i} className="text-sm text-danger pl-4">• {err}</p>
              ))}
            </div>
          )}

          {/* Warnings */}
          {validation.warnings && validation.warnings.length > 0 && (
            <div className="space-y-1">
              <p className="text-sm font-semibold text-warning">Warnings:</p>
              {validation.warnings.map((warn, i) => (
                <p key={i} className="text-sm text-warning pl-4">• {warn}</p>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
