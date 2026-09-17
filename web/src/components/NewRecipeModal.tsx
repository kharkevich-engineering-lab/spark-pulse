/** Modal for uploading or manually creating a custom recipe. */

import { useState, useCallback, useRef } from "react";
import { useI18n } from "@/lib/i18n";
import { Save, Upload, FileCode } from "lucide-react";
import { AlertModal, Button, ErrorLine, Field, Input, Modal, Textarea } from "@/ui";

type EntryMode = "upload" | "manual";

interface ValidationIssue {
  field: string;
  message: string;
}

/** Each problem on its own red line under the editor, in place of the bordered
 *  block that read like an outage for one missing field. */
function ValidationErrors({
  issues,
  label,
  className,
}: {
  issues: ValidationIssue[];
  label: string;
  className?: string;
}) {
  if (issues.length === 0) return null;
  return (
    <div className={className}>
      <ErrorLine>{label}</ErrorLine>
      <ul className="mt-0.5 space-y-0.5 text-[13px] text-bad">
        {issues.map((err, i) => (
          <li key={i}>{err.message}</li>
        ))}
      </ul>
    </div>
  );
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
  const { t } = useI18n();
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
      <Modal
        open
        onClose={handleCancel}
        size="lg"
        title={
          step === "upload" ? (mode === "upload" ? "Upload Recipe" : "Manual Recipe") : "Preview Recipe"
        }
        actions={
          <>
            <Button size="sm" onClick={handleCancel}>
              {step === "preview" ? "Back" : "Cancel"}
            </Button>
            {step === "preview" && !validationErrors.length && (
              <Button
                size="sm"
                variant="primary"
                icon={Save}
                loading={saving}
                disabled={!recipeName.trim()}
                onClick={handleSave}
              >
                {saving ? "Saving..." : "Save Recipe"}
              </Button>
            )}
          </>
        }
      >
        <>
          {/* Body */}
          {step === "upload" ? (
            <div>
              {/* Mode toggle */}
              <div className="flex items-center gap-2 mb-6 p-1 rounded-sm bg-bg border border-border w-fit">
                <button
                  onClick={() => { setMode("upload"); setStep("upload"); setFilename(""); }}
                  className={`px-3 py-1.5 text-sm font-medium rounded transition-colors flex items-center gap-1.5 ${
                    mode === "upload" ? "bg-primary/10 text-blue2" : "text-text-muted hover:text-text"
                  }`}
                >
                  <Upload size={14} />
                  Upload
                </button>
                <button
                  onClick={() => { setMode("manual"); setStep("upload"); }}
                  className={`px-3 py-1.5 text-sm font-medium rounded transition-colors flex items-center gap-1.5 ${
                    mode === "manual" ? "bg-primary/10 text-blue2" : "text-text-muted hover:text-text"
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
                    className={`border-2 border-dashed rounded-md p-12 text-center transition-colors ${
                      dragOver
                        ? "border-primary bg-primary/5"
                        : "border-border hover:border-primary/50 hover:bg-surface-hover"
                    }`}
                  >
                    <Upload size={32} className="mx-auto text-text-muted mb-4" />
                    <p className="text-sm font-medium text-text mb-1">
                      Drag and drop a YAML file here, or{" "}
                      <span
                        className="text-blue2 underline cursor-pointer"
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
                      className="text-sm text-blue2 hover:text-blue2 underline"
                    >
                      Enter YAML manually
                    </button>
                  </div>

                  <ValidationErrors
                    issues={validationErrors}
                    label={t("newRecipe.validationFailed")}
                    className="mt-4"
                  />
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
                        <div className="flex items-center rounded-sm border border-line overflow-hidden text-[13px]">
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
                                format === v ? "bg-bg2 text-text font-medium" : "text-muted hover:text-text"
                              }`}
                            >
                              v{v}
                            </button>
                          ))}
                        </div>
                        <Button size="sm" onClick={handleSaveManual} disabled={!content.trim()}>
                          Validate recipe
                        </Button>
                      </div>
                    </div>
                    <Textarea
                      id="recipe-yaml"
                      mono
                      value={content}
                      onChange={(e) => {
                        setContent(e.target.value);
                        // A cheap read for the header while typing; the
                        // authoritative name comes from the parser on validate.
                        const nameMatch = e.target.value.match(/^name:\s*(.+)$/m);
                        if (nameMatch) setRecipeName(nameMatch[1].trim());
                      }}
                      className="h-[400px]"
                      spellCheck={false}
                      placeholder={TEMPLATES["2"]}
                    />
                    <p className="text-[13px] text-muted mt-1.5">
                      Both recipe formats are accepted. Validation reports each problem against
                      the field it belongs to.
                    </p>
                  </div>
                </>
              )}
            </div>
          ) : (
            <div>
              <Field label={t("newRecipe.name")} className="mb-3">
                {(control) => (
                  <Input
                    {...control}
                    mono
                    type="text"
                    value={recipeName}
                    onChange={(e) => setRecipeName(e.target.value)}
                  />
                )}
              </Field>
              {mode === "upload" && filename && (
                <p className="mb-3 text-[13px] text-muted">Source: {filename}</p>
              )}

              <ValidationErrors issues={validationErrors} label={t("newRecipe.validationFailed")} />

              {/* YAML Preview */}
              <Field label={t("newRecipe.yamlContent")}>
                {(control) => (
                  <Textarea
                    {...control}
                    mono
                    value={content}
                    readOnly
                    className="h-[300px]"
                    spellCheck={false}
                  />
                )}
              </Field>
            </div>
          )}
        </>
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
