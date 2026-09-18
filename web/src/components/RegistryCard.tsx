/** Registry card — one OCI registry with its state, versions and actions. */

import { useState } from "react";
import { useI18n } from "@/lib/i18n";
import { CheckCircle2, XCircle, Power, PowerOff, ChevronDown, GitBranch, Pencil } from "lucide-react";
import { IconButton, NodeState, Select, type NodeCondition } from "@/ui";
import type { OciRegistry } from "@/lib/types";

export default function RegistryCard({
  reg,
  versions,
  onToggle,
  onTest,
  onRemove,
  onEdit,
  onVersionChange,
}: {
  reg: OciRegistry;
  versions?: string[];
  onToggle: () => void;
  onTest: () => void;
  onRemove: () => void;
  onEdit: () => void;
  onVersionChange?: (version: string) => void;
}) {
  const { t, plural } = useI18n();
  const [expanded, setExpanded] = useState(false);
  const [selectedVersion, setSelectedVersion] = useState<string>("");

  /** Connected, refused, or turned off — in the one node vocabulary.
   *
   *  The "test connection" button used to render a **permanent spinner** for
   *  any registry that was not connected: nothing was in flight, so it span
   *  until the page was closed, saying "working on it" about a registry that
   *  had already failed. It says which of the three it is instead. */
  const state: NodeCondition = reg.connected ? "ok" : reg.enabled ? "bad" : "unknown";

  const hasVersions = versions && versions.length > 0;

  return (
    <div className="rounded-md border border-line bg-surface hover:border-line-strong transition-colors">
      {/* Header row */}
      <div className="flex items-center justify-between p-4">
        <div className="flex items-center gap-3 min-w-0 flex-1">
          <NodeState
            state={state}
            dotOnly
            label={
              reg.connected
                ? t("registry.connected")
                : reg.enabled
                  ? t("registry.notConnected")
                  : t("common.disabled")
            }
          />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="font-mono font-semibold truncate">{reg.name}</span>
              {reg.default && (
                <span className="text-[13px] px-1.5 py-0.5 rounded-full border border-line text-muted font-medium">
                  {t("registry.defaultTag")}
                </span>
              )}
              {hasVersions && (
                <button
                  type="button"
                  onClick={() => setExpanded(!expanded)}
                  className="flex items-center gap-1 px-2 py-0.5 rounded-sm bg-bg2 border border-line text-[13px] hover:border-line-strong transition-colors"
                >
                  <GitBranch size={12} />
                  <span>{plural("registry.versionCount", versions?.length ?? 0)}</span>
                  <ChevronDown size={12} className={`transition-transform ${expanded ? "rotate-180" : ""}`} />
                </button>
              )}
            </div>
            <div className="text-[13px] text-muted truncate font-mono mt-1">{reg.url}</div>
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0 ml-4">
          <IconButton
            size="sm"
            icon={reg.connected ? CheckCircle2 : XCircle}
            label={t("registry.testConnection")}
            onClick={onTest}
            disabled={reg.connected}
            className={`border-transparent hover:border-line ${reg.connected ? "text-good" : "text-muted"}`}
          />
          <IconButton
            size="sm"
            icon={reg.enabled ? Power : PowerOff}
            label={reg.enabled ? t("common.disable") : t("common.enable")}
            onClick={onToggle}
            className={`border-transparent hover:border-line ${reg.enabled ? "text-good" : "text-muted"}`}
          />
          <IconButton
            size="sm"
            icon={Pencil}
            label={t("oci.editRegistryButton")}
            onClick={onEdit}
            className="border-transparent text-muted hover:text-text hover:border-line"
          />
          {!reg.default && (
            <IconButton
              size="sm"
              icon={XCircle}
              label={t("registry.remove")}
              onClick={onRemove}
              className="border-transparent text-muted hover:text-bad hover:border-line"
            />
          )}
        </div>
      </div>

      {/* Version dropdown (expanded) */}
      {hasVersions && expanded && (
        <div className="px-4 pb-4 border-t border-line pt-3">
          <label className="block text-[13px] text-muted mb-2 font-medium" htmlFor={`versions-${reg.name}`}>
            {t("registry.versions")}
          </label>
          <Select
            id={`versions-${reg.name}`}
            value={selectedVersion}
            onChange={(e) => {
              setSelectedVersion(e.target.value);
              onVersionChange?.(e.target.value);
            }}
          >
            <option value="">{t("registry.selectVersion")}</option>
            {versions?.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </Select>
        </div>
      )}
    </div>
  );
}
