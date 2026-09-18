/** Recipe drawer — uses SlideDrawer for layout, RecipeForm for content. */

import { useState, useEffect, useRef, useCallback } from "react";
import { useI18n } from "@/lib/i18n";
import RecipeForm from "./RecipeForm";
import DeployOptions, { deployParams, type DeployOptionsValue } from "./DeployOptions";
import { Button, ConfirmModal } from "@/ui";
import { X } from "lucide-react";
import SlideDrawer from "./SlideDrawer";
import type { RecipeDetail, RecipeCustomization, RecipeFormRef } from "@/lib/types";

export default function RecipeDrawer({ recipe, customization, isRunning, clusterAvailable, onClose, onError, onDeploy, onSaveCustomization, onReset }: {
  recipe: RecipeDetail;
  customization: RecipeCustomization;
  isRunning: boolean;
  /** Whether this install can run a `cluster_only` recipe at all. */
  clusterAvailable: boolean;
  onClose: () => void;
  onError: (msg: string) => void;
  onDeploy?: (name: string, params: Record<string, unknown>, options?: DeployOptionsValue) => Promise<void>;
  onSaveCustomization?: (fields: Partial<RecipeCustomization>) => void;
  onReset?: () => void | Promise<void>;
}) {
  const { t } = useI18n();
  const formRef = useRef<RecipeFormRef>(null);
  const [deployOptions, setDeployOptions] = useState<DeployOptionsValue>({});
  const clusterBlocked = recipe.cluster_only && !clusterAvailable;
  const hasCustomization = customization && Object.keys(customization).length > 0;
  const [isEditing, setIsEditing] = useState(false);
  const [deploying, setDeploying] = useState(false);
  const [resetConfirm, setResetConfirm] = useState(false);

  useEffect(() => {
    setIsEditing(false);
  }, [recipe.id]); // Reset edit state when recipe changes

  const handleDeploy = async () => {
    if (!onDeploy) return;
    setDeploying(true);
    try {
      const name = formRef.current?.getDeployName() || recipe.name;
      // The parallelism the form is showing, not an empty dict: it is what
      // the Preview was planned against, and a deploy that quietly dropped it
      // would start something other than what the operator was shown.
      await onDeploy(name, deployParams(recipe, deployOptions), deployOptions);
      onClose();
    } catch (e) { onError(e instanceof Error ? e.message : t("recipes.deployFailed")); }
    finally { setDeploying(false); }
  };

  const handleSave = async (fields: Partial<RecipeCustomization>) => {
    if (!onSaveCustomization) return;
    try {
      await onSaveCustomization(fields);
      setIsEditing(false);
    } catch (e) { onError(e instanceof Error ? e.message : t("recipes.saveCustomizationFailed")); }
  };

  const handleReset = useCallback(async () => {
    await onReset?.();
    setIsEditing(false);
  }, [onReset]);

  return (
    <SlideDrawer
      open={!!recipe}
      onClose={onClose}
      header={
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-xl font-bold truncate">{recipe.name}</h3>
            {isRunning && <span className="flex items-center gap-1.5 text-xs text-success font-medium px-2 py-0.5 rounded-full bg-success/15 shrink-0"><span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />{t("recipeCard.running")}</span>}
          </div>
          <p className="text-sm text-text-muted mt-1 truncate">{recipe.model}</p>
        </div>
      }
      actions={
        <>
          {/* Not editing: Deploy + Customize */}
          {!isEditing && (
            <>
              {onDeploy && (
                <Button
                  size="sm"
                  variant="primary"
                  onClick={handleDeploy}
                  disabled={deploying || isRunning || clusterBlocked}
                >
                  {deploying ? "…" : t("recipes.deploy")}
                </Button>
              )}
              {onSaveCustomization && !hasCustomization && (
                <Button size="sm" onClick={() => setIsEditing(true)} disabled={isRunning || clusterBlocked}>
                  {t("common.customize")}
                </Button>
              )}
              {onSaveCustomization && hasCustomization && (
                <Button size="sm" onClick={() => setIsEditing(true)} disabled={isRunning || clusterBlocked}>
                  {t("recipeCard.editCustom")}
                </Button>
              )}
            </>
          )}
          {/* Editing: Save + Reset */}
          {isEditing && (
            <>
              <Button size="sm" variant="primary" onClick={() => formRef.current?.save()}>
                {t("common.save")}
              </Button>
              {/* Ghost, not a colour of its own: `cn` is clsx, so a `text-warn`
                  here would sit beside `Button`'s own `text-text` and the
                  stylesheet's order would decide which won. */}
              {hasCustomization && onSaveCustomization && (
                <Button size="sm" onClick={() => setResetConfirm(true)}>
                  {t("common.reset")}
                </Button>
              )}
            </>
          )}
          <button type="button" onClick={onClose} className="p-1.5 rounded-md hover:bg-surface-hover transition-colors">
            <X size={18} />
          </button>
        </>
      }
    >
      {clusterBlocked && (
        <div className="px-6 py-3 border-b border-border">
          <div className="flex items-start gap-3 p-3 rounded-sm bg-warning/10 border border-warning/30">
            <p className="text-sm text-warning">{t("recipeCard.clusterRequired")}</p>
          </div>
        </div>
      )}
      {onDeploy && !isEditing && (
        <DeployOptions recipe={recipe} value={deployOptions} onChange={setDeployOptions} />
      )}
      <RecipeForm
        ref={formRef}
        recipe={recipe}
        customization={customization}
        onDeploy={handleDeploy}
        onSaveCustomization={handleSave}
        isRunning={isRunning}
        clusterBlocked={clusterBlocked}
        isEditing={isEditing}
      />

      {/* Reset confirmation modal — rendered inside drawer, below content */}
      {resetConfirm && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/30">
          <div className="pointer-events-auto">
            <ConfirmModal
              open={resetConfirm}
              onClose={() => setResetConfirm(false)}
              onConfirm={() => {
                void handleReset();
                setResetConfirm(false);
              }}
              title={t("recipes.resetTitle")}
              message={t("recipes.resetBody", { name: recipe.name })}
              confirmLabel={t("recipes.reset")}
              confirmVariant="danger"
            />
          </div>
        </div>
      )}
    </SlideDrawer>
  );
}
