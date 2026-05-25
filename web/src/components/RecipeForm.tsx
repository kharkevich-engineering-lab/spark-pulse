/** Recipe form component — editable fields for recipe customization.

The `isEditing` prop controls whether the form is in edit mode or view mode.
The form does NOT manage its own editing state — it's controlled by the parent modal.

Props:
- isEditing: whether the form fields are editable
- hasCustomization: whether the recipe already has customizations (used for UI hints)
*/

import { useState, useEffect, useImperativeHandle, forwardRef, useRef } from "react";
import type { RecipeDetail, RecipeCustomization, RecipeFormRef } from "@/lib/types";
import { X, Code2 } from "lucide-react";

const RecipeForm = forwardRef<RecipeFormRef, {
  recipe: RecipeDetail;
  customization: RecipeCustomization;
  onDeploy: (name: string, params: Record<string, unknown>) => Promise<void>;
  onSaveCustomization?: (fields: Partial<RecipeCustomization>) => void;
  onReset?: () => void;
  isRunning: boolean;
  clusterBlocked: boolean;
  isEditing: boolean;
}>(function RecipeForm({
  recipe,
  onSaveCustomization,
  onReset,
  isEditing,
}, ref) {
  const [editAsYaml, setEditAsYaml] = useState(false);
  const [rawYaml, setRawYaml] = useState("");
  const [yamlError, setYamlError] = useState<string | null>(null);
  const [name, setName] = useState(recipe.name);
  const [model, setModel] = useState(recipe.model || "");
  const [container, setContainer] = useState(recipe.container || "vllm-node");
  const [command, setCommand] = useState(recipe.command || "");
  const [editDefaults, setEditDefaults] = useState<Record<string, unknown>>({ ...recipe.defaults });
  const [envEntries, setEnvEntries] = useState<[string, string][]>(
    Object.entries(recipe.env || {}) as [string, string][]
  );
  const [buildArgs, setBuildArgs] = useState<string[]>(recipe.build_args || []);
  const [newEnvKey, setNewEnvKey] = useState("");
  const [newEnvValue, setNewEnvValue] = useState("");
  const [newBuildArg, setNewBuildArg] = useState("");
  const [modsList, setModsList] = useState<string[]>(recipe.mods || []);
  const [newMod, setNewMod] = useState("");
  const formChangeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const updateRawYaml = () => {
    const fields: Record<string, unknown> = {
      name, model, container,
      ...(command && { command }),
      ...(JSON.stringify(editDefaults) !== "{}" && { defaults: editDefaults }),
      ...(envEntries.length > 0 && { env: Object.fromEntries(envEntries) }),
      ...(buildArgs.length > 0 && { build_args: buildArgs }),
      ...(modsList.length > 0 && { mods: modsList }),
    };
    const serialize = (obj: unknown, indent: number = 0): string => {
      const pad = "  ".repeat(indent);
      if (typeof obj === "string") return `${pad}${obj}`;
      if (typeof obj === "number") return `${pad}${obj}`;
      if (typeof obj === "boolean") return `${pad}${obj}`;
      if (obj === null || obj === undefined) return `${pad}null`;
      if (Array.isArray(obj)) return obj.map(item => `${pad}- ${serialize(item, indent + 1)}`).join("\n");
      if (typeof obj === "object" && obj !== null) {
        return Object.entries(obj as Record<string, unknown>).map(([k, v]) => {
          if (typeof v === "object" && v !== null) return `${pad}${k}:\n${serialize(v, indent + 1)}`;
          return `${pad}${k}: ${serialize(v, indent + 1)}`;
        }).join("\n");
      }
      return `${pad}${String(obj)}`;
    };
    setRawYaml(`---\n${serialize(fields)}`);
  };

  // Sync form state -> rawYaml whenever a form field changes (but not during render)
  useEffect(() => { updateRawYaml(); }, [name, model, container, command, editDefaults, envEntries, buildArgs, modsList]);

  const watchFormChanges = () => {
    if (formChangeTimer.current) clearTimeout(formChangeTimer.current);
    formChangeTimer.current = setTimeout(() => { updateRawYaml(); setYamlError(null); }, 300);
  };

  const parseYaml = (yaml: string): Record<string, unknown> | null => {
    try {
      const result: Record<string, unknown> = {};
      const lines = yaml.split("\n");
      let currentKey: string | null = null;
      let currentList: string[] | null = null;
      let currentMap: Record<string, string> | null = null;
      for (const line of lines) {
        const match = line.match(/^(\w+):\s*(.*)$/);
        if (match) {
          if (currentKey === "build_args" && currentList) result[currentKey] = currentList;
          else if (currentKey === "env" && currentMap) result[currentKey] = currentMap;
          else if (currentKey === "mods" && currentList) result[currentKey] = currentList;
          currentKey = match[1];
          const value = match[2].trim();
          if (value === "") currentList = [];
          else if (currentKey === "defaults") currentMap = {};
          else { result[currentKey] = value; currentKey = null; }
        } else {
          const listMatch = line.match(/^\s*-\s*(.+)$/);
          if (listMatch && (currentKey === "build_args" || currentKey === "mods")) {
            if (!currentList) currentList = [];
            currentList.push(listMatch[1].trim());
          }
        }
      }
      if (currentKey === "build_args" && currentList) result[currentKey] = currentList;
      if (currentKey === "env" && currentMap) result[currentKey] = currentMap;
      if (currentKey === "mods" && currentList) result[currentKey] = currentList;
      return result;
    } catch { return null; }
  };

  useImperativeHandle(ref, () => ({
    save: () => {
      if (!onSaveCustomization) return;
      if (editAsYaml && rawYaml.trim()) {
        const parsed = parseYaml(rawYaml);
        if (!parsed) { setYamlError("Failed to parse YAML"); return; }
        onSaveCustomization({
          model: parsed.model as string, container: parsed.container as string,
          command: parsed.command as string, defaults: parsed.defaults as Record<string, unknown>,
          env: parsed.env as Record<string, string>, build_args: parsed.build_args as string[],
          mods: parsed.mods as string[],
        });
        return;
      }
      const fields: Partial<RecipeCustomization> = {};
      if (model !== recipe.model) fields.model = model;
      if (container !== recipe.container) fields.container = container;
      if (command !== recipe.command) fields.command = command;
      if (JSON.stringify(editDefaults) !== JSON.stringify(recipe.defaults)) {
        const diff: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(editDefaults)) {
          if (v !== recipe.defaults?.[k]) diff[k] = v;
        }
        if (JSON.stringify(diff) !== "{}") fields.defaults = diff;
      }
      const envObj = Object.fromEntries(envEntries.filter(e => e[0] || e[1]).map(e => [e[0], e[1]]));
      if (JSON.stringify(envObj) !== JSON.stringify(recipe.env || {})) fields.env = envObj;
      if (JSON.stringify(buildArgs) !== JSON.stringify(recipe.build_args || [])) fields.build_args = buildArgs;
      if (JSON.stringify(modsList) !== JSON.stringify(recipe.mods || [])) fields.mods = modsList;
      if (Object.keys(fields).length > 0) {
        onSaveCustomization(fields);
      } else {
        // Allow parent to treat Save as a completed action even with no diffs.
        onSaveCustomization({});
      }
    },
    cancel: () => {
      setName(recipe.name); setModel(recipe.model || ""); setContainer(recipe.container || "vllm-node");
      setCommand(recipe.command || ""); setEditDefaults({ ...recipe.defaults });
      setEnvEntries(Object.entries(recipe.env || {}) as [string, string][]);
      setBuildArgs(recipe.build_args || []); setModsList(recipe.mods || []);
      setNewEnvKey(""); setNewEnvValue(""); setNewBuildArg(""); setEditAsYaml(false);
    },
  }));

  const addEnv = () => {
    if (newEnvKey.trim()) { setEnvEntries(prev => [...prev, [newEnvKey.trim(), newEnvValue]]); setNewEnvKey(""); setNewEnvValue(""); }
  };
  const removeEnv = (i: number) => setEnvEntries(prev => prev.filter((_, idx) => idx !== i));
  const addBuildArg = () => { if (newBuildArg.trim()) { setBuildArgs(prev => [...prev, newBuildArg.trim()]); setNewBuildArg(""); } };
  const removeBuildArg = (i: number) => setBuildArgs(prev => prev.filter((_, idx) => idx !== i));
  const addMod = () => { if (newMod.trim() && !modsList.includes(newMod.trim())) { setModsList(prev => [...prev, newMod.trim()]); setNewMod(""); } };
  const removeMod = (mod: string) => setModsList(prev => prev.filter(m => m !== mod));

  // Form fields
  const renderFormFields = () => (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium mb-1">Recipe Name</label>
          <input type="text" value={name} onChange={(e) => { setName(e.target.value); watchFormChanges(); }}
            disabled={!isEditing}
            className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm disabled:opacity-60" />
        </div>
        <div>
          <label className="block text-sm font-medium mb-1">Model</label>
          <input type="text" value={model} onChange={(e) => { setModel(e.target.value); watchFormChanges(); }}
            disabled={!isEditing} placeholder="e.g. Intel/Qwen3.5-397B-INT4"
            className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm text-text-muted disabled:opacity-60" />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium mb-1">Container</label>
          <input type="text" value={container} onChange={(e) => { setContainer(e.target.value); watchFormChanges(); }}
            disabled={!isEditing}
            className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm disabled:opacity-60" />
        </div>
        {command && (
          <div>
            <label className="block text-sm font-medium mb-1">Command Template
              {isEditing && <span className="text-xs text-text-muted ml-2 font-normal">Use &#123;model&#125; tokens</span>}
            </label>
            <textarea value={command} onChange={(e) => { setCommand(e.target.value); watchFormChanges(); }}
              disabled={!isEditing} rows={3}
              className="w-full px-3 py-2 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-xs disabled:opacity-60 resize-y" />
          </div>
        )}
      </div>
      {Object.keys(recipe.defaults).length > 0 && (
        <div>
          <div className="flex items-center justify-between mb-2">
            <p className="text-sm font-medium">Default Parameters</p>
            {isEditing && <span className="text-xs text-text-muted">Edit values below</span>}
          </div>
          <div className="space-y-1">
            {Object.entries(editDefaults).map(([k, v]) => (
              <div key={k} className="flex items-center justify-between text-sm px-3 py-1.5 rounded bg-bg gap-3">
                <span className="font-mono text-text-muted shrink-0">{k}</span>
                {isEditing ? (
                  <input type={typeof v === "number" ? "number" : "text"} value={String(v)}
                    onChange={(e) => { const val = typeof v === "number" ? parseFloat(e.target.value) : e.target.value;
                      setEditDefaults(prev => ({ ...prev, [k]: val })); watchFormChanges(); }}
                    className="w-32 px-2 py-0.5 rounded border border-border focus:border-primary focus:outline-none font-mono text-sm" />
                ) : (<span className="font-mono shrink-0">{String(v)}</span>)}
              </div>
            ))}
          </div>
        </div>
      )}
      <div>
        <div className="flex items-center justify-between mb-2">
          <p className="text-sm font-medium">Environment Variables</p>
          {isEditing && (
            <div className="flex items-center gap-2">
              <input type="text" placeholder="KEY" value={newEnvKey} onChange={(e) => setNewEnvKey(e.target.value)}
                className="w-24 px-2 py-0.5 rounded border border-border focus:border-primary focus:outline-none font-mono text-xs" />
              <input type="text" placeholder="VALUE" value={newEnvValue} onChange={(e) => setNewEnvValue(e.target.value)}
                className="w-24 px-2 py-0.5 rounded border border-border focus:border-primary focus:outline-none font-mono text-xs" />
              <button onClick={addEnv} className="text-xs px-2 py-0.5 rounded bg-primary/20 text-primary hover:bg-primary/30">Add</button>
            </div>
          )}
        </div>
        {envEntries.length > 0 ? (
          <div className="space-y-1">
            {envEntries.map((e, i) => (
              <div key={i} className="flex items-center justify-between text-sm px-3 py-1.5 rounded bg-bg gap-3">
                <span className="font-mono text-text-muted shrink-0">{e[0] || "—"}</span>
                <span className="font-mono text-text-muted shrink-0 truncate max-w-[200px]">{e[1] || "—"}</span>
                {isEditing && <button onClick={() => removeEnv(i)} className="text-danger hover:text-danger-hover shrink-0">✕</button>}
              </div>
            ))}
          </div>
        ) : !isEditing && (<p className="text-xs text-text-muted italic">No environment variables</p>)}
      </div>
      <div>
        <div className="flex items-center justify-between mb-2">
          <p className="text-sm font-medium">Build Args</p>
          {isEditing && (
            <div className="flex items-center gap-2">
              <input type="text" placeholder="--build-arg X=1" value={newBuildArg} onChange={(e) => setNewBuildArg(e.target.value)}
                className="w-48 px-2 py-0.5 rounded border border-border focus:border-primary focus:outline-none font-mono text-xs" />
              <button onClick={addBuildArg} className="text-xs px-2 py-0.5 rounded bg-primary/20 text-primary hover:bg-primary/30">Add</button>
            </div>
          )}
        </div>
        {buildArgs.length > 0 ? (
          <div className="flex flex-wrap gap-2">
            {buildArgs.map((arg, i) => (
              <span key={i} className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-tag-bg font-mono">
                {arg}
                {isEditing && <button onClick={() => removeBuildArg(i)} className="text-danger hover:text-danger-hover ml-1">✕</button>}
              </span>
            ))}
          </div>
        ) : !isEditing && (<p className="text-xs text-text-muted italic">No build args</p>)}
      </div>
      <div>
        <div className="flex items-center justify-between mb-2">
          <p className="text-sm font-medium">Mods</p>
          {isEditing && (
            <div className="flex items-center gap-2">
              <input type="text" placeholder="mod-name" value={newMod} onChange={(e) => setNewMod(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), addMod())}
                className="w-40 px-2 py-0.5 rounded border border-border focus:border-primary focus:outline-none font-mono text-xs" />
              <button onClick={addMod} className="text-xs px-2 py-0.5 rounded bg-primary/20 text-primary hover:bg-primary/30">Add</button>
            </div>
          )}
        </div>
        {modsList.length > 0 ? (
          <div className="flex flex-wrap gap-2">
            {modsList.map(m => (
              <span key={m} className="inline-flex items-center gap-1 px-2.5 py-1 rounded text-xs bg-tag-bg font-mono">
                {m}
                {isEditing && <button onClick={() => removeMod(m)} className="text-danger hover:text-danger-hover ml-1"><X size={10} /></button>}
              </span>
            ))}
          </div>
        ) : (<p className="text-xs text-text-muted italic">No mods</p>)}
      </div>
      {isEditing && (
        <div className="pt-2 border-t border-border">
          <button onClick={onReset} className="px-4 py-2.5 rounded-lg border border-border hover:border-warning/50 text-sm font-medium transition-colors flex items-center gap-2 text-warning">
            <X size={14} /> Reset
          </button>
        </div>
      )}
    </div>
  );

  // YAML editor
  const renderYamlEditor = () => (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <label className="text-sm font-medium">Recipe YAML</label>
        {yamlError && (<span className="text-xs text-danger">{yamlError}</span>)}
      </div>
      <textarea value={rawYaml} onChange={(e) => { setRawYaml(e.target.value); }} disabled={!isEditing}
        className="w-full h-[400px] px-4 py-3 rounded-lg bg-bg border border-border focus:border-primary focus:outline-none font-mono text-sm resize-y disabled:opacity-60"
        spellCheck={false} />
      <p className="text-xs text-text-muted">Edit the YAML directly. Changes are saved when you click Save.</p>
    </div>
  );

  return (
    <div>
      {isEditing && (
        <div className="flex items-center gap-2 mb-4">
          <button onClick={() => setEditAsYaml(false)}
            className={`px-3 py-1.5 text-sm font-medium rounded transition-colors ${!editAsYaml ? "bg-primary/10 text-primary" : "text-text-muted hover:text-text"}`}>
            Form
          </button>
          <button onClick={() => setEditAsYaml(true)}
            className={`px-3 py-1.5 text-sm font-medium rounded transition-colors flex items-center gap-1.5 ${editAsYaml ? "bg-primary/10 text-primary" : "text-text-muted hover:text-text"}`}>
            <Code2 size={14} />
            Edit as YAML
          </button>
        </div>
      )}
      {editAsYaml ? renderYamlEditor() : renderFormFields()}
    </div>
  );
});

export default RecipeForm;
