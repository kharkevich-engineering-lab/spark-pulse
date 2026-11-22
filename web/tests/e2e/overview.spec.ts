/** The overview and the command palette, against the simulated cluster.
 *
 * The overview is home: a card per node with its memory, the live runs and
 * what just happened. The palette is the way in to a deploy from anywhere —
 * it opens the recipe's drawer, it never deploys by itself.
 */

import { expect, test } from "@playwright/test";
import { expectNoCrash, gotoPage, listDeployments, purgeDeployments } from "./helpers";

const RECIPE_ID = "bundled/qwen2.5-0.5b-instruct";
const RECIPE_NAME = "Qwen2.5-0.5B-Instruct";

test.describe.configure({ mode: "default" });

test.beforeEach(async ({ request }) => {
  await purgeDeployments(request, RECIPE_ID);
});

test.afterEach(async ({ request }) => {
  await purgeDeployments(request, RECIPE_ID);
});

test("home is the overview, with a card per node", async ({ page, request }) => {
  const nodes = (await (await request.get("/api/memory")).json()) as { nodes: { id: string; name: string }[] };
  await gotoPage(page, "/");

  await expect(page.getByRole("heading", { name: "Overview.", exact: true })).toBeVisible();
  for (const node of nodes.nodes) {
    await expect(page.getByTestId(`node-card-${node.id}`)).toContainText(node.name);
  }
  await expectNoCrash(page);
});

test("the palette opens a recipe's drawer, and the run shows on the overview", async ({ page, request }) => {
  await gotoPage(page, "/");

  await page.getByRole("button", { name: "Open the command palette" }).click();
  const input = page.getByRole("combobox", { name: "Command palette" });
  await expect(input).toBeFocused();
  await input.fill("0.5b");
  await expect(page.getByRole("option").first()).toContainText(RECIPE_NAME);
  await input.press("Enter");

  await expect(page).toHaveURL(/\/recipes$/);
  const drawerHeading = page.getByRole("heading", { name: RECIPE_NAME, exact: true });
  await expect(drawerHeading).toBeVisible();
  await page.getByRole("button", { name: "Deploy", exact: true }).click();
  await expect(drawerHeading).toHaveCount(0);

  const created = (await listDeployments(request)).filter((d) => d.recipe_id === RECIPE_ID);
  expect(created).toHaveLength(1);

  await gotoPage(page, "/");
  const runs = page.getByTestId("overview-runs");
  await expect(runs.getByRole("link", { name: RECIPE_NAME })).toBeVisible();

  // The run's name links to the run's own page.
  await runs.getByRole("link", { name: RECIPE_NAME }).click();
  await expect(page).toHaveURL(new RegExp(`/jobs/${created[0].id}$`));
  await expect(page.getByTestId(`run-detail-${created[0].id}`)).toBeVisible();
  await expectNoCrash(page);
});

test("the palette closes on Escape and jumps to a page", async ({ page }) => {
  await gotoPage(page, "/cluster");
  await page.keyboard.press("ControlOrMeta+k");
  const input = page.getByRole("combobox", { name: "Command palette" });
  await expect(input).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(input).toHaveCount(0);

  await page.keyboard.press("ControlOrMeta+k");
  await input.fill("settings");
  await input.press("Enter");
  await expect(page).toHaveURL(/\/settings$/);
});
