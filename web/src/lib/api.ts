import type { RecipeSummary, RecipeDetail, Deployment, MemoryResponse, CacheEntry, Settings } from "@/lib/types";

const API = "/api";

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, { headers: { "Content-Type": "application/json" }, ...init });
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

// ── Cache ───────────────────────────────────────────────────────────────────

export async function fetchCache(): Promise<{ entries: CacheEntry[] }> { return json<{ entries: CacheEntry[] }>("/cache"); }
export async function cleanCache(targets: string[]): Promise<Record<string, string>> { return json<Record<string, string>>("/cache/clean", { method: "POST", body: JSON.stringify({ targets }) }); }

// ── Settings ────────────────────────────────────────────────────────────────

export async function fetchSettings(): Promise<Settings> { return json<Settings>("/settings"); }
export async function updateSettings(partial: Partial<Settings>): Promise<Settings> { return json<Settings>("/settings", { method: "PUT", body: JSON.stringify(partial) }); }

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
