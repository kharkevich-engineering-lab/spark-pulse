/** Recipe card component.

Shows recipe name, model, description, badges. Shows a "Reset" button
when the recipe has customizations. Shows a "Customized" badge when
customizations exist.
*/

import { ArrowRight, Box, Layers, Cpu, Network, RotateCcw, Wand2 } from "lucide-react";
import type { RecipeSummary } from "@/lib/types";

export default function RecipeCard({ r, isRunning, clusterBlocked, duplicateNames, onSelect, onReset }: {
  r: RecipeSummary;
  isRunning: boolean;
  clusterBlocked: boolean;
  duplicateNames: Set<string>;
  onSelect: () => void;
  onReset?: () => void;
}) {

  return (
    <div onClick={() => !clusterBlocked && onSelect()}
      className={`p-5 rounded-xl bg-surface border transition-colors ${clusterBlocked ? "opacity-50 cursor-not-allowed border-border" : isRunning ? "border-success/50 hover:border-success/80 cursor-pointer group" : "border-border hover:border-border-hover cursor-pointer group"}`}>
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="font-semibold text-base group-hover:text-primary transition-colors">{r.name}</h3>
          <p className="text-xs text-text-muted mt-1 font-mono">{duplicateNames.has(r.name) ? r.id : r.model}</p>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {r.is_customized && onReset && (
            <button
              onClick={(e) => { e.stopPropagation(); onReset(); }}
              className="p-1 rounded hover:bg-warning/15 text-warning transition-colors"
              title="Reset to original"
            >
              <RotateCcw size={14} />
            </button>
          )}
          {isRunning
            ? <span className="flex items-center gap-1.5 text-xs text-success font-medium shrink-0"><span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />Running</span>
            : clusterBlocked
            ? <span className="text-xs text-text-muted shrink-0">Cluster only</span>
            : <ArrowRight size={16} className="text-text-muted group-hover:text-primary transition-colors shrink-0" />}
        </div>
      </div>
      <p className="text-sm text-text-muted mb-4 line-clamp-2">{r.description || r.model}</p>
      <div className="flex flex-wrap gap-2">
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-text-muted"><Box size={12} />{r.container}</span>
        {(r.solo_only || (!r.solo_only && !r.cluster_only)) && <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-primary/20 text-primary"><Cpu size={11} />Solo</span>}
        {(r.cluster_only || (!r.solo_only && !r.cluster_only)) && <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-warning/20 text-warning"><Network size={11} />Cluster</span>}
        {r.is_customized && <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-amber/20 text-amber"><Wand2 size={11} />Custom</span>}
      </div>
      {r.mods.length > 0 && <div className="mt-3 flex items-center gap-1 text-xs text-text-muted"><Layers size={12} /><span>{r.mods.length} mod{r.mods.length > 1 ? "s" : ""}</span></div>}
    </div>
  );
}
