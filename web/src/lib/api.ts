import type { RecipeSummary, RecipeDetail, Deployment, MemoryResponse, CacheEntry, Settings, SecretsResponse, ModSummary, ModDetail, RecipeCustomization, CustomRecipeInfo, CustomModInfo, ModFileMap, BenchmarkResult, OciRegistry, OciCollection, OciCollectionRecipe, OciRecipeMeta, OciUpdateCheck, OciUpdateApply, OciUpdateResult, OciAutoUpdateSettings, EngineListResponse, EngineDetail, EngineIndexRefreshResult, RenderRequest, RenderResult, ModelEntry, ModelSource, ModelDownloadJob, ModelSyncResult, ModelPresence, ModelDeleteResult, ImageEntry, ImagePullJob, ImageSyncResult, ImagePresence, ImageDeleteResult, RecipeImportResult, RecipeImportStatus, DeployPlan, DeployPlanRequest } from "@/lib/types";

const API = "/api";

// ── CSRF token ───────────────────────────────────────────────────────────────

let csrfToken: string | null = null;

/** Read CSRF token from a meta tag. Call early (e.g. in app bootstrap). */
export function initCsrfToken(): void {
  const el = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]');
  csrfToken = el?.content ?? null;
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...((init?.headers as Record<string, string>) || {}) };
  // Attach CSRF token on state-changing requests
  const method = (init?.method || "GET").toUpperCase();
  if ((method === "POST" || method === "PUT" || method === "DELETE" || method === "PATCH") && csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
  }
  const res = await fetch(`${API}${path}`, { headers, credentials: "include", ...init });
  if (res.status === 401) {
    // Redirect to login on any 401 (cookie-based auth — no token to check)
    window.location.href = "/login";
    throw new Error("Unauthorized");
  }
  if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
  return res.json();
}

// ── Recipes ─────────────────────────────────────────────────────────────────

export async function fetchRecipes(): Promise<RecipeSummary[]> { return json<RecipeSummary[]>("/recipes"); }
export async function fetchRecipe(id: string): Promise<RecipeDetail> { return json<RecipeDetail>(`/recipes/${id}`); }

/** Import recipes and mods from a local spark-vllm-docker checkout or a git URL. */
export async function importRecipes(body: { path?: string; url?: string; ref?: string }): Promise<RecipeImportResult> {
  return json<RecipeImportResult>("/recipes/import", { method: "POST", body: JSON.stringify(body) });
}

export async function fetchRecipeImportStatus(): Promise<RecipeImportStatus> {
  return json<RecipeImportStatus>("/recipes/import/status");
}

// ── Deployments ─────────────────────────────────────────────────────────────

export async function fetchDeployments(): Promise<Deployment[]> { return json<Deployment[]>("/deployments"); }
export async function createDeployment(body: { recipe_id: string; name: string; params: Record<string, unknown>; nodes?: string[]; engine?: string; variant?: string; model?: string; extra_args?: string[]; allow_missing_model?: boolean }): Promise<Deployment> { return json<Deployment>("/deployments", { method: "POST", body: JSON.stringify(body) }); }
/** Dry run: resolve engine, image, model and the rendered command without deploying. */
export async function planDeployment(body: DeployPlanRequest): Promise<DeployPlan> { return json<DeployPlan>("/deployments/plan", { method: "POST", body: JSON.stringify(body) }); }
export async function fetchDeployment(id: string): Promise<Deployment> { return json<Deployment>(`/deployments/${id}`); }
export async function stopDeployment(id: string): Promise<void> { await json(`/deployments/${id}`, { method: "DELETE" }); }
export async function fetchLogs(id: string, n = 200): Promise<{ logs: string }> { return json(`/deployments/${id}/logs?lines=${n}`); }

// ── Memory ──────────────────────────────────────────────────────────────────

export async function fetchMemory(): Promise<MemoryResponse> { return json<MemoryResponse>("/memory"); }
export async function killGpuProcess(pid: number): Promise<{ killed: boolean; pid: number; error?: string }> { return json(`/memory/processes/${pid}`, { method: "DELETE" }); }

// ── Cache ───────────────────────────────────────────────────────────────────

export async function fetchCache(): Promise<{ entries: CacheEntry[] }> { return json<{ entries: CacheEntry[] }>("/cache"); }
export async function cleanCache(targets: string[]): Promise<Record<string, string>> { return json<Record<string, string>>("/cache/clean", { method: "POST", body: JSON.stringify({ targets }) }); }

// ── Settings ────────────────────────────────────────────────────────────────

export async function fetchSettings(): Promise<Settings> { return json<Settings>("/settings"); }
export async function updateSettings(partial: Partial<Settings>): Promise<Settings> { return json<Settings>("/settings", { method: "PUT", body: JSON.stringify(partial) }); }
export async function fetchSecrets(): Promise<SecretsResponse> { return json<SecretsResponse>("/settings/secrets"); }
export async function saveSecrets(partial: { hf_token?: string }): Promise<SecretsResponse> { return json<SecretsResponse>("/settings/secrets", { method: "PUT", body: JSON.stringify(partial) }); }
export async function deleteSecret(key: string): Promise<void> { await json(`/settings/secrets/${key}`, { method: "DELETE" }); }

// ── Mods ─────────────────────────────────────────────────────────────────────

export async function fetchMods(): Promise<ModSummary[]> { return json<ModSummary[]>("/mods"); }
export async function fetchMod(id: string): Promise<ModDetail> { return json<ModDetail>(`/mods/${encodeURIComponent(id)}`); }

// ── Recipe Customizations ────────────────────────────────────────────────────

export async function fetchRecipeCustomization(recipeId: string): Promise<RecipeCustomization> { return json<RecipeCustomization>(`/recipes/customize/${encodeURIComponent(recipeId)}`); }
export async function saveRecipeCustomization(recipeId: string, customization: RecipeCustomization): Promise<RecipeCustomization> { return json<RecipeCustomization>(`/recipes/customize/${encodeURIComponent(recipeId)}`, { method: "PUT", body: JSON.stringify(customization) }); }
export async function deleteRecipeCustomization(recipeId: string): Promise<{ deleted: boolean }> { return json<{ deleted: boolean }>(`/recipes/customize/${encodeURIComponent(recipeId)}`, { method: "DELETE" }); }

// ── SSE ─────────────────────────────────────────────────────────────────────

export function connectLogStream(deploymentId: string, onMessage: (event: string, data: unknown) => void): () => void {
  const es = new EventSource(`/sse/logs/${deploymentId}`);
  const parse = (raw: string) => {
    try { return JSON.parse(raw); }
    catch { return null; }
  };
  es.addEventListener("log", (e: MessageEvent) => { const d = parse(e.data); if (d !== null) onMessage("log", d); });
  es.addEventListener("status", (e: MessageEvent) => { const d = parse(e.data); if (d !== null) onMessage("status", d); });
  es.addEventListener("error", (e: MessageEvent) => { const d = parse(e.data); if (d !== null) onMessage("error", d); });
  return () => es.close();
}

export function connectMetricsStream(onMessage: (event: string, data: unknown) => void): () => void {
  const es = new EventSource("/sse/metrics");
  const parse = (raw: string) => {
    try { return JSON.parse(raw); }
    catch { return null; }
  };
  es.addEventListener("metrics", (e: MessageEvent) => { const d = parse(e.data); if (d !== null) onMessage("metrics", d); });
  es.addEventListener("error", (e: MessageEvent) => { const d = parse(e.data); if (d !== null) onMessage("error", d); });
  return () => es.close();
}

// ── Custom Files ────────────────────────────────────────────────────────────

export async function listCustomRecipes(): Promise<CustomRecipeInfo[]> {
  return json<CustomRecipeInfo[]>("/custom-files/recipes/list");
}

export async function getCustomRecipeContent(recipeId: string): Promise<{ content: string; id: string }> {
  return json<{ content: string; id: string }>(`/custom-files/recipes/${recipeId}`);
}

export async function saveCustomRecipe(recipeId: string, yamlContent: string): Promise<{ saved: boolean }> {
  return json<{ saved: boolean }>(`/custom-files/recipes/${recipeId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: yamlContent }),
  });
}

export async function deleteCustomRecipe(recipeId: string): Promise<{ deleted: boolean }> {
  return json<{ deleted: boolean }>(`/custom-files/recipes/${recipeId}`, { method: "DELETE" });
}

export async function uploadCustomRecipe(file: File): Promise<{ id: string; name: string }> {
  const formData = new FormData();
  formData.append("file", file);
  const headers: Record<string, string> = {};
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const res = await fetch(`${API}/custom-files/recipes/upload`, {
    headers,
    body: formData,
    method: "POST",
    credentials: "include",
  });
  if (res.status === 401) { window.location.href = "/login"; throw new Error("Unauthorized"); }
  if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
  return res.json();
}

export async function listCustomMods(): Promise<CustomModInfo[]> {
  return json<CustomModInfo[]>("/custom-files/mods/list");
}

export async function getCustomModFiles(modId: string): Promise<{ files: ModFileMap; id: string }> {
  return json<{ files: ModFileMap; id: string }>(`/custom-files/mods/${modId}`);
}

export async function saveCustomModFiles(modId: string, files: ModFileMap): Promise<{ saved: boolean }> {
  return json<{ saved: boolean }>(`/custom-files/mods/${modId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(files),
  });
}

export async function deleteCustomMod(modId: string): Promise<{ deleted: boolean }> {
  return json<{ deleted: boolean }>(`/custom-files/mods/${modId}`, { method: "DELETE" });
}

// ── Symlink Sync ────────────────────────────────────────────────────────────

export async function syncSymlinks(mode: "create" | "remove"): Promise<{ recipes: string[]; mods: string[] }> {
  return json<{ recipes: string[]; mods: string[] }>("/custom-files/symlinks/sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
}

// ── Benchmarking ──────────────────────────────────────────────────────────────

export async function fetchBenchmarks(): Promise<BenchmarkResult[]> {
  return json<BenchmarkResult[]>("/benchmarks");
}

export async function fetchBenchmark(id: string): Promise<BenchmarkResult> {
  return json<BenchmarkResult>(`/benchmarks/${id}`);
}

export async function fetchLatestByRecipe(): Promise<Record<string, BenchmarkResult>> {
  return json<Record<string, BenchmarkResult>>("/benchmarks/latest-by-recipe");
}

export async function compareRuns(runIds: string[]): Promise<{
  runs: Record<string, BenchmarkResult>;
  comparison: Record<string, any>;
  run_ids: string[];
}> {
  return json<{
    runs: Record<string, BenchmarkResult>;
    comparison: Record<string, any>;
    run_ids: string[];
  }>("/benchmarks/compare", {
    method: "POST",
    body: JSON.stringify({ run_ids: runIds }),
  });
}

// ── OCI Registry ─────────────────────────────────────────────────────────────

export async function fetchOciRegistries(): Promise<OciRegistry[]> {
  return json<OciRegistry[]>("/oci/registries");
}

export async function addOciRegistry(registry: Partial<OciRegistry>): Promise<OciRegistry> {
  return json<OciRegistry>("/oci/registries", { method: "POST", body: JSON.stringify(registry) });
}

export async function updateOciRegistry(name: string, registry: Partial<OciRegistry>): Promise<OciRegistry> {
  return json<OciRegistry>(`/oci/registries/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(registry) });
}

export async function removeOciRegistry(name: string): Promise<void> {
  await json(`/oci/registries/${encodeURIComponent(name)}`, { method: "DELETE" });
}

export async function testOciRegistry(name: string): Promise<{ ok: boolean; error?: string }> {
  return json(`/oci/registries/${encodeURIComponent(name)}/test-connection`);
}

export async function fetchOciRegistryVersions(name: string): Promise<{ versions: string[] }> {
  return json(`/oci/registries/${encodeURIComponent(name)}/versions`);
}

export async function fetchOciCollections(registry?: string, version?: string): Promise<OciCollection[]> {
  const params = new URLSearchParams();
  if (registry) params.set("registry", registry);
  if (version) params.set("version", version);
  const query = params.toString();
  return json<OciCollection[]>(`/oci/collections${query ? `?${query}` : ""}`);
}

export async function installOciCollection(name: string, version: string, registry?: string): Promise<{ installed: string[] }> {
  return json<{ installed: string[] }>("/oci/install", {
    method: "POST",
    body: JSON.stringify({ name, version, registry }),
  });
}

export async function checkOciUpdates(collection?: string, registry?: string): Promise<OciUpdateCheck[]> {
  const params = new URLSearchParams();
  if (collection) params.set("collection", collection);
  if (registry) params.set("registry", registry);
  const query = params.toString();
  return json<OciUpdateCheck[]>(`/oci/check${query ? `?${query}` : ""}`);
}

export async function applyOciUpdates(updates: OciUpdateApply[]): Promise<OciUpdateResult[]> {
  return json<OciUpdateResult[]>("/oci/update", {
    method: "POST",
    body: JSON.stringify({ updates }),
  });
}

export async function fetchOciMeta(): Promise<OciRecipeMeta[]> {
  return json<OciRecipeMeta[]>("/oci/recipes/meta");
}

export async function fetchOciMetaByName(name: string): Promise<OciRecipeMeta> {
  return json<OciRecipeMeta>(`/oci/recipes/meta/${encodeURIComponent(name)}`);
}

export async function fetchOciCollectionRecipes(name: string, version?: string, registry?: string): Promise<OciCollectionRecipe[]> {
  const params = new URLSearchParams();
  if (version) params.set("version", version);
  if (registry) params.set("registry", registry);
  const query = params.toString();
  return json<OciCollectionRecipe[]>(`/oci/collections/${encodeURIComponent(name)}/recipes${query ? `?${query}` : ""}`);
}

export async function fetchOciAutoUpdateSettings(): Promise<OciAutoUpdateSettings> {
  return json<OciAutoUpdateSettings>("/oci/auto-update/settings");
}

export async function updateOciAutoUpdateSettings(partial: Partial<OciAutoUpdateSettings>): Promise<OciAutoUpdateSettings> {
  return json<OciAutoUpdateSettings>("/oci/auto-update/settings", {
    method: "PUT",
    body: JSON.stringify(partial),
  });
}

export async function runOciAutoUpdate(): Promise<{ success?: boolean; skipped?: boolean; reason?: string; updated?: number; log?: string[]; error?: string }> {
  return json("/oci/auto-update/run", { method: "POST" });
}

export async function installOciRecipe(body: { collection: string; recipe: string; version?: string; registry?: string; overwrite?: boolean }): Promise<{ success: boolean; recipe: string; action: string }> {
  return json<{ success: boolean; recipe: string; action: string }>("/oci/recipes/install", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateOciRecipe(recipeName: string, body: { collection: string; version?: string; registry?: string }): Promise<{ success: boolean; recipe: string; action: string }> {
  return json<{ success: boolean; recipe: string; action: string }>(`/oci/recipes/update/${encodeURIComponent(recipeName)}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function uninstallOciRecipe(recipeName: string): Promise<{ success: boolean; recipe: string; action: string }> {
  return json<{ success: boolean; recipe: string; action: string }>(`/oci/recipes/${encodeURIComponent(recipeName)}`, {
    method: "DELETE",
  });
}

export async function clearOciCache(key?: string): Promise<{ cleared: number }> {
  return json<{ cleared: number }>("/oci/cache/clear", {
    method: "POST",
    body: JSON.stringify(key ? { key } : {}),
  });
}

export async function startOciBackgroundUpdater(): Promise<{ started: boolean }> {
  return json<{ started: boolean }>("/oci/background/start", { method: "POST" });
}

export async function stopOciBackgroundUpdater(): Promise<{ stopped: boolean }> {
  return json<{ stopped: boolean }>("/oci/background/stop", { method: "POST" });
}

export async function runBenchmark(body: {
  deployment_id: string;
  baseline_id?: string;
  recipe_id?: string;
  recipe_name?: string;
  params?: Record<string, unknown>;
}): Promise<BenchmarkResult> {
  return json<BenchmarkResult>("/benchmarks", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ── Network Discovery ────────────────────────────────────────────────────────

export interface DiscoveryInterface {
  name: string;
  ip: string | null;
  mtu: number;
  is_up: boolean;
  type: "ethernet" | "infiniband" | "loopback" | "docker" | "other";
}

export interface InfinibandDevice {
  hca: string;
  ports: number[];
  net_devices: string[];
  state: string;
}

export interface NcclDefaults {
  socket_ifname: string;
  ib_hca: string | null;
  ib_disable: boolean;
}

export interface DiscoveredConfig {
  nccl: {
    debug: string | null;
    socket_ifname: string | null;
    ib_hca: string | null;
  };
  discovery_available: boolean;
}

export interface DiscoveryResult {
  local_ip: string | null;
  ethernet_if: string | null;
  infiniband_present: boolean;
  infiniband_devices: InfinibandDevice[];
  interfaces: DiscoveryInterface[];
  nccl_defaults: NcclDefaults | null;
  validation_errors: string[];
}

export interface ValidationResult {
  healthy: boolean;
  warnings: string[];
  errors: string[];
}

export interface DiscoveryResponse {
  detected: DiscoveryResult;
  validation: ValidationResult;
}

export async function runDiscovery(): Promise<DiscoveryResponse> {
  return json<DiscoveryResponse>("/discovery", { method: "POST" });
}

export async function getDiscovered(): Promise<DiscoveredConfig> {
  return json<DiscoveredConfig>("/discovery");
}

export async function applyNcclDefaults(body: {
  socket_ifname: string;
  ib_hca: string | null;
  ib_disable: boolean;
}): Promise<{ success: boolean; applied: NcclDefaults }> {
  return json<{ success: boolean; applied: NcclDefaults }>("/discovery/apply-nccl", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function getValidation(): Promise<ValidationResult> {
  return json<ValidationResult>("/discovery/validation");
}

// ── Launch Script (Phase 4) ──────────────────────────────────────────────────

import type {
  LaunchScriptInfo,
  LaunchScriptResolveResult,
  LaunchScriptResolveRequest,
  LaunchScriptAnalyzeRequest,
  LaunchScriptValidateRequest,
  LaunchScriptPatchRequest,
  PatchedScriptBundle,
} from "@/lib/types";

export async function resolveLaunchScript(body: LaunchScriptResolveRequest): Promise<LaunchScriptResolveResult> {
  return json<LaunchScriptResolveResult>("/launch-script/resolve", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function analyzeLaunchScript(body: LaunchScriptAnalyzeRequest): Promise<LaunchScriptInfo> {
  return json<LaunchScriptInfo>("/launch-script/analyze", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function validateLaunchScript(body: LaunchScriptValidateRequest): Promise<{ healthy: boolean; warnings: string[]; errors: string[] }> {
  return json<{ healthy: boolean; warnings: string[]; errors: string[] }>("/launch-script/validate", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function patchLaunchScript(body: LaunchScriptPatchRequest): Promise<PatchedScriptBundle> {
  return json<PatchedScriptBundle>("/launch-script/patch", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ── Mod Deployment (Phase 4) ─────────────────────────────────────────────────

import type {
  ModValidationResult,
  ModDeploymentResult,
  ModRollbackResult,
  ModValidateRequest,
  ModApplyRequest,
  ModRollbackRequest,
} from "@/lib/types";

export async function validateMod(body: ModValidateRequest): Promise<ModValidationResult> {
  return json<ModValidationResult>("/mods/validate", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function applyMod(body: ModApplyRequest): Promise<ModDeploymentResult> {
  return json<ModDeploymentResult>("/mods/apply", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function rollbackMod(body: ModRollbackRequest): Promise<ModRollbackResult> {
  return json<ModRollbackResult>("/mods/rollback", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ── Engines ─────────────────────────────────────────────────────────────────

export async function fetchEngines(): Promise<EngineListResponse> { return json<EngineListResponse>("/engines"); }
export async function fetchEngine(engine: string, variant = "default"): Promise<EngineDetail> { return json<EngineDetail>(`/engines/${engine}/${variant}`); }
export async function refreshEngines(): Promise<EngineIndexRefreshResult> { return json<EngineIndexRefreshResult>("/engines/refresh", { method: "POST" }); }
export async function renderLaunch(body: RenderRequest): Promise<RenderResult> { return json<RenderResult>("/engines/render", { method: "POST", body: JSON.stringify(body) }); }

// ── Models ───────────────────────────────────────────────────────────────────

export async function fetchModels(): Promise<ModelEntry[]> {
  return (await json<{ models: ModelEntry[] }>("/models")).models;
}

export async function fetchModel(id: string): Promise<ModelEntry> {
  return json<ModelEntry>(`/models/${id}`);
}

export async function fetchModelSources(): Promise<ModelSource[]> {
  return (await json<{ sources: ModelSource[] }>("/models/sources")).sources;
}

export async function saveModelSources(sources: ModelSource[]): Promise<ModelSource[]> {
  return (await json<{ sources: ModelSource[] }>("/models/sources", { method: "PUT", body: JSON.stringify({ sources }) })).sources;
}

export async function startModelDownload(body: { model: string; source?: string; revision?: string; allow_patterns?: string[] }): Promise<ModelDownloadJob> {
  return json<ModelDownloadJob>("/models/download", { method: "POST", body: JSON.stringify(body) });
}

export async function fetchModelDownloads(): Promise<ModelDownloadJob[]> {
  return (await json<{ jobs: ModelDownloadJob[] }>("/models/downloads")).jobs;
}

export async function fetchModelDownload(jobId: string): Promise<ModelDownloadJob> {
  return json<ModelDownloadJob>(`/models/downloads/${jobId}`);
}

export async function cancelModelDownload(jobId: string): Promise<ModelDownloadJob> {
  return json<ModelDownloadJob>(`/models/downloads/${jobId}/cancel`, { method: "POST" });
}

export async function syncModelToNodes(id: string, nodes: string[], sshUser?: string): Promise<ModelSyncResult> {
  return json<ModelSyncResult>(`/models/${id}/sync`, { method: "POST", body: JSON.stringify({ nodes, ssh_user: sshUser }) });
}

export async function fetchModelPresence(id: string, nodes: string[]): Promise<ModelPresence> {
  return json<ModelPresence>(`/models/${id}/presence?nodes=${encodeURIComponent(nodes.join(","))}`);
}

export async function deleteModel(id: string): Promise<ModelDeleteResult> {
  return json<ModelDeleteResult>(`/models/${id}`, { method: "DELETE" });
}

// ── Engine images ──────────────────────────────────────────────────
//
// An image ref carries slashes and colons, so it never goes in the path: the
// catalogue is flat and every ref-taking call passes it in the body or query.

export async function fetchImages(): Promise<ImageEntry[]> {
  return (await json<{ images: ImageEntry[] }>("/images")).images;
}

export async function startImagePull(ref: string): Promise<ImagePullJob> {
  return json<ImagePullJob>("/images/pull", { method: "POST", body: JSON.stringify({ ref }) });
}

export async function fetchImagePulls(): Promise<ImagePullJob[]> {
  return (await json<{ jobs: ImagePullJob[] }>("/images/pulls")).jobs;
}

export async function fetchImagePull(jobId: string): Promise<ImagePullJob> {
  return json<ImagePullJob>(`/images/pulls/${jobId}`);
}

export async function cancelImagePull(jobId: string): Promise<ImagePullJob> {
  return json<ImagePullJob>(`/images/pulls/${jobId}/cancel`, { method: "POST" });
}

export async function deleteImage(ref: string): Promise<ImageDeleteResult> {
  return json<ImageDeleteResult>(`/images?ref=${encodeURIComponent(ref)}`, { method: "DELETE" });
}

export async function syncImageToNodes(ref: string, nodes: string[], sshUser?: string): Promise<ImageSyncResult> {
  return json<ImageSyncResult>("/images/sync", { method: "POST", body: JSON.stringify({ ref, nodes, ssh_user: sshUser }) });
}

export async function fetchImagePresence(ref: string, nodes: string[]): Promise<ImagePresence> {
  return json<ImagePresence>(
    `/images/presence?ref=${encodeURIComponent(ref)}&nodes=${encodeURIComponent(nodes.join(','))}`,
  );
}

// ── Node registry ────────────────────────────────────────────────────────────

import type { AddNodeRequest, ClusterNode, DiscoverNodesResult, NodeFinding } from "@/lib/types";

export async function fetchNodes(): Promise<ClusterNode[]> {
  return json<ClusterNode[]>("/nodes");
}

/** Register a node. The id is minted by the server and is never sent. */
export async function addNode(body: AddNodeRequest): Promise<ClusterNode> {
  return json<ClusterNode>("/nodes", { method: "POST", body: JSON.stringify(body) });
}

export async function updateNode(id: string, changes: Partial<ClusterNode>): Promise<ClusterNode> {
  return json<ClusterNode>(`/nodes/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(changes) });
}

/** Forget a node. Wiping its identity and uninstalling its agent are separate. */
export async function removeNode(id: string): Promise<{ removed: boolean; node: ClusterNode }> {
  return json(`/nodes/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** Browse the LAN for peers. Returns an empty list when mDNS is unavailable. */
export async function discoverNodes(timeout = 3): Promise<DiscoverNodesResult> {
  return json<DiscoverNodesResult>(`/nodes/discover?timeout=${timeout}`);
}

export async function fetchNodeDiagnostics(): Promise<{ findings: NodeFinding[] }> {
  return json<{ findings: NodeFinding[] }>("/nodes/diagnostics");
}
