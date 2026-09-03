/** Deploy options — engine, model override and extra args, plus a dry-run preview.

Sits above the recipe form in the deploy drawer. "Preview" calls
POST /api/deployments/plan, which resolves the engine, image ref, model and the
exact command that would run, so the operator sees it before deploying.

The engine picker is limited to engines that can actually run this recipe: a v1
recipe carries a vLLM `command` template and therefore pins itself to vLLM.
*/

import { useEffect, useMemo, useState } from "react";
import { fetchEngines, fetchModels, planDeployment } from "@/lib/api";
import type { DeployPlan, EngineSummary, RecipeDetail } from "@/lib/types";
import { AlertCircle, ChevronDown, Eye, Loader2 } from "lucide-react";

export interface DeployOptionsValue {
  engine?: string;
  model?: string;
  extra_args?: string[];
}

/** Split a free-text args field into argv, respecting simple quoting. */
export function parseExtraArgs(raw: string): string[] {
  const matches = raw.match(/"[^"]*"|'[^']*'|\S+/g);
  if (!matches) return [];
  return matches.map((token) =>
    (token.startsWith('"') && token.endsWith('"')) || (token.startsWith("'") && token.endsWith("'"))
      ? token.slice(1, -1)
      : token,
  );
}

/** Engines that may run this recipe. A `command:` template is vLLM-only. */
export function eligibleEngines(engines: EngineSummary[], recipe: RecipeDetail): EngineSummary[] {
  const enabled = engines.filter((e) => e.enabled);
  if (recipe.command && recipe.command.trim()) {
    return enabled.filter((e) => e.engine === "vllm");
  }
  return enabled;
}

export default function DeployOptions({
  recipe,
  value,
  onChange,
}: {
  recipe: RecipeDetail;
  value: DeployOptionsValue;
  onChange: (next: DeployOptionsValue) => void;
}) {
  const [engines, setEngines] = useState<EngineSummary[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [extraArgsText, setExtraArgsText] = useState("");
  const [plan, setPlan] = useState<DeployPlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    fetchEngines()
      .then((r) => setEngines(r.engines))
      .catch(() => setEngines([]));
    fetchModels()
      .then((m) => setModels(m.map((entry) => entry.id)))
      .catch(() => setModels([]));
  }, []);

  const available = useMemo(() => eligibleEngines(engines, recipe), [engines, recipe]);

  const preview = async () => {
    setPlanning(true);
    setPlanError(null);
    setPlan(null);
    try {
      const result = await planDeployment({
        recipe_id: recipe.id,
        engine: value.engine || undefined,
        model: value.model || undefined,
        extra_args: value.extra_args ?? [],
      });
      setPlan(result);
    } catch (e) {
      setPlanError(e instanceof Error ? e.message : "Preview failed");
    } finally {
      setPlanning(false);
    }
  };

  return (
    <div className="px-6 pt-5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 text-sm font-medium text-text-muted hover:text-text transition-colors"
        aria-expanded={open}
      >
        <ChevronDown size={16} className={`transition-transform ${open ? "rotate-180" : ""}`} />
        Deploy options
      </button>

      {open && (
        <div className="mt-4 space-y-4 p-4 rounded-xl bg-bg border border-border">
          <div>
            <label htmlFor="deploy-engine" className="block text-sm font-medium mb-1">
              Engine
            </label>
            <select
              id="deploy-engine"
              value={value.engine ?? ""}
              onChange={(e) => onChange({ ...value, engine: e.target.value || undefined })}
              className="w-full px-3 py-2 rounded-lg bg-surface border border-border focus:border-primary focus:outline-none font-mono text-sm"
            >
              <option value="">Recipe default</option>
              {available.map((engine) => (
                <option key={engine.key} value={engine.engine}>
                  {engine.engine} · {engine.version}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label htmlFor="deploy-model" className="block text-sm font-medium mb-1">
              Model override
            </label>
            <input
              id="deploy-model"
              list="deploy-model-options"
              type="text"
              value={value.model ?? ""}
              placeholder={recipe.model || "leave empty for the recipe's model"}
              onChange={(e) => onChange({ ...value, model: e.target.value || undefined })}
              className="w-full px-3 py-2 rounded-lg bg-surface border border-border focus:border-primary focus:outline-none font-mono text-sm"
            />
            <datalist id="deploy-model-options">
              {models.map((id) => (
                <option key={id} value={id} />
              ))}
            </datalist>
          </div>

          <div>
            <label htmlFor="deploy-extra-args" className="block text-sm font-medium mb-1">
              Extra args
            </label>
            <input
              id="deploy-extra-args"
              type="text"
              value={extraArgsText}
              placeholder="--enable-prefix-caching --max-num-seqs 16"
              onChange={(e) => {
                setExtraArgsText(e.target.value);
                onChange({ ...value, extra_args: parseExtraArgs(e.target.value) });
              }}
              className="w-full px-3 py-2 rounded-lg bg-surface border border-border focus:border-primary focus:outline-none font-mono text-sm"
            />
            <p className="text-xs text-text-muted mt-1">Appended to the engine command, quoted.</p>
          </div>

          <button
            type="button"
            onClick={preview}
            disabled={planning}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border hover:border-primary/50 text-sm font-medium transition-colors disabled:opacity-50"
          >
            {planning ? <Loader2 size={14} className="animate-spin" /> : <Eye size={14} />}
            Preview
          </button>

          {planError && (
            <div className="flex items-start gap-2 p-3 rounded-lg bg-danger/10 border border-danger/30 text-danger text-sm">
              <AlertCircle size={16} className="shrink-0 mt-0.5" />
              <span>{planError}</span>
            </div>
          )}

          {plan && (
            <div className="space-y-3 text-sm" data-testid="deploy-plan">
              <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-1">
                <dt className="text-text-muted">Engine</dt>
                <dd className="font-mono truncate">
                  {plan.engine}/{plan.variant}
                </dd>
                <dt className="text-text-muted">Image</dt>
                <dd className="font-mono truncate">{plan.image_ref}</dd>
                <dt className="text-text-muted">Model</dt>
                <dd className="font-mono truncate">{plan.model || "(from the command)"}</dd>
                <dt className="text-text-muted">Port</dt>
                <dd className="font-mono">{plan.port}</dd>
                {plan.mods.length > 0 && (
                  <>
                    <dt className="text-text-muted">Mods</dt>
                    <dd className="font-mono truncate">{plan.mods.join(", ")}</dd>
                  </>
                )}
              </dl>

              {plan.warnings.map((warning) => (
                <p key={warning} className="text-xs text-warning">
                  {warning}
                </p>
              ))}

              <div>
                <p className="text-xs uppercase tracking-wide text-text-muted mb-1">Command</p>
                <pre className="p-3 rounded-lg bg-surface border border-border text-xs font-mono overflow-x-auto whitespace-pre-wrap">
                  {plan.launch_command}
                </pre>
              </div>

              <div>
                <p className="text-xs uppercase tracking-wide text-text-muted mb-1">Container</p>
                <p className="font-mono text-xs text-text-muted">
                  {plan.container.privileged ? "privileged" : "unprivileged"}
                  {plan.container.network_host ? " · network host" : ""}
                  {plan.container.ipc_host ? " · ipc host" : ""}
                  {` · shm ${plan.container.shm_size_gb}g`}
                </p>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
