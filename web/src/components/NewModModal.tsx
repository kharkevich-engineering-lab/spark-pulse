/** Modal for creating or uploading a custom mod. */

import { useState, useCallback } from "react";
import { Save, X } from "lucide-react";
import { AlertModal } from "@/components/Modal";

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
  const [modName, setModName] = useState("");
  const [files, setFiles] = useState<ModFile[]>([{ name: "run.sh", content: "#!/bin/bash\necho 'Mod: ${modName}'" }]);
  const [saving, setSaving] = useState(false);
  const [errorModal, setErrorModal] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const handleZipUpload = useCallback(async (file: File) => {
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
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div className="absolute inset-0 bg-black/60" onClick={handleCancel} />
        <div className="relative w-full max-w-3xl max-h-[90vh] overflow-auto rounded-xl bg-surface border border-border shadow-2xl">
          {/* Header */}
          <div className="sticky top-0 bg-surface border-b border-border px-6 py-4 flex items-center justify-between">
            <h3 className="text-lg font-bold">New Mod</h3>
            <button onClick={handleCancel} className="p-1.5 rounded-lg hover:bg-surface-hover transition-colors">
              <X size={18} />
            </button>
          </div>

          {/* Body */}
          <div className="p-6 space-y-4">
            {/* Name */}
            <div>
              <label className="block text-sm font-medium mb-1">Mod Name</label>
              <input
                type="text"
                value={modName}
                onChange={(e) => setModName(e.target.value)}
                placeholder="my-mod"
                className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm"
              />
            </div>

            {/* Upload ZIP zone */}
            <div>
              <label className="block text-sm font-medium mb-1">Or upload ZIP</label>
              <div
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
                className="border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors text-sm"
              >
                <span className={dragOver ? "text-primary" : "text-text-muted"}>
                  Drag & drop a ZIP here
                </span>
              </div>
            </div>

            {/* Divider */}
            <div className="flex items-center gap-3">
              <div className="flex-1 h-px bg-border" />
              <span className="text-xs text-text-muted">or create manually</span>
              <div className="flex-1 h-px bg-border" />
            </div>

            {/* Files */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <label className="text-sm font-medium">Files</label>
                <button onClick={addFile} className="text-xs px-2 py-1 rounded bg-primary/20 text-primary hover:bg-primary/30">
                  + Add File
                </button>
              </div>
              <div className="space-y-3">
                {files.map((f, i) => (
                  <div key={i} className="p-3 rounded-lg bg-bg border border-border space-y-2">
                    <div className="flex items-center gap-2">
                      <input
                        type="text"
                        value={f.name}
                        onChange={(e) => updateFile(i, "name", e.target.value)}
                        placeholder="filename"
                        className="flex-1 px-2 py-1 rounded border border-border focus:border-primary focus:outline-none font-mono text-xs"
                      />
                      <button
                        onClick={() => removeFile(i)}
                        className="p-1 rounded hover:bg-danger/10 text-danger"
                      >
                        <X size={12} />
                      </button>
                    </div>
                    <textarea
                      value={f.content}
                      onChange={(e) => updateFile(i, "content", e.target.value)}
                      placeholder={`// Content of ${f.name || "file"}...`}
                      rows={4}
                      className="w-full px-2 py-1 rounded border border-border focus:border-primary focus:outline-none font-mono text-xs resize-y"
                    />
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between px-6 py-4 border-t border-border bg-surface sticky bottom-0">
            <button
              onClick={handleCancel}
              className="px-4 py-2 rounded-lg border border-border hover:bg-surface-hover text-sm font-medium transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={saving || !modName.trim()}
              className="px-4 py-2 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50 text-white font-medium text-sm transition-colors flex items-center gap-1.5"
            >
              <Save size={14} />
              {saving ? "Saving..." : "Create Mod"}
            </button>
          </div>
        </div>
      </div>

      {/* Error modal */}
      {errorModal && (
        <AlertModal
          open={!!errorModal}
          onClose={() => setErrorModal(null)}
          title="Error"
          message={errorModal}
        />
      )}
    </>
  );
}
