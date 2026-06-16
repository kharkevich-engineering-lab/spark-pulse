/** Drawer for editing a custom recipe's YAML content — uses SlideDrawer. */

import { useState, useEffect, useRef, useCallback } from "react";
import { Save, Upload, Trash2, X } from "lucide-react";
import { ConfirmModal } from "@/components/Modal";
import type { CustomRecipeInfo } from "@/lib/types";
import { getCustomRecipeContent } from "@/lib/api";
import LazyCodeEditor from "./LazyCodeEditor";
import SlideDrawer from "./SlideDrawer";

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
  const onErrorRef = useRef(onError);

  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  // Load content when drawer opens or recipe changes.
  useEffect(() => {
    if (!open || !recipe) {
      setContent("");
      setLoadingContent(false);
      return;
    }

    let cancelled = false;
    setLoadingContent(true);
    void (async () => {
      try {
        const data = await getCustomRecipeContent(recipe.id);
        if (!cancelled) setContent(data.content);
      } catch (e) {
        if (!cancelled) onErrorRef.current(e instanceof Error ? e.message : "Failed to load recipe");
      } finally {
        if (!cancelled) setLoadingContent(false);
      }
    })();
    return () => { cancelled = true; };
  }, [open, recipe?.id]);

  const handleSave = useCallback(async () => {
    if (!recipe || !content.trim()) return;
    setSaving(true);
    try { await onSave(recipe.id, content); onClose(); }
    catch (e) { onError(e instanceof Error ? e.message : "Failed to save"); }
    finally { setSaving(false); }
  }, [recipe, content, onSave, onClose, onError]);

  const handleDeleteConfirm = useCallback(async () => {
    if (!recipe) return;
    try { await onDelete(recipe.id); onClose(); }
    catch (e) { onError(e instanceof Error ? e.message : "Failed to delete"); }
    finally { setShowDelete(false); }
  }, [recipe, onDelete, onClose, onError]);

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !recipe) return;
    if (file.size > 1024 * 1024) { onError("File too large (max 1MB)"); return; }
    setUploading(true);
    try { setContent(await file.text()); }
    catch { onError("Failed to read file"); }
    finally { setUploading(false); e.target.value = ""; }
  };

  if (!open || !recipe) return null;

  return (
    <SlideDrawer
      open={open}
      onClose={onClose}
      header={
        <div>
          <h3 className="text-xl font-bold truncate">{recipe.name}</h3>
          <p className="text-sm text-text-muted mt-1 font-mono">{recipe.filename}</p>
        </div>
      }
      actions={
        <>
          <button type="button" onClick={() => setShowDelete(true)}
            className="px-3 py-1.5 rounded-lg border border-border hover:border-danger text-sm font-medium transition-colors text-danger flex items-center gap-1.5">
            <Trash2 size={14} /> Delete
          </button>
          <input type="file" accept=".yaml,.yml" ref={fileInputRef} onChange={handleFileUpload} className="hidden" />
          <button type="button" onClick={() => fileInputRef.current?.click()} disabled={uploading}
            className="px-3 py-1.5 rounded-lg border border-border hover:border-primary/50 text-sm font-medium transition-colors flex items-center gap-1.5 disabled:opacity-50">
            <Upload size={14} /> Upload
          </button>
          <button type="button" onClick={handleSave} disabled={saving || !content.trim() || loadingContent}
            className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50 text-white font-medium text-sm transition-colors flex items-center gap-1.5">
            <Save size={14} />
            {saving ? "Saving..." : "Save"}
          </button>
          <button type="button" onClick={onClose} className="p-1.5 rounded-lg hover:bg-surface-hover transition-colors">
            <X size={18} />
          </button>
        </>
      }
    >
      <div className="px-6 py-5 flex flex-col min-h-0">
        <label className="block text-sm font-medium mb-1">Recipe YAML</label>
        <LazyCodeEditor
          value={content}
          language="yaml"
          onChange={(evn: React.ChangeEvent<HTMLTextAreaElement>) => setContent(evn.target.value)}
          placeholder="name: My Recipe&#10;model: ..."
          padding={16}
          disabled={loadingContent}
          className="flex-1 min-h-[300px] font-mono text-sm"
          spellCheck={false}
        />
      </div>

      {/* Delete confirmation modal */}
      {showDelete && recipe && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/30">
          <div className="pointer-events-auto">
            <ConfirmModal
              open={showDelete}
              onClose={() => setShowDelete(false)}
              onConfirm={handleDeleteConfirm}
              title="Delete Recipe"
              message={`Delete "${recipe.name}"? This cannot be undone.`}
              confirmLabel="Delete"
              confirmVariant="danger"
            />
          </div>
        </div>
      )}
    </SlideDrawer>
  );
}
