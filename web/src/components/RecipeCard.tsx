/** Recipe card — uses BaseCard with recipe-specific badges and actions. */

import { Box, Cpu, Layers, Network, Package, RotateCcw, Zap } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import BaseCard from "./BaseCard";
import type { RecipeSummary } from "@/lib/types";

/** Engines that can actually run this recipe, as the API reports them. */
function usableEngines(r: RecipeSummary): string[] {
  if (r.engine_support?.length) {
    return r.engine_support.filter((e) => e.supported && e.enabled).map((e) => e.engine);
  }
  return r.engines ?? [];
}

export default function RecipeCard({ r, isRunning, clusterBlocked, onSelect, onReset, onUninstall }: {
  r: RecipeSummary;
  isRunning: boolean;
  clusterBlocked: boolean;
  onSelect: () => void;
  onReset?: () => void;
  /** Uninstalls an OCI-installed recipe. Only ever passed for `source ===
   *  "oci"` — a bundled recipe has nothing this can remove. */
  onUninstall?: () => void;
}) {
  const { t } = useI18n();
  const engines = usableEngines(r);
  const isOci = r.source === "oci";
  /** Shipped with the package, so the operator cannot edit the file behind it.
   *  `unknown` — an id no source claims — is not managed: nothing here knows
   *  where it came from, and claiming otherwise would be a guess. */
  const isManaged = r.source === "bundled";
  const badges = (
    <>
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-text-muted">
        <Box size={12} />{r.container}
      </span>
      {r.source && r.source !== "unknown" && (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg text-text-muted">
          <Package size={11} />{r.source}
        </span>
      )}
      {engines.length > 1 && (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-success/15 text-success">
          <Layers size={11} />{engines.join(" · ")}
        </span>
      )}
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
      {r.is_customized && (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-amber/20 text-amber">
          <Zap size={11} />{t("recipeCard.custom")}
        </span>
      )}
    </>
  );

  const icon = (
    <div className="flex items-center gap-2 shrink-0">
      <Zap size={16} className={isRunning ? "text-success" : "text-blue2"} />
      {isRunning && <span className="flex items-center gap-1 text-xs text-success font-medium"><span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />{t("recipeCard.running")}</span>}
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
      onDelete={isOci ? onUninstall : undefined}
      deleteLabel={isOci ? t("recipes.uninstallRecipe", { name: r.name }) : undefined}
      hint={isManaged ? t("recipes.managedRecipe") : undefined}
    />
  );
}
