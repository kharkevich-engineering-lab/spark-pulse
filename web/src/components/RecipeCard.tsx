/** Recipe card — uses BaseCard with recipe-specific badges and actions. */

import { Ban, Cpu, Gauge, Layers, Network, Package, RotateCcw, Zap } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import BaseCard from "./BaseCard";
import ServesChip from "./ServesChip";
import type { RecipeSummary } from "@/lib/types";

/** Engines that can actually run this recipe, as the API reports them. */
function usableEngines(r: RecipeSummary): string[] {
  if (r.engine_support?.length) {
    return r.engine_support.filter((e) => e.supported && e.enabled).map((e) => e.engine);
  }
  return r.engines ?? [];
}

export default function RecipeCard({ r, isRunning, isQueued, clusterBlocked, notHere, tight, onSelect, onReset }: {
  r: RecipeSummary;
  isRunning: boolean;
  /** A deploy of it is waiting on its model download (Runs shows it). */
  isQueued?: boolean;
  clusterBlocked: boolean;
  /** Why it does not run on this fleet (`fit.reason`). The card is dimmed and
   *  says so, and still opens: the recipe is worth reading. */
  notHere?: string;
  /** Why it runs only just (`fit.reason` of a `tight` verdict): within 10% of
   *  a node's memory. Said on the card, not dimmed — it does run. */
  tight?: string;
  onSelect: () => void;
  onReset?: () => void;
}) {
  const { t } = useI18n();
  const engines = usableEngines(r);
  /** Shipped with the package, so the operator cannot edit the file behind it.
   *  `unknown` — an id no source claims — is not managed: nothing here knows
   *  where it came from, and claiming otherwise would be a guess. */
  const isManaged = r.source === "bundled";
  // Two lines, always: what it is and what runs it, then how it runs. The
  // wrapping row put Solo and Cluster wherever they happened to fit.
  const badges = (
    <>
      <div className="flex w-full flex-wrap gap-1.5" data-testid={`recipe-line1-${r.id}`}>
        {r.source && r.source !== "unknown" && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-text-muted">
            <Package size={11} />{r.source}
          </span>
        )}
        {/* Which engines can run it, one or several: a single-engine recipe
            (the llama.cpp one) used to say nothing at all. */}
        {engines.length > 0 && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-success/15 text-success">
            <Layers size={11} />{engines.join(" · ")}
          </span>
        )}
      </div>
      <div className="flex w-full flex-wrap gap-1.5" data-testid={`recipe-line2-${r.id}`}>
        {(r.solo_only || (!r.solo_only && !r.cluster_only)) && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-primary/20 text-blue2">
            <Cpu size={11} />{t("recipeCard.solo")}
          </span>
        )}
        {(r.cluster_only || (!r.solo_only && !r.cluster_only)) && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-warning/20 text-warning">
            <Network size={11} />{t("recipeCard.cluster")}
          </span>
        )}
        <ServesChip serves={r.serves} />
        {r.is_customized && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-amber/20 text-amber">
            <Zap size={11} />{t("recipeCard.custom")}
          </span>
        )}
        {tight && (
          <span
            data-testid={`tight-${r.id}`}
            title={tight}
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-warn/15 text-warn"
          >
            <Gauge size={11} />{t("recipeCard.tight")}
          </span>
        )}
        {notHere && (
          <span
            data-testid={`not-here-${r.id}`}
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-text-muted"
          >
            <Ban size={11} />{t("recipeCard.notHere", { reason: notHere })}
          </span>
        )}
      </div>
    </>
  );

  const icon = (
    <div className="flex items-center gap-2 shrink-0">
      <Zap size={16} className={isRunning ? "text-success" : "text-blue2"} />
      {isRunning && <span className="flex items-center gap-1 text-xs text-success font-medium"><span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />{t("recipeCard.running")}</span>}
      {!isRunning && isQueued && (
        <span data-testid={`queued-${r.id}`} className="flex items-center gap-1 text-xs text-blue2 font-medium">
          <span className="w-1.5 h-1.5 rounded-full bg-blue2 animate-pulse" />{t("recipeCard.queued")}
        </span>
      )}
      {clusterBlocked && <span className="text-xs text-text-muted">{t("recipeCard.clusterOnly")}</span>}
      {r.is_customized && onReset && (
        <button onClick={(e) => { e.stopPropagation(); onReset(); }} className="p-1 rounded hover:bg-warning/15 text-warning transition-colors" title={t("recipeCard.resetToOriginal")}>
          <RotateCcw size={14} />
        </button>
      )}
    </div>
  );

  return (
    <BaseCard
      icon={icon}
      title={r.name}
      description={r.description || r.model}
      badges={badges}
      onClick={onSelect}
      disabled={clusterBlocked}
      dimmed={Boolean(notHere)}
      hint={isManaged ? t("recipes.managedRecipe") : undefined}
    />
  );
}
