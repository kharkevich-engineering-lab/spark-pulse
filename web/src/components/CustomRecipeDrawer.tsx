/** Drawer for editing a custom recipe's YAML content — uses SlideDrawer. */

import { useState, useEffect, useRef, useCallback } from "react";
import { useI18n } from "@/lib/i18n";
import { Save, Upload, Trash2, X } from "lucide-react";
import { Button, ConfirmModal, IconButton } from "@/ui";
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
  const { t } = useI18n();
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
        if (!cancelled) onErrorRef.current(e instanceof Error ? e.message : t("customFiles.loadFailed"));
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
    catch (e) { onError(e instanceof Error ? e.message : t("customFiles.saveFailed")); }
    finally { setSaving(false); }
  }, [recipe, content, onSave, onClose, onError]);

  const handleDeleteConfirm = useCallback(async () => {
    if (!recipe) return;
    try { await onDelete(recipe.id); onClose(); }
    catch (e) { onError(e instanceof Error ? e.message : t("customFiles.deleteFailed")); }
    finally { setShowDelete(false); }
  }, [recipe, onDelete, onClose, onError]);

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !recipe) return;
    if (file.size > 1024 * 1024) { onError(t("customFiles.tooLarge1MB")); return; }
    setUploading(true);
    try { setContent(await file.text()); }
    catch { onError(t("customFiles.readFailed")); }
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
          <Button size="sm" variant="danger" icon={Trash2} onClick={() => setShowDelete(true)}>
            {t("common.delete")}
          </Button>
          <input type="file" accept=".yaml,.yml" ref={fileInputRef} onChange={handleFileUpload} className="hidden" />
          <Button
            size="sm"
            icon={Upload}
            loading={uploading}
            onClick={() => fileInputRef.current?.click()}
          >
            {t("common.upload")}
          </Button>
          <Button
            size="sm"
            variant="primary"
            icon={Save}
            loading={saving}
            disabled={!content.trim() || loadingContent}
            onClick={handleSave}
          >
            {saving ? t("common.saving") : t("common.save")}
          </Button>
          <IconButton size="sm" icon={X} label={t("common.close")} onClick={onClose} className="border-transparent text-muted hover:text-text hover:border-line" />
        </>
      }
    >
      <div className="px-6 py-5 flex flex-col min-h-0">
        <label className="block text-sm font-medium mb-1">{t("customFiles.recipeYaml")}</label>
        <LazyCodeEditor
          value={content}
          language="yaml"
          onChange={(evn: React.ChangeEvent<HTMLTextAreaElement>) => setContent(evn.target.value)}
          placeholder={t("customFiles.yamlPlaceholder")}
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
              title={t("customFiles.deleteRecipe")}
              message={t("customFiles.deleteBody", { name: recipe.name })}
              confirmLabel={t("common.delete")}
              confirmVariant="danger"
            />
          </div>
        </div>
      )}
    </SlideDrawer>
  );
}
