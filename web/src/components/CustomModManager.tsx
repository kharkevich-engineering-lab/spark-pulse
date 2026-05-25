/** Modal for editing a custom mod's files. */

import { useState, useEffect } from "react";
import { Save, Trash2, X } from "lucide-react";
import { ConfirmModal } from "@/components/Modal";
import type { CustomModInfo, ModFileMap } from "@/lib/types";

export default function CustomModManager({
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

  const handleSave = async () => {
    if (!mod) return;
    setSaving(true);
    try {
      await onSave(mod.id, { ...fileMap });
      onClose();
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!mod) return;
    try {
      await onDelete(mod.id);
      onClose();
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to delete");
    } finally {
      setShowDelete(false);
    }
  };

  const handleFileChange = (newVal: string) => {
    if (selectedFile) {
      setFileMap((prev) => ({ ...prev, [selectedFile]: newVal }));
    }
  };

  if (!open || !mod) return null;

  const allFiles = Object.keys(fileMap).sort();

  return (
    <>
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div className="absolute inset-0 bg-black/60" onClick={onClose} />
        <div className="relative w-full max-w-4xl max-h-[85vh] overflow-auto rounded-xl bg-surface border border-border shadow-2xl">
          {/* Header */}
          <div className="sticky top-0 bg-surface border-b border-border px-6 py-4 flex items-center justify-between">
            <div>
              <h3 className="text-lg font-bold">{mod.name}</h3>
              {mod.description && <p className="text-xs text-text-muted mt-0.5">{mod.description}</p>}
            </div>
            <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-surface-hover transition-colors">
              <X size={18} />
            </button>
          </div>

          {/* Content */}
          <div className="flex h-[500px]">
            {/* File list sidebar */}
            <div className="w-48 border-r border-border p-4 overflow-auto">
              <h4 className="text-xs font-medium text-text-muted mb-2 uppercase">Files</h4>
              {allFiles.length > 0 ? (
                <ul className="space-y-1">
                  {allFiles.map((f) => (
                    <li key={f}>
                      <button
                        onClick={() => setSelectedFile(f)}
                        className={`w-full text-left px-2 py-1 rounded text-xs font-mono transition-colors truncate ${
                          selectedFile === f
                            ? "bg-primary/15 text-primary"
                            : "hover:bg-surface-hover text-text-muted"
                        }`}
                      >
                        {f}
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-text-muted italic">No files</p>
              )}
            </div>

            {/* File editor */}
            <div className="flex-1 flex flex-col">
              <div className="flex-1 p-4">
                {selectedFile ? (
                  <textarea
                    value={fileMap[selectedFile] || ""}
                    onChange={(e) => handleFileChange(e.target.value)}
                    className="w-full h-full px-4 py-3 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm resize-none"
                    spellCheck={false}
                    placeholder="#!/bin/bash&#10;..."
                  />
                ) : (
                  <div className="flex items-center justify-center h-full text-text-muted text-sm">
                    Select a file to edit
                  </div>
                )}
              </div>
            </div>
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
                disabled={saving}
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
      {showDelete && mod && (
        <ConfirmModal
          open={showDelete}
          onClose={() => setShowDelete(false)}
          onConfirm={handleDelete}
          title="Delete Mod"
          message={`Delete "${mod.name}"? This cannot be undone.`}
          confirmLabel="Delete"
          confirmVariant="danger"
        />
      )}
    </>
  );
}
