/** Git update notification component.

Renders a bell icon in the header with a dot indicator when updates are
available. Clicking it opens a dropdown with version info and action buttons.
*/

import { useState, useEffect, useCallback, useRef } from "react";
import {
  GitCommit,
  ArrowDownUp,
  Loader2,
  CheckCircle2,
  AlertCircle,
  X,
  Download,
  RefreshCw,
  CircleDot,
} from "lucide-react";
import { fetchGitUpdateStatus, triggerGitFetch, triggerGitPull } from "@/lib/api";
import type { GitUpdateStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

export default function GitUpdateNotification() {
  const [status, setStatus] = useState<GitUpdateStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const dismissTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchStatus = useCallback(async () => {
    try {
      const data = await fetchGitUpdateStatus();
      setStatus(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to fetch status");
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    // Poll every 30 seconds for status updates
    const interval = setInterval(fetchStatus, 30000);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  // Auto-dismiss dropdown when update is no longer available
  useEffect(() => {
    if (dismissTimerRef.current) {
      clearTimeout(dismissTimerRef.current);
      dismissTimerRef.current = null;
    }
    if (status?.version_available && open) {
      dismissTimerRef.current = setTimeout(() => {
        setOpen(false);
        fetchStatus();
      }, 8000);
    }
  }, [status?.version_available, open, fetchStatus]);

  const handleFetch = async () => {
    setActionLoading("fetch");
    try {
      await triggerGitFetch();
      await fetchStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Fetch failed");
    } finally {
      setActionLoading(null);
    }
  };

  const handlePull = async () => {
    setActionLoading("pull");
    try {
      await triggerGitPull();
      await fetchStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Pull failed");
    } finally {
      setActionLoading(null);
    }
  };

  // Don't render if not a repo
  if (!status?.is_repo) return null;

  const hasUpdate = status.version_available;
  const hasError = !status.git_available;

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className={cn(
          "p-2 rounded-lg transition-colors relative",
          hasUpdate
            ? "hover:bg-primary/10 text-primary"
            : hasError
              ? "hover:bg-surface-hover text-warning"
              : "hover:bg-surface-hover text-text-muted"
        )}
        title={hasUpdate ? "Update available" : hasError ? "Git not available" : "Git status"}
      >
        <GitCommit size={18} />
        {hasUpdate && (
          <span className="absolute top-1 right-1 w-2 h-2 rounded-full bg-primary animate-pulse" />
        )}
        {hasError && (
          <span className="absolute top-1 right-1 w-2 h-2 rounded-full bg-warning" />
        )}
      </button>

      {/* Dropdown */}
      {open && (
        <>
          <div
            className="fixed inset-0 z-50"
            onClick={() => setOpen(false)}
          />
          <div className="absolute right-0 top-full mt-2 w-80 rounded-xl bg-surface border border-border shadow-xl z-50 overflow-hidden">
            {/* Header */}
            <div className="px-4 py-3 border-b border-border flex items-center justify-between">
              <div className="flex items-center gap-2">
                <GitCommit size={16} className="text-text-muted" />
                <span className="text-sm font-semibold">Git Auto-Update</span>
              </div>
              <button onClick={() => setOpen(false)} className="p-1 rounded hover:bg-surface-hover">
                <X size={14} />
              </button>
            </div>

            {/* Content */}
            <div className="p-4 space-y-3">
              {error && (
                <div className="flex items-start gap-2 text-xs text-danger bg-danger/10 rounded-lg p-2">
                  <AlertCircle size={14} className="shrink-0 mt-0.5" />
                  <span>{error}</span>
                </div>
              )}

              {!hasUpdate && !hasError && (
                <div className="flex items-center gap-2 text-sm text-text-muted">
                  <CheckCircle2 size={16} className="text-success shrink-0" />
                  <span>Up to date</span>
                </div>
              )}

              {hasUpdate && (
                <div className="space-y-2">
                  <div className="flex items-center gap-2 text-sm text-primary">
                    <CircleDot size={16} className="animate-pulse" />
                    <span className="font-medium">Update available</span>
                  </div>
                  <div className="text-xs text-text-muted space-y-1">
                    {status.local_version && status.remote_version && (
                      <div className="flex items-center gap-1.5">
                        <ArrowDownUp size={12} />
                        <span>
                          <code className="px-1 rounded bg-bg">{status.local_version}</code>
                          <span className="text-text-muted mx-0.5">→</span>
                          <code className="px-1 rounded bg-bg">{status.remote_version}</code>
                        </span>
                      </div>
                    )}
                    {status.has_uncommitted_changes && (
                      <div className="flex items-center gap-1.5 text-warning">
                        <AlertCircle size={12} />
                        <span>Uncommitted changes present</span>
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* Action buttons */}
              <div className="flex gap-2 pt-1">
                <button
                  onClick={handleFetch}
                  disabled={actionLoading === "fetch"}
                  className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg border border-border hover:border-primary/50 text-sm font-medium transition-colors disabled:opacity-50"
                >
                  {actionLoading === "fetch" ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : (
                    <RefreshCw size={14} />
                  )}
                  Fetch
                </button>
                {hasUpdate && (
                  <button
                    onClick={handlePull}
                    disabled={actionLoading === "pull"}
                    className="flex-1 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-white text-sm font-medium transition-colors disabled:opacity-50"
                  >
                    {actionLoading === "pull" ? (
                      <Loader2 size={14} className="animate-spin" />
                    ) : (
                      <Download size={14} />
                    )}
                    Pull
                  </button>
                )}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
