/** Benchmarks: a tab of Runs, and a launcher that opens from the run.
 *
 * The page these replace had a nav entry of its own and a Run form whose first
 * field was a free-text deployment id — an identifier the operator had to
 * carry across from the list of runs sitting on another page. So the two
 * properties worth an end-to-end check are that `/benchmarking` still answers
 * (bookmarks, the MCP tools and the docs all name it) and that the launcher
 * arrives with its target already settled.
 *
 * Every assertion is gated on `benchmarking_enabled` in `/api/config`: with
 * the feature off there is no tab to find, and a spec that assumes it is on
 * would be red on an install that turned it off.
 */

import { expect, test } from "@playwright/test";
import { expectNoCrash, gotoPage, purgeDeployments, readConfig } from "./helpers";

const RECIPE_ID = "bundled/qwen3.8-27b";
const RECIPE_NAME = "Qwen3.8-27B";

test.describe.configure({ mode: "default" });

test.afterEach(async ({ request }) => {
  await purgeDeployments(request, RECIPE_ID);
});

test("puts the benchmarks beside the runs rather than on a page of their own", async ({
  page,
  request,
}) => {
  const config = await readConfig(request);
  await gotoPage(page, "/jobs");
  await expect(page.getByRole("heading", { name: "What is serving." })).toBeVisible();

  const tab = page.getByRole("tab", { name: /^Benchmarks/ });
  if (!config.benchmarking_enabled) {
    await expect(tab).toHaveCount(0);
    return;
  }

  await tab.click();
  // The history and the summary, the two sub-views. "Comparison" was a third
  // pill that showed nothing until two runs had been ticked elsewhere.
  await expect(page.getByRole("tab", { name: /^History/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /^Summary/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Comparison" })).toHaveCount(0);
  await expectNoCrash(page);
});

test("deep-links /benchmarking to that tab", async ({ page, request }) => {
  const config = await readConfig(request);
  await gotoPage(page, "/benchmarking");

  if (!config.benchmarking_enabled) {
    // The feature is off, so the route goes home rather than to a blank tab.
    await expect(page.getByRole("heading", { name: "Recipes and mods." })).toBeVisible();
    return;
  }

  await expect(page.getByRole("heading", { name: "What is serving." })).toBeVisible();
  await expect(page.getByRole("tab", { name: /^Benchmarks/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByRole("tab", { name: /^History/ })).toBeVisible();
  await expectNoCrash(page);
});

test("lists what has been measured, and starts a run from the run itself", async ({
  page,
  request,
}) => {
  const config = await readConfig(request);
  test.skip(!config.benchmarking_enabled, "benchmarking is off on this backend");

  const created = await request.post("/api/deployments", {
    data: { recipe_id: RECIPE_ID, name: RECIPE_NAME, params: {} },
  });
  expect(created.ok(), "POST /api/deployments should succeed").toBeTruthy();
  const deployment = (await created.json()) as { id: string };

  await gotoPage(page, "/jobs");
  const row = page.getByTestId(`deployment-${deployment.id}`);
  await expect(row).toBeVisible();

  // The launcher opens from the run, so the target is settled before the
  // dialog appears — there is no id to type and nothing to mistype.
  await row.getByRole("button", { name: "Benchmark" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Benchmark this run" })).toBeVisible();
  await expect(dialog).toContainText(RECIPE_NAME);
  await expect(dialog.getByPlaceholder("deployment-id")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Run" }).click();
  await expect(dialog).toHaveCount(0);

  // The measurement it just started is in the history.
  await page.getByRole("tab", { name: /^Benchmarks/ }).click();
  await expect(page.getByRole("tab", { name: /^History/ })).toBeVisible();
  await expect(page.getByText(RECIPE_NAME).first()).toBeVisible();
  await expectNoCrash(page);
});
