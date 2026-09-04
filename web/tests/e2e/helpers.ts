/** Shared helpers for the end-to-end suite.
 *
 * Everything here talks to the same simulation backend the browser does, so a
 * spec can arrange and clean up its own state over the REST API instead of
 * depending on whatever a previous spec left behind.
 */

import { expect, type APIRequestContext, type Page } from "@playwright/test";

/** The subset of `/api/config` the suite reasons about. */
export interface AppConfig {
  auth_enabled: boolean;
  mcp_enabled: boolean;
  cluster_enabled: boolean;
  cluster_experimental: boolean;
  benchmarking_enabled: boolean;
  simulation_mode: boolean;
  runtime: string;
}

export interface Deployment {
  id: string;
  name: string;
  recipe_id: string;
  status: string;
}

/** Nav entries as `Layout.tsx` declares them, in order. */
export const NAV_ITEMS = [
  { href: "/", label: "Recipes & Mods", heading: "Recipes & Mods" },
  { href: "/jobs", label: "Inference", heading: "Inference" },
  { href: "/cluster", label: "Cluster", heading: "Cluster Orchestration" },
  { href: "/benchmarking", label: "Benchmarking", heading: "Benchmarking" },
  { href: "/monitoring", label: "Monitoring", heading: "Monitoring" },
  { href: "/models", label: "Models", heading: "Models" },
  { href: "/images", label: "Images", heading: "Engine images" },
  { href: "/cache", label: "Cache", heading: "Cache Manager" },
  { href: "/mcp", label: "MCP", heading: "MCP Server" },
  { href: "/oci", label: "OCI Registry", heading: "OCI Recipe Registry" },
  { href: "/settings", label: "Settings", heading: "Settings" },
] as const;

/** Nav entries the running backend should actually show.
 *
 * Benchmarking is the one route a config flag hides, so the expected nav is
 * derived from `/api/config` rather than hard-coded — the same rule the
 * frontend applies.
 */
export function expectedNavLabels(config: AppConfig): string[] {
  return NAV_ITEMS.filter(
    (item) => item.href !== "/benchmarking" || config.benchmarking_enabled,
  ).map((item) => item.label);
}

export async function readConfig(request: APIRequestContext): Promise<AppConfig> {
  const response = await request.get("/api/config");
  expect(response.ok(), "GET /api/config should succeed").toBeTruthy();
  return (await response.json()) as AppConfig;
}

export async function listDeployments(request: APIRequestContext): Promise<Deployment[]> {
  const response = await request.get("/api/deployments");
  expect(response.ok(), "GET /api/deployments should succeed").toBeTruthy();
  return (await response.json()) as Deployment[];
}

/** Remove every deployment for a recipe, so a spec starts from a known state.
 *
 * DELETE stops an active deployment and removes a terminal one, so it takes
 * two passes to clear a running job out of the history.
 */
export async function purgeDeployments(
  request: APIRequestContext,
  recipeId: string,
): Promise<void> {
  for (let attempt = 0; attempt < 3; attempt++) {
    const mine = (await listDeployments(request)).filter((d) => d.recipe_id === recipeId);
    if (mine.length === 0) return;
    for (const deployment of mine) {
      await request.delete(`/api/deployments/${deployment.id}`);
    }
  }
}

/** Open a page and wait for the shell to have rendered. */
export async function gotoPage(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await expect(page.getByRole("navigation")).toBeVisible();
}

/** Fail loudly if the React error boundary swallowed a render crash. */
export async function expectNoCrash(page: Page): Promise<void> {
  await expect(page.getByText("Something went wrong")).toHaveCount(0);
}

/** Escape a value so it can be dropped into a `RegExp` as a literal. */
export function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** The nav label of a link, with the "exp" chip the Cluster entry carries. */
export function navLabel(text: string): string {
  return text.split("\n")[0].replace(/\s*exp$/i, "").trim();
}

/** A GB10 as `nvidia-smi` reports it on this hardware: no memory figures.
 *
 * `memory.used` comes back as `[N/A]` on a GB10 because host and GPU memory
 * are unified, so the backend sets `memory_supported: false` and the page has
 * to render the card without a usage bar.
 */
export const UNIFIED_MEMORY_GPU = {
  gpu: [
    {
      index: 0,
      gpu: "GPU 0",
      uuid: "GPU-11111111-2222-3333-4444-555555555555",
      name: "NVIDIA GB10",
      memory_total: 0,
      memory_used: 0,
      memory_free: 0,
      memory_supported: false,
      temperature: 47,
      utilization: 12,
      power_draw: null,
      power_limit: null,
    },
  ],
  cpu: { total: 131072, used: 43520, free: 87552, available: 92160, usage_percent: 33.2 },
  disk: [
    {
      mount: "/",
      total: 1290277824000,
      used: 837702287360,
      free: 452575536640,
      usage_percent: 64.9,
    },
  ],
  processes: [
    {
      gpu_uuid: "GPU-11111111-2222-3333-4444-555555555555",
      pid: 98251,
      process_name: "VLLM::EngineCore",
      used_memory: 83421,
      is_tracked: false,
    },
  ],
};

/** Serve the Monitoring page's data from a fixture instead of the backend.
 *
 * Two reasons, both structural. Simulation mode delegates GPU stats to the
 * real `nvidia-smi` parsing, so a CI runner or a laptop reports no GPU at all
 * and the interesting rendering never happens. And `GET /api/memory` (like
 * `/sse/metrics`) does `from spark_pulse.tools.deployments import ...`, which
 * rebinds `tools.deployments` to the *real* module for the life of the
 * process — after one such call, simulated deploys shell out to a
 * spark-vllm-docker checkout that does not exist. Stubbing keeps the specs
 * independent of each other's ordering.
 */
export async function stubMemoryEndpoints(
  page: Page,
  payload: unknown = UNIFIED_MEMORY_GPU,
): Promise<void> {
  await page.route("**/api/memory", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(payload) }),
  );
  await page.route("**/sse/metrics", (route) => route.abort());
}
