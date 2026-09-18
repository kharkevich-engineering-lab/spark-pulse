/** Modal for creating or uploading a custom mod. */

import { useState, useCallback } from "react";
import { useI18n } from "@/lib/i18n";
import { Save, X } from "lucide-react";
import { AlertModal, Button, Field, IconButton, Input, Modal, Textarea } from "@/ui";

interface ModFile {
  name: string;
  content: string;
}

export default function NewModModal({
  open,
  onClose,
  onSave,
  onError,
}: {
  open: boolean;
  onClose: () => void;
  onSave: (id: string, name: string) => Promise<void>;
  onError: (msg: string) => void;
}) {
  const { t } = useI18n();
  const [modName, setModName] = useState("");
  const [files, setFiles] = useState<ModFile[]>([{ name: "run.sh", content: "#!/bin/bash\necho 'Mod: ${modName}'" }]);
  const [saving, setSaving] = useState(false);
  const [errorModal, setErrorModal] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const handleZipUpload = useCallback(async (file: File) => {
    if (file.size > 10 * 1024 * 1024) { onError("ZIP file too large (max 10MB)"); return; }
    try {
      const name = file.name.replace(/\.zip$/i, "") || "uploaded-mod";
      const formData = new FormData();
      formData.append("zip_file", file);
      formData.append("name", name);

      const resp = await fetch("/api/custom-files/mods/upload", {
        method: "POST",
        body: formData,
        credentials: "include",
      });

      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        throw new Error(errData.detail || "Failed to upload mod");
      }

      const data = await resp.json();
      await onSave(data.id, data.name);
      onClose();
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to save mod");
    }
  }, [onSave, onError, onClose]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file && file.name.endsWith(".zip")) {
        void handleZipUpload(file);
    } else {
      setErrorModal("Please upload a .zip file");
    }
  }, [handleZipUpload]);

  const addFile = () => {
    setFiles(prev => [...prev, { name: "", content: "" }]);
  };

  const removeFile = (index: number) => {
    setFiles(prev => prev.filter((_, i) => i !== index));
  };

  const updateFile = (index: number, field: "name" | "content", value: string) => {
    setFiles(prev => {
      const next = [...prev];
      next[index] = { ...next[index], [field]: value };
      return next;
    });
  };

  const handleSave = async () => {
    if (!modName.trim()) {
      setErrorModal("Mod name is required");
      return;
    }
    const runSh = files.find(f => f.name === "run.sh");
    if (!runSh || !runSh.content.trim()) {
      setErrorModal("run.sh is required and cannot be empty");
      return;
    }
    setSaving(true);
    try {
      const fileMap: Record<string, string> = {};
      for (const f of files) {
        if (f.name && f.content) {
          fileMap[f.name] = f.content;
        }
      }

      const resp = await fetch(`/api/custom-files/mods/custom/${modName.trim().toLowerCase().replace(/\s+/g, "-")}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(fileMap),
        credentials: "include",
      });

      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        setErrorModal(errData.detail || "Failed to save mod");
        return;
      }

      await onSave(`custom/${modName.trim().toLowerCase().replace(/\s+/g, "-")}`, modName.trim());
      onClose();
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    setFiles([{ name: "run.sh", content: "#!/bin/bash\necho 'Mod' " }]);
    setModName("");
    onClose();
  };

  if (!open) return null;

  return (
    <>
      <Modal
        open
        onClose={handleCancel}
        size="lg"
        title={t("newMod.title")}
        actions={
          <>
            <Button size="sm" onClick={handleCancel}>
              Cancel
            </Button>
            <Button
              size="sm"
              variant="primary"
              icon={Save}
              loading={saving}
              disabled={!modName.trim()}
              onClick={handleSave}
            >
              {saving ? "Saving..." : "Create Mod"}
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          <Field label={t("newMod.name")}>
            {(control) => (
              <Input
                {...control}
                mono
                type="text"
                value={modName}
                onChange={(e) => setModName(e.target.value)}
                placeholder={t("newMod.namePlaceholder")}
              />
            )}
          </Field>

          {/* Upload ZIP zone */}
          <div>
            <p className="block text-[13px] font-medium mb-1.5">{t("newMod.uploadZip")}</p>
            <div
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
              className="border border-dashed border-line rounded-sm p-6 text-center cursor-pointer transition-colors text-[14px]"
            >
              <span className={dragOver ? "text-blue2" : "text-muted"}>Drag &amp; drop a ZIP here</span>
            </div>
          </div>

          {/* Divider */}
          <div className="flex items-center gap-3">
            <div className="flex-1 h-px bg-line" />
            <span className="text-[13px] text-muted">or create manually</span>
            <div className="flex-1 h-px bg-line" />
          </div>

          {/* Files */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <p className="text-[13px] font-medium">{t("newMod.files")}</p>
              <Button size="sm" onClick={addFile}>
                Add file
              </Button>
            </div>
            <div className="space-y-3">
              {files.map((f, i) => (
                <div key={i} className="p-3 rounded-sm bg-bg border border-line space-y-2">
                  <div className="flex items-center gap-2">
                    <Input
                      mono
                      type="text"
                      aria-label={t("newMod.filenamePlaceholder")}
                      value={f.name}
                      onChange={(e) => updateFile(i, "name", e.target.value)}
                      placeholder={t("newMod.filenamePlaceholder")}
                      className="flex-1"
                    />
                    <IconButton
                      size="sm"
                      variant="danger"
                      icon={X}
                      label={`Remove ${f.name || "file"}`}
                      onClick={() => removeFile(i)}
                    />
                  </div>
                  <Textarea
                    mono
                    aria-label={`Content of ${f.name || "file"}`}
                    value={f.content}
                    onChange={(e) => updateFile(i, "content", e.target.value)}
                    placeholder={`// Content of ${f.name || "file"}...`}
                    rows={4}
                  />
                </div>
              ))}
            </div>
          </div>
        </div>
      </Modal>

      {/* Error modal */}
      {errorModal && (
        <AlertModal
          open={!!errorModal}
          onClose={() => setErrorModal(null)}
          title={t("common.error")}
          message={errorModal}
        />
      )}
    </>
  );
}
