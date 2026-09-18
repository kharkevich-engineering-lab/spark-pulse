/** Settings: every setting in the product, on one page, over one form.
 *
 * Read-only on purpose: saving here writes ~/.config/spark-pulse/settings.json
 * on the machine running the suite, which would change what every other spec
 * sees. So this drives the tabs and asserts what they report, and the only
 * thing it asserts about Save is that it is *there* and inert.
 */

import { expect, test } from "@playwright/test";
import { expectNoCrash, gotoPage } from "./helpers";

interface Settings {
  spark_vllm_path: string;
  default_port_range_start: number;
  default_port_range_end: number;
  default_engine: string;
  engine_indexes: string[];
  docker: { shm_size_gb: number; pids_limit: number };
}

interface McpToolList {
  result: { tools: { name: string }[] };
}

test("shows the configuration the backend is running with", async ({ page, request }) => {
  const response = await request.get("/api/settings");
  expect(response.ok(), "GET /api/settings should succeed").toBeTruthy();
  const settings = (await response.json()) as Settings;

  await gotoPage(page, "/settings");

  // Every tab renders, and each is reached by name.
  for (const tab of [
    "Deployment",
    "Containers",
    "Features",
    "Library",
    "MCP",
    "Preferences",
    "Secrets",
    "Environment",
  ]) {
    await expect(page.getByRole("tab", { name: tab, exact: true })).toBeVisible();
  }

  const save = page.getByRole("button", { name: /Save settings/ });

  // The form is populated from /api/settings, so every configured value should
  // be sitting in a field on the tab it belongs to.
  const valuesOnScreen = async () =>
    page
      .locator("input")
      .evaluateAll((nodes) => nodes.map((node) => (node as HTMLInputElement).value));

  await expect(page.getByRole("heading", { name: "Deployment Defaults", exact: true })).toBeVisible();
  const deployment = await valuesOnScreen();
  expect(deployment).toContain(settings.spark_vllm_path);
  expect(deployment).toContain(String(settings.default_port_range_start));
  expect(deployment).toContain(String(settings.default_port_range_end));

  // The Docker block used to render its defaults from literals in the page, so
  // it showed numbers the backend had never heard of. It comes from the API now.
  await page.getByRole("tab", { name: "Containers" }).click();
  await expect(page.getByRole("heading", { name: "Container Limits", exact: true })).toBeVisible();
  const containers = await valuesOnScreen();
  expect(containers).toContain(String(settings.docker.shm_size_gb));
  expect(containers).toContain(String(settings.docker.pids_limit));

  // Library: the engine registry, which was a panel at the bottom of Engines,
  // and the model sources, which were a panel at the top of Models.
  await page.getByRole("tab", { name: "Library" }).click();
  await expect(page.getByRole("heading", { name: "Where engines come from" })).toBeVisible();
  const library = await valuesOnScreen();
  expect(library).toContain(settings.default_engine);
  const indexes = await page.getByLabel("Engine indexes").inputValue();
  expect(indexes).toBe(settings.engine_indexes.join("\n"));
  await expect(page.getByRole("heading", { name: "Model sources" })).toBeVisible();
  await expect(page.getByLabel("Source 1 name")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Registries", exact: true })).toBeVisible();

  // MCP: the tool list is asked of the endpoint itself rather than kept by hand
  // in the page, which is how the page came to document nine of twenty-one.
  const rpc = await request.post("/mcp", {
    data: { jsonrpc: "2.0", id: 1, method: "tools/list", params: {} },
  });
  expect(rpc.ok(), "POST /mcp tools/list should succeed").toBeTruthy();
  const tools = ((await rpc.json()) as McpToolList).result.tools;
  expect(tools.length).toBeGreaterThan(0);

  await page.getByRole("tab", { name: "MCP", exact: true }).click();
  await expect(page.getByRole("heading", { name: `Tools (${tools.length})` })).toBeVisible();
  for (const tool of [tools[0], tools[tools.length - 1]]) {
    await expect(page.getByText(tool.name, { exact: true })).toBeVisible();
  }

  // Read-only by design: what the Environment tab reports is exactly what a
  // browser must not be able to change.
  await page.getByRole("tab", { name: "Environment" }).click();
  await expect(page.getByRole("heading", { name: "Access", exact: true })).toBeVisible();
  expect(await page.locator("input").count()).toBe(0);

  // Save is on every tab — it used to be rendered on three of six, so an edit
  // made on Containers and read back from Environment had no button to write
  // it with. It stays inert until something changes, which is also what keeps
  // this spec from writing to the settings file of whoever is running it.
  for (const tab of ["Preferences", "Secrets", "Environment", "Deployment"]) {
    await page.getByRole("tab", { name: tab, exact: true }).click();
    await expect(save).toBeVisible();
    await expect(save).toBeDisabled();
  }

  // The old `/mcp` page is that tab now. The route stays, because it is in the
  // docs, in the nav group that speaks for it, and in whatever an operator
  // bookmarked.
  await gotoPage(page, "/mcp");
  await expect(page).toHaveURL(/\/settings#mcp$/);
  await expect(page.getByRole("heading", { name: "Server Status" })).toBeVisible();

  await expectNoCrash(page);
});
