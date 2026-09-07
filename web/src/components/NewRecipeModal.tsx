/** Modal for uploading or manually creating a custom recipe. */

import { useState, useCallback, useRef } from "react";
import { Save, Upload, X, AlertCircle, FileCode } from "lucide-react";
import { AlertModal } from "@/components/Modal";

type EntryMode = "upload" | "manual";

interface ValidationIssue {
  field: string;
  message: string;
}

/** The recipe starters, one per format.
 *
 * v2 is the default because it is the format the engines are described in —
 * v1 puts the whole launch into one vLLM-specific command template, and a
 * recipe written that way can only ever run on vLLM. v1 stays offered because
 * it stays valid forever and there are plenty of them about.
 */
const TEMPLATES: Record<"2" | "1", string> = {
  "2": `recipe_version: "2"

# What this recipe is called in the list.
name: My Custom Recipe

# The model to serve. It has to be in the local catalogue, or the deploy will
# offer to download it.
model: org/model-name

# Which engine runs it, and the per-engine flags.
engine: vllm
params:
  port: 8000
  tensor_parallel: 1
engines:
  vllm:
    args: --enable-prefix-caching
`,
  "1": `# The original format: one vLLM command template, filled in from defaults.
name: My Custom Recipe
model: org/model-name
container: vllm-node
command: vllm serve org/model-name --port {port}
defaults:
  port: 8000
`,
};

/** Unpack whatever the validator said into per-field issues.
 *
 * It answers `{message, errors: [{path, message}]}` — the point of validating
 * before saving is to be told *where* to look, not to be handed one sentence.
 * A plain-string detail is still handled: not every failure on this path comes
 * from the schema.
 */
function readValidationErrors(payload: unknown): ValidationIssue[] {
  const detail = (payload as { detail?: unknown })?.detail;
  if (typeof detail === "string") return [{ field: "yaml", message: detail }];
  if (detail && typeof detail === "object") {
    const errors = (detail as { errors?: unknown }).errors;
    if (Array.isArray(errors) && errors.length) {
      return errors.map((e) => {
        const { path, message } = e as { path?: string; message?: string };
        return {
          field: path || "yaml",
          message: path ? `${path}: ${message ?? ""}` : (message ?? "invalid"),
        };
      });
    }
    const message = (detail as { message?: string }).message;
    if (message) return [{ field: "yaml", message }];
  }
  return [{ field: "yaml", message: "Validation failed" }];
}

export default function NewRecipeModal({
  open,
  onClose,
  onSave,
  onError,
}: {
  open: boolean;
  onClose: () => void;
  onSave: (id: string, name: string, content: string) => Promise<void>;
  onError: (msg: string) => void;
}) {
  const [mode, setMode] = useState<EntryMode>("upload");
  const [content, setContent] = useState("");
  const [filename, setFilename] = useState("");
  const [validationErrors, setValidationErrors] = useState<ValidationIssue[]>([]);
  const [recipeName, setRecipeName] = useState("");
  const [saving, setSaving] = useState(false);
  const [errorModal, setErrorModal] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [step, setStep] = useState<"upload" | "preview">("upload");
  const [format, setFormat] = useState<"2" | "1">("2");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const processFile = useCallback(async (file: File) => {
    if (file.size > 1024 * 1024) { onError("File too large (max 1MB)"); return; }
    setFilename(file.name);
    setValidationErrors([]);
    setContent("");

    try {
      const text = await file.text();
      setContent(text);

      // Validate YAML via backend
      const validateResp = await fetch("/api/custom-files/recipes/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: text }),
        credentials: "include",
      });

      if (!validateResp.ok) {
        setValidationErrors(readValidationErrors(await validateResp.json().catch(() => ({}))));
        return;
      }

      // Extract name from YAML
      const nameMatch = text.match(/^name:\s*(.+)$/m);
      setRecipeName((nameMatch?.[1].trim()) || file.name.replace(/\.(yaml|yml)$/, ""));
      setContent(text);
      setStep("preview");
    } catch {
      onError("Failed to read file");
    }
  }, [onError]);

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
    if (file && (file.name.endsWith(".yaml") || file.name.endsWith(".yml"))) {
      processFile(file);
    } else {
      setValidationErrors([{ field: "file", message: "Please upload a .yaml or .yml file" }]);
    }
  }, [processFile]);

  const handleFileSelect = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) processFile(file);
    e.target.value = "";
  }, [processFile]);

  const handleSwitchToManual = () => {
    setMode("manual");
    setStep("upload");
    setFilename("");
    setValidationErrors([]);
    setContent(TEMPLATES[format]);
    setRecipeName("My Custom Recipe");
  };

  const handleSaveManual = async () => {
    setContent(content);
    // Validate via backend
    setStep("preview");
    setValidationErrors([]);

    try {
      const validateResp = await fetch("/api/custom-files/recipes/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
        credentials: "include",
      });

      if (!validateResp.ok) {
        setValidationErrors(readValidationErrors(await validateResp.json().catch(() => ({}))));
        return;
      }

      // The name comes from the parser rather than from a regex over the
      // source: quoting, an anchor, a folded scalar — the YAML is already
      // parsed on the other side, so asking it is both shorter and right.
      const parsed = await validateResp.json().catch(() => ({}));
      setRecipeName(String(parsed.name || recipeName));
    } catch (e) {
      setValidationErrors([{ field: "yaml", message: e instanceof Error ? e.message : "Validation failed" }]);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const slug = recipeName.trim().toLowerCase().replace(/\s+/g, "-");
      const recipeId = `custom/${slug}`;

      // The write itself only requires the YAML to parse. That is deliberate
      // on the backend's side — listing is lenient too, so a half-written
      // recipe an operator means to come back to still appears — which is why
      // "Validate recipe" above is the step that actually checks the schema.
      const saveResp = await fetch(`/api/custom-files/recipes/${recipeId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
        credentials: "include",
      });

      if (!saveResp.ok) {
        const issues = readValidationErrors(await saveResp.json().catch(() => ({})));
        setErrorModal(issues.map((i) => i.message).join("\n"));
        return;
      }

      await onSave(recipeId, recipeName, content);
      onClose();
    } catch (e) {
      onError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    if (step === "preview") {
      if (mode === "manual") {
        setStep("upload");
        setValidationErrors([]);
      } else {
        onClose();
      }
    } else {
      setContent("");
      setFilename("");
      setValidationErrors([]);
      setStep("upload");
      setMode("upload");
      onClose();
    }
  };

  if (!open) return null;

  return (
    <>
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div className="absolute inset-0 bg-black/60" onClick={handleCancel} />
        <div className="relative w-full max-w-3xl max-h-[90vh] overflow-auto rounded-xl bg-surface border border-border shadow-2xl">
          {/* Header */}
          <div className="sticky top-0 bg-surface border-b border-border px-6 py-4 flex items-center justify-between">
            <h3 className="text-lg font-bold">
              {step === "upload"
                ? (mode === "upload" ? "Upload Recipe" : "Manual Recipe")
                : "Preview Recipe"}
            </h3>
            <button onClick={handleCancel} className="p-1.5 rounded-lg hover:bg-surface-hover transition-colors">
              <X size={18} />
            </button>
          </div>

          {/* Body */}
          {step === "upload" ? (
            <div className="p-6">
              {/* Mode toggle */}
              <div className="flex items-center gap-2 mb-6 p-1 rounded-lg bg-bg border border-border w-fit">
                <button
                  onClick={() => { setMode("upload"); setStep("upload"); setFilename(""); }}
                  className={`px-3 py-1.5 text-sm font-medium rounded transition-colors flex items-center gap-1.5 ${
                    mode === "upload" ? "bg-primary/10 text-primary" : "text-text-muted hover:text-text"
                  }`}
                >
                  <Upload size={14} />
                  Upload
                </button>
                <button
                  onClick={() => { setMode("manual"); setStep("upload"); }}
                  className={`px-3 py-1.5 text-sm font-medium rounded transition-colors flex items-center gap-1.5 ${
                    mode === "manual" ? "bg-primary/10 text-primary" : "text-text-muted hover:text-text"
                  }`}
                >
                  <FileCode size={14} />
                  Manual
                </button>
              </div>

              {mode === "upload" && (
                <>
                  {/* Upload Zone */}
                  <div
                    onDragOver={handleDragOver}
                    onDragLeave={handleDragLeave}
                    onDrop={handleDrop}
                    className={`border-2 border-dashed rounded-xl p-12 text-center transition-colors ${
                      dragOver
                        ? "border-primary bg-primary/5"
                        : "border-border hover:border-primary/50 hover:bg-surface-hover"
                    }`}
                  >
                    <Upload size={32} className="mx-auto text-text-muted mb-4" />
                    <p className="text-sm font-medium text-text mb-1">
                      Drag and drop a YAML file here, or{" "}
                      <span
                        className="text-primary underline cursor-pointer"
                        onClick={(e) => {
                          e.stopPropagation();
                          fileInputRef.current?.click();
                        }}
                      >
                        browse
                      </span>
                    </p>
                    <p className="text-xs text-text-muted mt-1">.yaml or .yml files supported</p>
                  </div>
                  {/* Hidden file input — only triggers when user clicks "browse" text above */}
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept=".yaml,.yml"
                    onChange={handleFileSelect}
                    className="hidden"
                  />

                  {/* Or separator */}
                  <div className="flex items-center gap-3 my-6">
                    <div className="flex-1 h-px bg-border" />
                    <span className="text-xs text-text-muted">or</span>
                    <div className="flex-1 h-px bg-border" />
                  </div>

                  {/* Switch to manual */}
                  <div className="text-center">
                    <button
                      onClick={handleSwitchToManual}
                      className="text-sm text-primary hover:text-primary-hover underline"
                    >
                      Enter YAML manually
                    </button>
                  </div>

                  {/* Validation errors */}
                  {validationErrors.length > 0 && (
                    <div className="mt-4 p-4 rounded-lg bg-danger/10 border border-danger/30">
                      <div className="flex items-start gap-3">
                        <AlertCircle size={16} className="text-danger mt-0.5 shrink-0" />
                        <div>
                          <p className="text-sm font-medium text-danger">Validation failed</p>
                          <ul className="text-xs text-danger mt-1 space-y-0.5">
                            {validationErrors.map((err, i) => (
                              <li key={i}>{err.message}</li>
                            ))}
                          </ul>
                        </div>
                      </div>
                    </div>
                  )}
                </>
              )}
              {mode === "manual" && (
                <>
                  {/* Manual YAML editor */}
                  <div>
                    <div className="flex items-center justify-between mb-2 gap-3 flex-wrap">
                      <label className="text-sm font-medium" htmlFor="recipe-yaml">
                        Recipe YAML
                      </label>
                      <div className="flex items-center gap-2">
                        {/* Both formats validate; v2 is the one the engines
                            are described in. Switching only replaces an
                            untouched starter — nobody's edits are thrown away
                            by a click on a format button. */}
                        <div className="flex items-center rounded-lg border border-border overflow-hidden text-xs">
                          {(["2", "1"] as const).map((v) => (
                            <button
                              key={v}
                              type="button"
                              aria-pressed={format === v}
                              onClick={() => {
                                setFormat(v);
                                const untouched = Object.values(TEMPLATES).includes(content);
                                if (untouched) setContent(TEMPLATES[v]);
                              }}
                              className={`px-2.5 py-1 transition-colors ${
                                format === v
                                  ? "bg-primary/15 text-primary font-medium"
                                  : "text-text-muted hover:text-text"
                              }`}
                            >
                              v{v}
                            </button>
                          ))}
                        </div>
                        <button
                          onClick={handleSaveManual}
                          disabled={!content.trim()}
                          className="px-3 py-1 rounded-lg bg-primary/20 text-primary hover:bg-primary/30 text-sm font-medium transition-colors disabled:opacity-50"
                        >
                          Validate recipe
                        </button>
                      </div>
                    </div>
                    <textarea
                      id="recipe-yaml"
                      value={content}
                      onChange={(e) => {
                        setContent(e.target.value);
                        // A cheap read for the header while typing; the
                        // authoritative name comes from the parser on validate.
                        const nameMatch = e.target.value.match(/^name:\s*(.+)$/m);
                        if (nameMatch) setRecipeName(nameMatch[1].trim());
                      }}
                      className="w-full h-[400px] px-4 py-3 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm resize-y"
                      spellCheck={false}
                      placeholder={TEMPLATES["2"]}
                    />
                    <p className="text-xs text-text-muted mt-1.5">
                      Both recipe formats are accepted. Validation reports each problem against
                      the field it belongs to.
                    </p>
                  </div>
                </>
              )}
            </div>
          ) : (
            <div className="p-6">
              {/* Name */}
              <div className="mb-3">
                <label className="block text-sm font-medium mb-1">Recipe Name</label>
                <input
                  type="text"
                  value={recipeName}
                  onChange={(e) => setRecipeName(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm"
                />
              </div>
              {mode === "upload" && filename && (
                <div className="mb-3">
                  <span className="text-xs text-text-muted">Source: {filename}</span>
                </div>
              )}

              {/* Validation errors */}
              {validationErrors.length > 0 && (
                <div className="mb-4 p-4 rounded-lg bg-danger/10 border border-danger/30">
                  <div className="flex items-start gap-3">
                    <AlertCircle size={16} className="text-danger mt-0.5 shrink-0" />
                    <div>
                      <p className="text-sm font-medium text-danger">Validation failed</p>
                      <ul className="text-xs text-danger mt-1 space-y-0.5">
                        {validationErrors.map((err, i) => (
                          <li key={i}>{err.message}</li>
                        ))}
                      </ul>
                    </div>
                  </div>
                </div>
              )}

              {/* YAML Preview */}
              <div>
                <label className="block text-sm font-medium mb-1">YAML Content</label>
                <textarea
                  value={content}
                  readOnly
                  className="w-full h-[300px] px-4 py-3 rounded-lg bg-bg border border-border font-mono text-sm resize-y"
                  spellCheck={false}
                />
              </div>
            </div>
          )}

          {/* Footer */}
          <div className="flex items-center justify-between px-6 py-4 border-t border-border bg-surface sticky bottom-0">
            <button
              onClick={handleCancel}
              className="px-4 py-2 rounded-lg border border-border hover:bg-surface-hover text-sm font-medium transition-colors"
            >
              {step === "preview" ? "Back" : "Cancel"}
            </button>
            <div className="flex items-center gap-3">
              {step === "preview" && !validationErrors.length && (
                <button
                  onClick={handleSave}
                  disabled={saving || !recipeName.trim()}
                  className="px-4 py-2 rounded-lg bg-primary hover:bg-primary-hover disabled:opacity-50 text-white font-medium text-sm transition-colors flex items-center gap-1.5"
                >
                  <Save size={14} />
                  {saving ? "Saving..." : "Save Recipe"}
                </button>
              )}
              {step === "upload" && mode === "upload" && (
                <button
                  onClick={onClose}
                  className="px-4 py-2 rounded-lg bg-primary hover:bg-primary-hover text-white font-medium text-sm transition-colors"
                >
                  Cancel
                </button>
              )}
            </div>
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
