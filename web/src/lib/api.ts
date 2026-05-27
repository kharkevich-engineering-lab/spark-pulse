import type { RecipeSummary, RecipeDetail, Deployment, MemoryResponse, CacheEntry, Settings, SecretsResponse, ModSummary, ModDetail, RecipeCustomization, GitUpdateStatus, GitUpdateAction, GitUpdateCheckResult, CustomRecipeInfo, CustomModInfo, ModFileMap, BenchmarkResult } from "@/lib/types";

const API = "/api";

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...((init?.headers as Record<string, string>) || {}) };
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

// ── Deployments ─────────────────────────────────────────────────────────────

export async function fetchDeployments(): Promise<Deployment[]> { return json<Deployment[]>("/deployments"); }
export async function createDeployment(body: { recipe_id: string; name: string; params: Record<string, unknown>; nodes?: string[] }): Promise<Deployment> { return json<Deployment>("/deployments", { method: "POST", body: JSON.stringify(body) }); }
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

// ── Git Update ───────────────────────────────────────────────────────────────

export async function fetchGitUpdateStatus(): Promise<GitUpdateStatus> { return json<GitUpdateStatus>("/git-update/status"); }
export async function triggerGitUpdateCheck(): Promise<GitUpdateCheckResult> { return json<GitUpdateCheckResult>("/git-update/check", { method: "POST" }); }
export async function triggerGitFetch(): Promise<GitUpdateAction> { return json<GitUpdateAction>("/git-update/fetch", { method: "POST" }); }
export async function triggerGitPull(): Promise<GitUpdateAction> { return json<GitUpdateAction>("/git-update/pull", { method: "POST" }); }

// ── SSE ─────────────────────────────────────────────────────────────────────

export function connectLogStream(deploymentId: string, onMessage: (event: string, data: unknown) => void): () => void {
  const es = new EventSource(`/sse/logs/${deploymentId}`);
  es.addEventListener("log", (e: MessageEvent) => onMessage("log", JSON.parse(e.data)));
  es.addEventListener("status", (e: MessageEvent) => onMessage("status", JSON.parse(e.data)));
  es.addEventListener("error", (e: MessageEvent) => onMessage("error", JSON.parse(e.data)));
  return () => es.close();
}

export function connectMetricsStream(onMessage: (event: string, data: unknown) => void): () => void {
  const es = new EventSource("/sse/metrics");
  es.addEventListener("metrics", (e: MessageEvent) => onMessage("metrics", JSON.parse(e.data)));
  es.addEventListener("error", (e: MessageEvent) => onMessage("error", JSON.parse(e.data)));
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
