/** Modal for editing a custom recipe's YAML content. */

import { useState, useEffect, useRef } from "react";
import { Save, Upload, Trash2, X, Loader2 } from "lucide-react";
import { ConfirmModal } from "@/components/Modal";
import type { CustomRecipeInfo } from "@/lib/types";
import { getCustomRecipeContent } from "@/lib/api";

export default function CustomRecipeModal({
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
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Load content when modal opens or recipe changes
  useEffect(() => {
    if (open && recipe) {
      loadContent();
    } else {
      setContent("");
    }
  }, [open, recipe]);

  const loadContent = async () => {
    if (!recipe) return;
    setLoadingContent(true);
    try {
      const data = await getCustomRecipeContent(recipe.id);
      setContent(data.content);
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to load recipe");
    } finally {
      setLoadingContent(false);
    }
  };

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
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div className="absolute inset-0 bg-black/60" onClick={onClose} />
        <div className="relative w-full max-w-4xl max-h-[90vh] overflow-auto rounded-xl bg-surface border border-border shadow-2xl">
          {/* Header */}
          <div className="sticky top-0 bg-surface border-b border-border px-6 py-4 flex items-center justify-between">
            <div>
              <h3 className="text-lg font-bold">{recipe.name}</h3>
              <p className="text-xs text-text-muted mt-0.5 font-mono">{recipe.filename}</p>
            </div>
            <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-surface-hover transition-colors">
              <X size={18} />
            </button>
          </div>

          {/* Content */}
          <div className="p-6">
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
              ref={textareaRef}
              value={content}
              onChange={(e) => setContent(e.target.value)}
              className="w-full h-[400px] px-4 py-3 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm resize-y"
              spellCheck={false}
              placeholder="name: My Recipe&#10;model: ..."
              disabled={loadingContent}
            />
            {loadingContent && (
              <div className="absolute inset-0 flex items-center justify-center bg-surface/80">
                <Loader2 className="animate-spin text-primary" size={24} />
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between px-6 py-4 border-t border-border bg-surface sticky bottom-0">
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
