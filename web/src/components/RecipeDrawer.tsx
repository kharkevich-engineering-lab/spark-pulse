/** Recipe drawer — uses SlideDrawer for layout, RecipeForm for content. */

import { useState, useEffect, useRef, useCallback } from "react";
import RecipeForm from "./RecipeForm";
import DeployOptions, { type DeployOptionsValue } from "./DeployOptions";
import { ConfirmModal } from "@/components/Modal";
import { X } from "lucide-react";
import SlideDrawer from "./SlideDrawer";
import type { RecipeDetail, RecipeCustomization, RecipeFormRef } from "@/lib/types";

export default function RecipeDrawer({ recipe, customization, isRunning, clusterEnabled, onClose, onError, onDeploy, onSaveCustomization, onReset }: {
  recipe: RecipeDetail;
  customization: RecipeCustomization;
  isRunning: boolean;
  clusterEnabled: boolean;
  onClose: () => void;
  onError: (msg: string) => void;
  onDeploy?: (name: string, params: Record<string, unknown>, options?: DeployOptionsValue) => Promise<void>;
  onSaveCustomization?: (fields: Partial<RecipeCustomization>) => void;
  onReset?: () => void | Promise<void>;
}) {
  const formRef = useRef<RecipeFormRef>(null);
  const [deployOptions, setDeployOptions] = useState<DeployOptionsValue>({});
  const clusterBlocked = recipe.cluster_only && !clusterEnabled;
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
      await onDeploy(name, {}, deployOptions);
      onClose();
    } catch (e) { onError(e instanceof Error ? e.message : "Failed to deploy"); }
    finally { setDeploying(false); }
  };

  const handleSave = async (fields: Partial<RecipeCustomization>) => {
    if (!onSaveCustomization) return;
    try {
      await onSaveCustomization(fields);
      setIsEditing(false);
    } catch (e) { onError(e instanceof Error ? e.message : "Failed to save customization"); }
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
            {isRunning && <span className="flex items-center gap-1.5 text-xs text-success font-medium px-2 py-0.5 rounded-full bg-success/15 shrink-0"><span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />Running</span>}
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
                <button
                  type="button"
                  onClick={handleDeploy}
                  disabled={deploying || isRunning || clusterBlocked}
                  className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium text-sm transition-colors"
                >
                  {deploying ? "…" : "Deploy"}
                </button>
              )}
              {onSaveCustomization && !hasCustomization && (
                <button
                  type="button"
                  onClick={() => setIsEditing(true)}
                  disabled={isRunning || clusterBlocked}
                  className="px-3 py-1.5 rounded-lg border border-border hover:border-primary/50 text-sm font-medium transition-colors"
                >
                  Customize
                </button>
              )}
              {onSaveCustomization && hasCustomization && (
                <button
                  type="button"
                  onClick={() => setIsEditing(true)}
                  disabled={isRunning || clusterBlocked}
                  className="px-3 py-1.5 rounded-lg border border-border hover:border-primary/50 text-sm font-medium transition-colors"
                >
                  Edit Custom
                </button>
              )}
            </>
          )}
          {/* Editing: Save + Reset */}
          {isEditing && (
            <>
              <button
                type="button"
                onClick={() => formRef.current?.save()}
                className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-white font-medium text-sm transition-colors"
              >
                Save
              </button>
              {hasCustomization && onSaveCustomization && (
                <button
                  type="button"
                  onClick={() => setResetConfirm(true)}
                  className="px-3 py-1.5 rounded-lg border border-border hover:border-warning/50 text-sm font-medium transition-colors text-warning"
                >
                  Reset
                </button>
              )}
            </>
          )}
          <button type="button" onClick={onClose} className="p-1.5 rounded-lg hover:bg-surface-hover transition-colors">
            <X size={18} />
          </button>
        </>
      }
    >
      {clusterBlocked && (
        <div className="px-6 py-3 border-b border-border">
          <div className="flex items-start gap-3 p-3 rounded-lg bg-warning/10 border border-warning/30">
            <p className="text-sm text-warning">This recipe requires cluster mode.</p>
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
              title="Reset Customization"
              message={`Reset "${recipe.name}" to its original recipe? Any customizations you made will be lost.`}
              confirmLabel="Reset"
              confirmVariant="danger"
            />
          </div>
        </div>
      )}
    </SlideDrawer>
  );
}
