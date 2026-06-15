/** Registry card — displays a single OCI registry with status and actions. */

import { CheckCircle2, XCircle, Loader2, Power, PowerOff } from "lucide-react";
import type { OciRegistry } from "@/lib/types";

export default function RegistryCard({
  reg,
  onToggle,
  onTest,
  onRemove,
}: {
  reg: OciRegistry;
  onToggle: () => void;
  onTest: () => void;
  onRemove: () => void;
}) {
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

  return (
    <div className="flex items-center justify-between p-3 rounded-lg border border-border bg-surface hover:bg-surface-hover transition-colors">
      <div className="flex items-center gap-3 min-w-0">
        <div className={`shrink-0 ${statusColor}`}>
          {statusIcon}
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-mono font-semibold truncate">{reg.name}</span>
            {reg.default && (
              <span className="text-xs px-1.5 py-0.5 rounded bg-primary/15 text-primary font-medium">
                default
              </span>
            )}
          </div>
          <div className="text-xs text-text-muted truncate font-mono">{reg.url}</div>
        </div>
      </div>
      <div className="flex items-center gap-1.5 shrink-0">
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
  );
}
