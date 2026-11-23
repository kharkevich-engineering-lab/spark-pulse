/** Shared helpers for the end-to-end suite.
 *
 * Everything here talks to the same simulation backend the browser does, so a
 * spec can arrange and clean up its own state over the REST API instead of
 * depending on whatever a previous spec left behind.
 */

import { expect, type APIRequestContext, type Locator, type Page } from "@playwright/test";

/** The subset of `/api/config` the suite reasons about. */
export interface AppConfig {
  auth_enabled: boolean;
  mcp_enabled: boolean;
  cluster_enabled: boolean;
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

/** The sidebar entries as `Layout.tsx` declares them, in order.
 *
 * Every object type is one entry; `heading` is the page title on its route.
 * Benchmarks has an entry only when benchmarking is enabled, so it is not in
 * this fixed list — `expectedNavLabels` adds it from the config.
 */
export const NAV_GROUPS = [
  { href: "/", label: "Overview", heading: "Overview." },
  { href: "/jobs", label: "Runs", heading: "Runs." },
  { href: "/cluster", label: "Nodes", heading: "Nodes." },
  { href: "/recipes", label: "Recipes", heading: "Recipes." },
  { href: "/models", label: "Models", heading: "Models." },
  { href: "/engines", label: "Engines", heading: "Engines." },
  { href: "/mods", label: "Mods", heading: "Mods." },
  { href: "/settings", label: "Settings", heading: "Settings." },
] as const;

/** The labels the sidebar should carry, given whether benchmarking is on. */
export function expectedNavLabels(benchmarking = false): string[] {
  const labels: string[] = NAV_GROUPS.map((item) => item.label);
  if (benchmarking) labels.splice(2, 0, "Benchmarks");
  return labels;
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

/** Open a page and wait for the shell to have rendered.
 *
 * The brand lockup rather than the nav: under 900px the nav lives inside the
 * menu and is not on the page until the menu button is pressed.
 */
export async function gotoPage(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await expect(page.getByRole("link", { name: /Spark Pulse home/ })).toBeVisible();
}

/** The nav, at whatever width the project runs at.
 *
 * Above 900px it is in the header. Below it the menu button has to be pressed
 * first — so a spec that navigates through the nav works on a phone without
 * knowing which project it is running under.
 */
export async function openNav(page: Page): Promise<Locator> {
  const menuButton = page.getByRole("button", { name: "Menu" });
  if (await menuButton.isVisible()) {
    if ((await menuButton.getAttribute("aria-expanded")) !== "true") {
      await menuButton.click();
    }
    return page.getByTestId("mobile-nav");
  }
  return page.getByTestId("primary-nav");
}

/** Fail loudly if the React error boundary swallowed a render crash. */
export async function expectNoCrash(page: Page): Promise<void> {
  await expect(page.getByText("Something went wrong")).toHaveCount(0);
}

/** Escape a value so it can be dropped into a `RegExp` as a literal. */
export function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** The nav label of a link, with the "exp" chip the Fleet entry carries. */
export function navLabel(text: string): string {
  return text.split("\n")[0].replace(/\s*exp$/i, "").trim();
}

/** A GB10 as `nvidia-smi` reports it on this hardware: no memory figures.
 *
 * `memory.used` comes back as `[N/A]` on a GB10 because host and GPU memory
 * are unified, so the backend sets `memory_supported: false` and the page has
 * to render the card without a usage bar.
 */
export const UNIFIED_MEMORY_NODE = {
  id: "control",
  name: "spark-01",
  address: "192.168.1.100",
  is_control_plane: true,
  reachable: true,
  error: null,
  unavailable: [],
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

/** The whole answer: one shape, whatever the cluster size. */
export const UNIFIED_MEMORY_GPU = { nodes: [UNIFIED_MEMORY_NODE] };

/** Serve the Monitoring page's data from a fixture instead of the backend.
 *
 * Only for the shapes the simulated cluster does not produce — a machine with
 * no GPU at all, and an answer from a control plane too old to know about
 * nodes. The simulated agent answers with a GB10 now, so what the page does
 * with real data is checked against the real backend instead of against a
 * fixture that can agree with a broken page.
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

/** Open a run's page from the Runs list and return it.
 *
 * A run has a page of its own (`/jobs/:id`), reached from its name on the
 * list — a table row at 1200px and over, a card under it, the same link on
 * both. The page carries the endpoint, the actions and three tabs (Overview,
 * Logs, Events); a spec that reads the logs or the events opens that tab.
 */
export async function openRun(page: Page, id: string): Promise<Locator> {
  const row = page.getByTestId(`deployment-${id}`);
  await expect(row).toBeVisible();
  await row.getByRole("link").first().click();
  const detail = page.getByTestId(`run-detail-${id}`);
  await expect(detail).toBeVisible();
  return detail;
}

/** Back from a run's page to the list. */
export async function backToRuns(page: Page): Promise<void> {
  await page.getByRole("main").getByRole("link", { name: "Runs", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Runs.", exact: true })).toBeVisible();
}
