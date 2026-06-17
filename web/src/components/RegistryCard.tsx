/** Registry card — displays a single OCI registry with status, versions and actions. */

import { useState } from "react";
import { CheckCircle2, XCircle, Loader2, Power, PowerOff, ChevronDown, GitBranch } from "lucide-react";
import type { OciRegistry } from "@/lib/types";

export default function RegistryCard({
  reg,
  versions,
  onToggle,
  onTest,
  onRemove,
  onVersionChange,
}: {
  reg: OciRegistry;
  versions?: string[];
  onToggle: () => void;
  onTest: () => void;
  onRemove: () => void;
  onVersionChange?: (version: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [selectedVersion, setSelectedVersion] = useState<string>("");

  const statusIcon = reg.connected
    ? <CheckCircle2 size={14} className="text-success" />
    : reg.enabled
      ? <XCircle size={14} className="text-destructive" />
      : <XCircle size={14} className="text-text-muted" />;

  const statusColor = reg.connected
    ? "text-success"
    : reg.enabled
      ? "text-destructive"
      : "text-text-muted";

  const hasVersions = versions && versions.length > 0;

  return (
    <div className="rounded-xl border border-border bg-surface hover:bg-surface-hover transition-colors">
      {/* Header row */}
      <div className="flex items-center justify-between p-4">
        <div className="flex items-center gap-3 min-w-0 flex-1">
          <div className={`shrink-0 ${statusColor}`}>
            {statusIcon}
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="font-mono font-semibold truncate">{reg.name}</span>
              {reg.default && (
                <span className="text-xs px-1.5 py-0.5 rounded bg-primary/15 text-primary font-medium">
                  default
                </span>
              )}
              {hasVersions && (
                <button
                  onClick={() => setExpanded(!expanded)}
                  className="flex items-center gap-1 px-2 py-0.5 rounded bg-surface-pressed border border-border text-xs hover:bg-surface-pressed/80 transition-colors"
                >
                  <GitBranch size={12} />
                  <span>{versions?.length} versions</span>
                  <ChevronDown size={12} className={`transition-transform ${expanded ? "rotate-180" : ""}`} />
                </button>
              )}
            </div>
            <div className="text-xs text-text-muted truncate font-mono mt-1">{reg.url}</div>
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0 ml-4">
          <button
            onClick={onTest}
            className="p-1.5 rounded hover:bg-surface-pressed transition-colors"
            title="Test connection"
            disabled={reg.connected}
          >
            {reg.connected ? (
              <CheckCircle2 size={16} className="text-success" />
            ) : (
              <Loader2 size={16} className="animate-spin text-text-muted" />
            )}
          </button>
          <button
            onClick={onToggle}
            className="p-1.5 rounded hover:bg-surface-pressed transition-colors"
            title={reg.enabled ? "Disable" : "Enable"}
          >
            {reg.enabled ? (
              <Power size={16} className="text-success" />
            ) : (
              <PowerOff size={16} className="text-text-muted" />
            )}
          </button>
          {!reg.default && (
            <button
              onClick={onRemove}
              className="p-1.5 rounded hover:bg-destructive/15 transition-colors"
              title="Remove registry"
            >
              <XCircle size={16} className="text-text-muted hover:text-destructive" />
            </button>
          )}
        </div>
      </div>

      {/* Version dropdown (expanded) */}
      {hasVersions && expanded && (
        <div className="px-4 pb-4 border-t border-border pt-3">
          <label className="block text-xs text-text-muted mb-2 font-medium">Available Versions</label>
          <select
            value={selectedVersion}
            onChange={e => {
              setSelectedVersion(e.target.value);
              onVersionChange?.(e.target.value);
            }}
            className="w-full px-3 py-2 rounded-lg border border-border bg-surface text-sm font-mono"
          >
            <option value="">Select a version...</option>
            {versions?.map(v => (
              <option key={v} value={v}>{v}</option>
            ))}
          </select>
        </div>
      )}
    </div>
  );
}
