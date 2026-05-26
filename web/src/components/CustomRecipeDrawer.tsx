/** Drawer for editing a custom recipe's YAML content. */

import { useState, useEffect, useRef } from "react";
import { Save, Upload, Trash2, X } from "lucide-react";
import { ConfirmModal } from "@/components/Modal";
import type { CustomRecipeInfo } from "@/lib/types";
import { getCustomRecipeContent } from "@/lib/api";

export default function CustomRecipeDrawer({
  open,
  recipe,
  onClose,
  onSave,
  onDelete,
  onError,
}: {
  open: boolean;
  recipe: CustomRecipeInfo | null;
  onClose: () => void;
  onSave: (id: string, content: string) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  onError: (msg: string) => void;
}) {
  const [content, setContent] = useState("");
  const [loadingContent, setLoadingContent] = useState(false);
  const [saving, setSaving] = useState(false);
  const [showDelete, setShowDelete] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const drawerRef = useRef<HTMLDivElement>(null);

  // Load content when drawer opens or recipe changes.
  // Ignore stale async responses if the selected recipe changes mid-request.
  useEffect(() => {
    if (!open || !recipe) {
      setContent("");
      setLoadingContent(false);
      return;
    }

    let cancelled = false;
    setLoadingContent(true);

    const recipeId = recipe.id;
    void (async () => {
      try {
        const data = await getCustomRecipeContent(recipeId);
        if (!cancelled) {
          setContent(data.content);
        }
      } catch (e) {
        if (!cancelled) {
          onError(e instanceof Error ? e.message : "Failed to load recipe");
        }
      } finally {
        if (!cancelled) {
          setLoadingContent(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [open, recipe?.id, onError]);

  // Focus drawer on open
  useEffect(() => {
    if (open) {
      drawerRef.current?.focus();
      const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
      window.addEventListener("keydown", handler);
      return () => window.removeEventListener("keydown", handler);
    }
  }, [open, onClose]);

  const handleSave = async () => {
    if (!recipe || !content.trim()) return;
    setSaving(true);
    try {
      await onSave(recipe.id, content);
      onClose();
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!recipe) return;
    try {
      await onDelete(recipe.id);
      onClose();
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to delete");
    } finally {
      setShowDelete(false);
    }
  };

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !recipe) return;
    setUploading(true);
    try {
      const text = await file.text();
      setContent(text);
    } catch {
      onError("Failed to read file");
    } finally {
      setUploading(false);
      e.target.value = "";
    }
  };

  if (!open || !recipe) return null;

  return (
    <>
      <div className="fixed inset-0 z-50 flex justify-end" onClick={onClose}>
        <div
          ref={drawerRef}
          tabIndex={-1}
          className="h-full w-full max-w-2xl bg-surface border-l border-border shadow-xl flex flex-col overflow-hidden outline-none"
          onClick={(e) => e.stopPropagation()}
        >
          {/* Header */}
          <div className="sticky top-0 bg-surface border-b border-border px-6 py-4 flex items-center justify-between shrink-0">
            <div>
              <h3 className="text-lg font-bold">{recipe.name}</h3>
              <p className="text-xs text-text-muted mt-0.5 font-mono">{recipe.filename}</p>
            </div>
            <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-surface-hover transition-colors">
              <X size={18} />
            </button>
          </div>

          {/* Content */}
          <div className="flex-1 overflow-y-auto p-6">
            <div className="flex items-center justify-between mb-3">
              <label className="text-sm font-medium">Recipe YAML</label>
              <div className="flex items-center gap-2">
                <input
                  type="file"
                  accept=".yaml,.yml"
                  ref={fileInputRef}
                  onChange={handleFileUpload}
                  className="hidden"
                />
                <button
                  onClick={() => fileInputRef.current?.click()}
                  disabled={uploading}
                  className="px-3 py-1.5 rounded-lg border border-border hover:border-primary/50 text-sm font-medium transition-colors flex items-center gap-1.5 disabled:opacity-50"
                >
                  <Upload size={14} />
                  Upload
                </button>
              </div>
            </div>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              className="w-full h-[400px] px-4 py-3 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm resize-y"
              spellCheck={false}
              placeholder="name: My Recipe&#10;model: ..."
              disabled={loadingContent}
            />
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between px-6 py-4 border-t border-border bg-surface shrink-0">
            <button
              onClick={() => setShowDelete(true)}
              className="px-3 py-2 rounded-lg border border-border hover:border-danger text-sm font-medium text-danger transition-colors flex items-center gap-1.5"
            >
              <Trash2 size={14} />
              Delete
            </button>
            <div className="flex items-center gap-3">
              <button onClick={onClose} className="px-4 py-2 rounded-lg border border-border hover:bg-surface-hover text-sm font-medium transition-colors">
                Cancel
              </button>
              <button
                onClick={handleSave}
                disabled={saving || !content.trim() || loadingContent}
                className="px-4 py-2 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50 text-white font-medium text-sm transition-colors flex items-center gap-1.5"
              >
                <Save size={14} />
                {saving ? "Saving..." : "Save"}
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Confirm delete modal */}
      {showDelete && recipe && (
        <ConfirmModal
          open={showDelete}
          onClose={() => setShowDelete(false)}
          onConfirm={handleDelete}
          title="Delete Recipe"
          message={`Delete "${recipe.name}"? This cannot be undone.`}
          confirmLabel="Delete"
          confirmVariant="danger"
        />
      )}
    </>
  );
}
