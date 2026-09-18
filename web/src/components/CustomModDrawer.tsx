/** Drawer for editing a custom mod's files — uses SlideDrawer. */

import { useState, useEffect, useCallback } from "react";
import { useI18n } from "@/lib/i18n";
import { Save, Trash2, X } from "lucide-react";
import { Button, ConfirmModal, IconButton } from "@/ui";
import type { CustomModInfo, ModFileMap } from "@/lib/types";
import SlideDrawer from "./SlideDrawer";

export default function CustomModDrawer({
  open,
  mod,
  files,
  onClose,
  onSave,
  onDelete,
  onError,
}: {
  open: boolean;
  mod: CustomModInfo | null;
  files: ModFileMap;
  onClose: () => void;
  onSave: (modId: string, fileMap: ModFileMap) => Promise<void>;
  onDelete: (modId: string) => Promise<void>;
  onError: (msg: string) => void;
}) {
  const { t } = useI18n();
  const [fileMap, setFileMap] = useState<ModFileMap>({});
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [showDelete, setShowDelete] = useState(false);

  useEffect(() => {
    if (open && mod) {
      setFileMap({ ...files });
      const defaultFile = files["run.sh"] ? "run.sh" : Object.keys(files)[0] || null;
      setSelectedFile(defaultFile);
    }
  }, [open, mod, files]);

  const handleSave = useCallback(async () => {
    if (!mod) return;
    setSaving(true);
    try { await onSave(mod.id, { ...fileMap }); onClose(); }
    catch (e) { onError(e instanceof Error ? e.message : t("customFiles.saveFailed")); }
    finally { setSaving(false); }
  }, [mod, fileMap, onSave, onClose, onError]);

  const handleDeleteConfirm = useCallback(async () => {
    if (!mod) return;
    try { await onDelete(mod.id); onClose(); }
    catch (e) { onError(e instanceof Error ? e.message : t("customFiles.deleteFailed")); }
    finally { setShowDelete(false); }
  }, [mod, onDelete, onClose, onError]);

  const handleFileChange = (newVal: string) => {
    if (selectedFile) setFileMap((prev) => ({ ...prev, [selectedFile]: newVal }));
  };

  if (!open || !mod) return null;

  const allFiles = Object.keys(fileMap).sort();

  return (
    <SlideDrawer
      open={open}
      onClose={onClose}
      header={
        <div>
          <h3 className="text-xl font-bold truncate">{mod.name}</h3>
          {mod.description && <p className="text-sm text-text-muted mt-1 truncate">{mod.description}</p>}
        </div>
      }
      actions={
        <>
          <Button size="sm" variant="danger" icon={Trash2} onClick={() => setShowDelete(true)}>
            {t("common.delete")}
          </Button>
          <Button size="sm" variant="primary" icon={Save} loading={saving} onClick={handleSave}>
            {saving ? t("common.saving") : t("common.save")}
          </Button>
          <IconButton size="sm" icon={X} label={t("common.close")} onClick={onClose} className="border-transparent text-muted hover:text-text hover:border-line" />
        </>
      }
    >
      <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
        <div className="flex flex-1 min-h-0">
          {/* File list sidebar */}
          <div className="w-44 border-r border-border p-4 overflow-auto shrink-0">
            <h4 className="text-xs font-medium text-text-muted mb-2 uppercase">{t("customFiles.files")}</h4>
            {allFiles.length > 0 ? (
              <ul className="space-y-1">
                {allFiles.map((f) => (
                  <li key={f}>
                    <button onClick={() => setSelectedFile(f)}
                      className={`w-full text-left px-2 py-1 rounded text-xs font-mono transition-colors truncate ${
                        selectedFile === f ? "bg-primary/15 text-blue2" : "hover:bg-surface-hover text-text-muted"
                      }`}>
                      {f}
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-xs text-text-muted italic">{t("customFiles.noFiles")}</p>
            )}
          </div>

          {/* File editor */}
          <div className="flex-1 flex flex-col min-w-0 p-4">
            <div className="flex-1 min-h-0">
              {selectedFile ? (
                <textarea
                  value={fileMap[selectedFile] || ""}
                  onChange={(e) => handleFileChange(e.target.value)}
                  className="w-full h-full px-4 py-3 rounded-sm bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm resize-none"
                  spellCheck={false}
                  placeholder={t("customFiles.scriptPlaceholder")}
                />
              ) : (
                <div className="flex items-center justify-center h-full text-muted text-[14px]">
                  {t("customFiles.selectFile")}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Delete confirmation modal */}
      {showDelete && mod && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-black/30">
          <div className="pointer-events-auto">
            <ConfirmModal
              open={showDelete}
              onClose={() => setShowDelete(false)}
              onConfirm={handleDeleteConfirm}
              title={t("customFiles.deleteMod")}
              message={t("customFiles.deleteBody", { name: mod.name })}
              confirmLabel={t("common.delete")}
              confirmVariant="danger"
            />
          </div>
        </div>
      )}
    </SlideDrawer>
  );
}
