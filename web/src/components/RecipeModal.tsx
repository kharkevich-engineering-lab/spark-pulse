/** Recipe modal wrapper.

Combines the backdrop, keyboard handling, and scrollable container
around RecipeForm. Lifts editing state so header buttons can toggle
edit mode.
*/

import { useState, useEffect, useRef } from "react";
import RecipeForm from "./RecipeForm";
import type { RecipeDetail, RecipeCustomization, RecipeFormRef } from "@/lib/types";

export default function RecipeModal({ recipe, customization, isRunning, clusterEnabled, onClose, onError, onDeploy, onSaveCustomization, onReset }: {
  recipe: RecipeDetail;
  customization: RecipeCustomization;
  isRunning: boolean;
  clusterEnabled: boolean;
  onClose: () => void;
  onError: (msg: string) => void;
  onDeploy?: (name: string, params: Record<string, unknown>) => Promise<void>;
  onSaveCustomization?: (fields: Partial<RecipeCustomization>) => void;
  onReset?: () => void;
}) {
  const modalRef = useRef<HTMLDivElement>(null);
  const formRef = useRef<RecipeFormRef>(null);
  const clusterBlocked = recipe.cluster_only && !clusterEnabled;
  const hasCustomization = customization && Object.keys(customization).length > 0;
  const [isEditing, setIsEditing] = useState(false);
  const [deploying, setDeploying] = useState(false);

  useEffect(() => {
    modalRef.current?.focus();
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // If focus is inside a confirm modal (dialog), let that modal handle it
        const active = document.activeElement;
        if (active && active.closest("[data-confirm-modal]")) return;

        // In edit mode: cancel (restore form, exit edit)
        // Not in edit mode: close modal
        if (isEditing) {
          formRef.current?.cancel();
          setIsEditing(false);
        } else {
          onClose();
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose, isEditing]);

  const handleDeploy = async () => {
    if (!onDeploy) return;
    setDeploying(true);
    try {
      const nameInput = modalRef.current?.querySelector<HTMLInputElement>('input[placeholder="Recipe Name"]');
      const name = nameInput?.value || recipe.name;
      await onDeploy(name, {});
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

  const handleReset = async () => {
    setIsEditing(false);
    onReset?.();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div ref={modalRef} tabIndex={-1} className="relative w-full max-w-2xl max-h-[85vh] overflow-auto rounded-xl bg-surface border border-border outline-none">
        {/* Header row: name + actions */}
        <div className="sticky top-0 bg-surface border-b border-border px-6 py-4 flex items-start justify-between gap-3">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="text-xl font-bold truncate">{recipe.name}</h3>
              {isRunning && <span className="flex items-center gap-1.5 text-xs text-success font-medium px-2 py-0.5 rounded-full bg-success/15 shrink-0"><span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />Running</span>}
            </div>
            <p className="text-sm text-text-muted mt-1 truncate">{recipe.model}</p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {/* Not editing: Deploy + Customize */}
            {!isEditing && (
              <>
                {onDeploy && (
                  <button
                    onClick={handleDeploy}
                    disabled={deploying || isRunning || clusterBlocked}
                    className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium text-sm transition-colors"
                  >
                    {deploying ? "…" : "Deploy"}
                  </button>
                )}
                {onSaveCustomization && !hasCustomization && (
                  <button
                    onClick={() => setIsEditing(true)}
                    disabled={isRunning || clusterBlocked}
                    className="px-3 py-1.5 rounded-lg border border-border hover:border-primary/50 text-sm font-medium transition-colors"
                  >
                    Customize
                  </button>
                )}
                {onSaveCustomization && hasCustomization && (
                  <button
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
                  onClick={() => formRef.current?.save()}
                  className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-white font-medium text-sm transition-colors"
                >
                  Save
                </button>
                {hasCustomization && onSaveCustomization && (
                  <button
                    onClick={handleReset}
                    className="px-3 py-1.5 rounded-lg border border-border hover:border-warning/50 text-sm font-medium transition-colors text-warning"
                  >
                    Reset
                  </button>
                )}
              </>
            )}
            <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-surface-hover transition-colors">✕</button>
          </div>
        </div>

        {clusterBlocked && (
          <div className="px-6 py-3 border-b border-border">
            <div className="flex items-start gap-3 p-3 rounded-lg bg-warning/10 border border-warning/30">
              <p className="text-sm text-warning">This recipe requires cluster mode.</p>
            </div>
          </div>
        )}

        <div className="p-6">
          <RecipeForm
            ref={formRef}
            recipe={recipe}
            customization={customization}
            onDeploy={handleDeploy}
            onSaveCustomization={handleSave}
            onReset={handleReset}
            isRunning={isRunning}
            clusterBlocked={clusterBlocked}
            isEditing={isEditing}
          />
        </div>
      </div>
    </div>
  );
}
