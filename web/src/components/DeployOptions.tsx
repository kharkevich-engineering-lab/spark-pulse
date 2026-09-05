/** Deploy options — engine, model override and extra args, plus a dry-run preview.

Sits above the recipe form in the deploy drawer. "Preview" calls
POST /api/deployments/plan, which resolves the engine, image ref, model and the
exact command that would run, so the operator sees it before deploying.

The engine picker is limited to engines that can actually run this recipe: a v1
recipe carries a vLLM `command` template and therefore pins itself to vLLM.
*/

import { useEffect, useMemo, useState } from "react";
import { fetchEngines, fetchModels, fetchNodes, planDeployment, runPreflight } from "@/lib/api";
import type { ClusterNode, DeployPlan, EngineSummary, PreflightReport, RecipeDetail } from "@/lib/types";
import { AlertCircle, ChevronDown, Eye, Loader2 } from "lucide-react";
import { formatSize } from "@/lib/utils";
import PreflightPanel from "@/components/PreflightPanel";
import { ExperimentalBadge, ExperimentalBanner } from "@/components/Experimental";
import {
  MULTI_NODE_BADGE_TITLE,
  MULTI_NODE_REASON,
  MULTI_NODE_TITLE,
  MULTI_NODE_UNPROVEN,
} from "@/lib/experimental";

export interface DeployOptionsValue {
  engine?: string;
  model?: string;
  extra_args?: string[];
  /** Addresses this deployment should span. Empty/omitted means solo on the
   *  control node — see the node selector below for why the control node's
   *  own address is always the first entry once there is more than one. */
  nodes?: string[];
  /** Tensor-parallel width. Undefined means "whatever the recipe declares";
   *  the form seeds it from there so the number on screen is the number sent. */
  tensor_parallel?: number;
  /** Pipeline-parallel depth, same convention as `tensor_parallel`. */
  pipeline_parallel?: number;
}

/** The parallelism shape the form can express. `dp` is deliberately absent:
 *  no recipe in the tree sets it and no engine block maps it to a flag, so
 *  offering a control for it would be offering a knob with nothing behind it.
 *  The server still counts it — see `_check_capacity` — which is why the
 *  occupancy line below says what *this* shape occupies rather than claiming
 *  to be the rule. */
export interface Parallelism {
  tensor_parallel: number;
  pipeline_parallel: number;
}

/** A recipe's declared parallelism, as the number the form should start at.
 *
 * `params` and `defaults` are the same dict on the wire (the API writes both
 * from one source); `params` is read first because it is the name the v2
 * schema uses. Anything missing or unreadable is 1, which is what the engine
 * would render for an absent parameter anyway.
 */
export function recipeParallelism(recipe: RecipeDetail): Parallelism {
  const declared = { ...(recipe.defaults ?? {}), ...(recipe.params ?? {}) };
  const read = (key: string): number => {
    const raw = declared[key];
    const n = typeof raw === "number" ? raw : Number.parseInt(String(raw ?? ""), 10);
    return Number.isFinite(n) && n >= 1 ? Math.floor(n) : 1;
  };
  return { tensor_parallel: read("tensor_parallel"), pipeline_parallel: read("pipeline_parallel") };
}

/** How many nodes a shape occupies. One GPU per node, so this is the product. */
export function occupancy(shape: Parallelism): number {
  return shape.tensor_parallel * shape.pipeline_parallel;
}

/** The shape to propose when the operator changes the node count.
 *
 * One GPU per node means the world size *is* the node count, so a shape that
 * occupies anything else is refused — too large as "does not fit", too small
 * as a launch vLLM refuses outright. Ticking a second node must
 * therefore leave the form in a state that works, and the only free variable
 * the operator has not spoken about is tensor width.
 *
 * So: leave a shape that already fits alone, keep a deliberate pipeline depth
 * when the node count divides by it, and otherwise fall back to the one shape
 * that always fits a one-GPU-per-node cluster — `tp = nodes, pp = 1`.
 */
export function proposeParallelism(nodeCount: number, current: Parallelism): Parallelism {
  const nodes = Math.max(1, nodeCount);
  if (occupancy(current) === nodes) return current;
  const pp = current.pipeline_parallel;
  if (pp >= 1 && nodes % pp === 0) {
    return { tensor_parallel: nodes / pp, pipeline_parallel: pp };
  }
  return { tensor_parallel: nodes, pipeline_parallel: 1 };
}

/** The occupancy line: what this shape occupies against what is selected.
 *
 * Worded from the server's own refusals (`native_runtime._check_capacity`) on
 * purpose. The operator meets one of those sentences if they press Preview or
 * Deploy anyway, and two different explanations of one rule read as two rules.
 */
export function describeOccupancy(
  shape: Parallelism,
  nodeCount: number,
): { fits: boolean; text: string } {
  const needed = occupancy(shape);
  const flags = `tp=${shape.tensor_parallel} pp=${shape.pipeline_parallel}`;
  const nodeWord = (n: number) => `${n} node${n === 1 ? "" : "s"}`;
  if (needed === nodeCount) {
    return { fits: true, text: `${flags} occupies ${nodeWord(nodeCount)} — the nodes selected.` };
  }
  if (needed > nodeCount) {
    return {
      fits: false,
      text:
        `${flags} does not fit ${nodeWord(nodeCount)}: it needs ${needed}. ` +
        `This hardware has one GPU per node, so either lower the parallelism ` +
        `or deploy across ${nodeWord(needed)}.`,
    };
  }
  return {
    fits: false,
    text:
      `${flags} only occupies ${needed} of the ${nodeWord(nodeCount)} selected. ` +
      `One GPU per node means the world size is the node count, so the extra ` +
      `${nodeCount - needed} would join the rendezvous with nothing to hold and ` +
      `and vLLM refuses a --nnodes that does not divide it exactly, so this ` +
      `would fail on every rank rather than serve on a subset. ` +
      `Deploy on ${nodeWord(needed)}, or raise the ` +
      `parallelism until tp*pp is ${nodeCount}.`,
  };
}

/** The `params` body a plan, a pre-flight and a create all get.
 *
 * Falls back to the recipe's own numbers so an unseeded form still sends what
 * it displays: previewing and deploying must be the same request, or the
 * preview is a description of some other deployment.
 */
export function deployParams(
  recipe: RecipeDetail,
  value: DeployOptionsValue,
): Record<string, unknown> {
  const declared = recipeParallelism(recipe);
  return {
    tensor_parallel: value.tensor_parallel ?? declared.tensor_parallel,
    pipeline_parallel: value.pipeline_parallel ?? declared.pipeline_parallel,
  };
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

/** How an engine reads in the picker.
 *
 * The variant has to be here. Two variants of one engine — vllm and
 * vllm-b12x — are two different images with different kernels, and labelling
 * both "vllm · 0.1.0" gives the operator two identical options. "default" is
 * the absence of a variant, so it is the one that stays unsaid.
 */
export function engineLabel(engine: EngineSummary): string {
  const name = engine.variant && engine.variant !== "default"
    ? `${engine.engine}/${engine.variant}`
    : engine.engine;
  return `${name} · ${engine.version}`;
}

export interface EngineChoice {
  engine: EngineSummary;
  supported: boolean;
  /** Empty when supported; otherwise why this engine cannot run the recipe. */
  reason: string;
}

/** Every enabled engine with the backend's verdict on this recipe.
 *
 * The verdict comes from `recipe.engine_support`, which the API computes with
 * the same engine plugins that plan the deployment, so the picker offers
 * exactly what a deploy would accept. Older payloads without that field fall
 * back to the one rule the frontend can apply on its own: a `command:`
 * template is written in vLLM's flags.
 */
export function engineChoices(engines: EngineSummary[], recipe: RecipeDetail): EngineChoice[] {
  const support = new Map((recipe.engine_support ?? []).map((e) => [e.engine, e]));
  const hasCommand = Boolean(recipe.command && recipe.command.trim());

  return engines
    .filter((e) => e.enabled)
    .map((engine) => {
      const reported = support.get(engine.engine);
      if (reported) {
        return { engine, supported: reported.supported, reason: reported.reason };
      }
      const supported = !hasCommand || engine.engine === "vllm";
      return {
        engine,
        supported,
        reason: supported ? "" : "recipe carries an engine-specific command for 'vllm'",
      };
    });
}

/** What the plan says about the image being on this host, in one line.
 *
 * An absent image used to mean a silent multi-minute download once the deploy
 * had started; saying so here is the whole point of asking the plan.
 */
export function describeImagePresence(plan: Pick<DeployPlan, "image_present" | "image_size_bytes">): string {
  if (plan.image_present) {
    return plan.image_size_bytes ? `pulled · ${formatSize(plan.image_size_bytes)}` : "pulled";
  }
  const size = plan.image_size_bytes ? formatSize(plan.image_size_bytes) : "several GB";
  return `image not pulled, ${size} will download first`;
}

/** Engines that may actually run this recipe. */
export function eligibleEngines(engines: EngineSummary[], recipe: RecipeDetail): EngineSummary[] {
  return engineChoices(engines, recipe)
    .filter((c) => c.supported)
    .map((c) => c.engine);
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
  const [nodes, setNodes] = useState<ClusterNode[]>([]);
  const [extraArgsText, setExtraArgsText] = useState("");
  // Free text rather than the number itself: a controlled number input snaps
  // an emptied field back to a digit, so clearing it to retype "2" would give
  // "12". The parent only ever hears whole numbers >= 1.
  const [tpText, setTpText] = useState("");
  const [ppText, setPpText] = useState("");
  // Whether the operator has spoken about tensor width. The node-count
  // proposal below is for a form that is still showing the recipe's number;
  // once an operator has typed one, changing the node count must not quietly
  // replace it.
  const [tpTouched, setTpTouched] = useState(false);
  const [plan, setPlan] = useState<DeployPlan | null>(null);
  const [preflight, setPreflight] = useState<PreflightReport | null>(null);
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
    fetchNodes()
      .then(setNodes)
      .catch(() => setNodes([]));
  }, []);

  const declared = useMemo(() => recipeParallelism(recipe), [recipe]);

  // Seed from the recipe, once per recipe. Seeding the *parent's* value —
  // rather than only the text on screen — is the point: the plan, the
  // pre-flight and the create all read it, so a form that displayed 2 while
  // sending nothing would preview a deployment nobody asked for.
  useEffect(() => {
    setTpText(String(declared.tensor_parallel));
    setPpText(String(declared.pipeline_parallel));
    setTpTouched(false);
    onChange({ ...value, ...declared });
    // Deliberately only `recipe.id`: `value` and `onChange` are read fresh
    // from the render that changed it, and re-running on every value change
    // would undo the operator's own edits on the next keystroke.
  }, [recipe.id]);

  /** What the form is currently asking for, whether or not the parent kept it. */
  const shape: Parallelism = {
    tensor_parallel: value.tensor_parallel ?? declared.tensor_parallel,
    pipeline_parallel: value.pipeline_parallel ?? declared.pipeline_parallel,
  };

  const choices = useMemo(() => engineChoices(engines, recipe), [engines, recipe]);
  const available = useMemo(() => choices.filter((c) => c.supported), [choices]);
  const unavailable = useMemo(() => choices.filter((c) => !c.supported), [choices]);

  // A registry of one (or zero, before the control node has registered
  // itself) has exactly one possible value for "which nodes take part" — the
  // control node, solo — so there is nothing for a selector to select.
  const controlNode = useMemo(() => nodes.find((n) => n.is_control_plane), [nodes]);
  const otherNodes = useMemo(() => nodes.filter((n) => !n.is_control_plane), [nodes]);
  const selectedAddresses = useMemo(() => new Set(value.nodes ?? []), [value.nodes]);
  const worldSize = 1 + otherNodes.filter((n) => selectedAddresses.has(n.address)).length;
  // Live, so the mismatch is on screen before Preview is pressed rather than
  // arriving as a 400 afterwards. The server is still the one that decides.
  const fit = describeOccupancy(shape, worldSize);

  const toggleNode = (address: string) => {
    const next = new Set(selectedAddresses);
    if (next.has(address)) next.delete(address);
    else next.add(address);
    // Solo is "no peers picked", not "nothing in the set". The set always
    // holds the control node's own address once anything has been picked, so
    // testing the set for emptiness never fired: unticking the last peer left
    // `nodes: [<control address>]` behind, which is a *cluster of one* — it
    // plans with the machine's LAN address instead of 127.0.0.1 and carries
    // the multi-node warning, for a deployment the operator asked to be solo.
    const peers = otherNodes.filter((n) => next.has(n.address)).map((n) => n.address);
    // The control node is never deselectable, so it always leads the list —
    // and always at rank 0, since the plan assigns ranks by array order.
    const ordered = [...(controlNode ? [controlNode.address] : []), ...peers];
    // `undefined`, not `[]`: a solo deploy is the one that names no nodes.
    const chosen = peers.length === 0 ? undefined : ordered;
    // Propose a shape that fits the new count. Ticking a peer used to leave
    // the form asking for two nodes with a tp of 1, which the server refuses
    // — and no control here could raise it, so the tick was a dead end.
    const proposed = tpTouched ? shape : proposeParallelism(peers.length + 1, shape);
    setTpText(String(proposed.tensor_parallel));
    setPpText(String(proposed.pipeline_parallel));
    onChange({ ...value, nodes: chosen, ...proposed });
  };

  /** Take a typed parallelism value, if it is one. Text that is not a whole
   *  number >= 1 stays on screen and is not sent: half-typed input is not an
   *  instruction, and a form that "corrected" it would fight the operator. */
  const editParallelism = (key: keyof Parallelism, raw: string) => {
    if (key === "tensor_parallel") {
      setTpText(raw);
      setTpTouched(true);
    } else {
      setPpText(raw);
    }
    const parsed = Number.parseInt(raw, 10);
    if (!/^\d+$/.test(raw.trim()) || !Number.isFinite(parsed) || parsed < 1) return;
    onChange({ ...value, [key]: parsed });
  };

  const preview = async () => {
    setPlanning(true);
    setPlanError(null);
    setPlan(null);
    setPreflight(null);
    const body = {
      recipe_id: recipe.id,
      engine: value.engine || undefined,
      model: value.model || undefined,
      extra_args: value.extra_args ?? [],
      nodes: value.nodes?.length ? value.nodes : undefined,
      // The same body the deploy sends — see `deployParams`.
      params: deployParams(recipe, value),
    };
    try {
      const result = await planDeployment(body);
      setPlan(result);
    } catch (e) {
      setPlanError(e instanceof Error ? e.message : "Preview failed");
      setPlanning(false);
      return;
    }
    // The pre-flight is the same question against the real nodes, so it is
    // asked here rather than after a deploy has already started downloading.
    // It can fail on its own without costing the operator the preview.
    try {
      setPreflight(await runPreflight(body));
    } catch {
      setPreflight(null);
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
              {available.map(({ engine }) => (
                <option key={engine.key} value={engine.key}>
                  {engineLabel(engine)}
                </option>
              ))}
            </select>

            {unavailable.length > 0 && (
              <ul className="mt-2 space-y-1" data-testid="engines-unavailable">
                {unavailable.map(({ engine, reason }) => (
                  <li
                    key={engine.key}
                    className="flex items-start gap-1.5 text-xs text-text-muted"
                  >
                    <AlertCircle size={13} className="shrink-0 mt-0.5" />
                    <span>
                      <span className="font-mono">{engine.key}</span> unavailable
                      {reason ? `: ${reason}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
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

          <div data-testid="deploy-parallelism">
            <span className="block text-sm font-medium mb-1">Parallelism</span>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label
                  htmlFor="deploy-tensor-parallel"
                  className="block text-xs text-text-muted mb-1"
                >
                  Tensor parallel
                </label>
                <input
                  id="deploy-tensor-parallel"
                  type="number"
                  min={1}
                  value={tpText}
                  onChange={(e) => editParallelism("tensor_parallel", e.target.value)}
                  className="w-full px-3 py-2 rounded-lg bg-surface border border-border focus:border-primary focus:outline-none font-mono text-sm"
                />
              </div>
              <div>
                <label
                  htmlFor="deploy-pipeline-parallel"
                  className="block text-xs text-text-muted mb-1"
                >
                  Pipeline parallel
                </label>
                <input
                  id="deploy-pipeline-parallel"
                  type="number"
                  min={1}
                  value={ppText}
                  onChange={(e) => editParallelism("pipeline_parallel", e.target.value)}
                  className="w-full px-3 py-2 rounded-lg bg-surface border border-border focus:border-primary focus:outline-none font-mono text-sm"
                />
              </div>
            </div>
            <p
              className={`text-xs mt-1 ${fit.fits ? "text-text-muted" : "text-warning"}`}
              data-testid="deploy-occupancy"
            >
              {fit.text}
            </p>
          </div>

          {nodes.length >= 2 && (
            <div data-testid="deploy-node-selector">
              <label className="flex items-center gap-2 text-sm font-medium mb-1">
                Nodes
                <ExperimentalBadge title={MULTI_NODE_BADGE_TITLE} />
              </label>
              <div
                className="space-y-1.5 p-2 rounded-lg bg-surface border border-border"
                data-testid="deploy-nodes"
              >
                {controlNode && (
                  <label className="flex items-center gap-2 text-sm opacity-70">
                    <input type="checkbox" checked disabled className="shrink-0" />
                    <span>
                      {controlNode.name}{" "}
                      <span className="font-mono text-text-muted">{controlNode.address}</span>{" "}
                      · control node
                    </span>
                  </label>
                )}
                {otherNodes.map((node) => (
                  <label key={node.id} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      className="shrink-0"
                      checked={selectedAddresses.has(node.address)}
                      onChange={() => toggleNode(node.address)}
                    />
                    <span>
                      {node.name} <span className="font-mono text-text-muted">{node.address}</span>
                    </span>
                  </label>
                ))}
              </div>
              <p className="text-xs text-text-muted mt-1" data-testid="deploy-world-size">
                {worldSize} node{worldSize === 1 ? "" : "s"}, ranks 0-{worldSize - 1}
              </p>
              {worldSize > 1 && (
                <ExperimentalBanner
                  className="mt-2"
                  title={MULTI_NODE_TITLE}
                  reason={MULTI_NODE_REASON}
                  items={MULTI_NODE_UNPROVEN}
                />
              )}
            </div>
          )}

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
            <div
              className="flex items-start gap-2 p-3 rounded-lg bg-danger/10 border border-danger/30 text-danger text-sm"
              data-testid="deploy-plan-error"
            >
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
                <dt className="text-text-muted">On this host</dt>
                <dd className={plan.image_present ? "font-mono" : "font-mono text-warning"}>
                  {describeImagePresence(plan)}
                </dd>
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

              {preflight && <PreflightPanel report={preflight} />}

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
