/** The deploy journey: launch a recipe, see the job, stop it.
 *
 * Kept as one test on purpose. Deployments are process-wide state in the
 * simulation backend, so splitting the journey across tests that Playwright is
 * free to run in parallel would make them depend on each other. The spec
 * clears its own recipe's deployments before and after, so it can run in any
 * order and be re-run on a dirty store.
 */

import { expect, test } from "@playwright/test";
import { escapeRegExp, expectNoCrash, gotoPage, listDeployments, purgeDeployments } from "./helpers";

const RECIPE_ID = "bundled/qwen3.8-27b";
const RECIPE_NAME = "Qwen3.8-27B";

// Both tests in this file drive the same recipe's deployments, so they run one
// after another in a single worker rather than in parallel with each other.
test.describe.configure({ mode: "default" });

test.beforeEach(async ({ request }) => {
  await purgeDeployments(request, RECIPE_ID);
});

test.afterEach(async ({ request }) => {
  await purgeDeployments(request, RECIPE_ID);
});

test("deploys a recipe, shows it on the Inference page and stops it", async ({ page, request }) => {
  await gotoPage(page, "/");
  await page.getByRole("button", { name: new RegExp(escapeRegExp(RECIPE_NAME)) }).click();

  const drawerHeading = page.getByRole("heading", { name: RECIPE_NAME, exact: true });
  await expect(drawerHeading).toBeVisible();
  await page.getByRole("button", { name: "Deploy", exact: true }).click();
  // The drawer closes itself once the deployment has been created.
  await expect(drawerHeading).toHaveCount(0);

  const created = (await listDeployments(request)).filter((d) => d.recipe_id === RECIPE_ID);
  expect(created, "the deploy should have created exactly one deployment").toHaveLength(1);
  const deployment = created[0];
  expect(deployment.status).toBe("pending");

  await page.getByRole("navigation").getByRole("link", { name: "Inference" }).click();
  await expect(page.getByRole("heading", { name: "Inference", exact: true })).toBeVisible();

  const row = page.getByTestId(`deployment-${deployment.id}`);
  await expect(row).toBeVisible();
  await expect(row.getByText(RECIPE_NAME, { exact: true })).toBeVisible();
  await expect(row.getByText(RECIPE_ID, { exact: false })).toBeVisible();
  await expect(row.getByText("Pending", { exact: true })).toBeVisible();

  // A pending deployment is cancelled rather than stopped, so the row button
  // and the confirm dialog are both labelled "Cancel".
  await row.getByTitle("Cancel", { exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "Cancel" })).toBeVisible();
  // The dialog renders a dismiss button and then the confirm button; both are
  // labelled "Cancel" for a pending job, and the confirm one comes last.
  await dialog.getByRole("button", { name: "Cancel", exact: true }).last().click();

  await expect(row.getByText("Stopped", { exact: true })).toBeVisible();
  await expectNoCrash(page);

  const after = (await listDeployments(request)).filter((d) => d.recipe_id === RECIPE_ID);
  expect(after.map((d) => d.status)).toEqual(["stopped"]);
});

test("shows the log stream for a deployment", async ({ page, request }) => {
  const created = await request.post("/api/deployments", {
    data: { recipe_id: RECIPE_ID, name: RECIPE_NAME, params: {} },
  });
  expect(created.ok(), "POST /api/deployments should succeed").toBeTruthy();
  const deployment = (await created.json()) as { id: string; status: string };
  expect(deployment.status, "simulation mode should not really launch anything").toBe("pending");

  await gotoPage(page, "/jobs");
  const row = page.getByTestId(`deployment-${deployment.id}`);
  await expect(row).toBeVisible();

  await row.getByText(RECIPE_NAME, { exact: true }).click();
  await expect(row.getByRole("button", { name: "Hide" })).toBeVisible();
  await expect(row.getByText("Streaming")).toBeVisible();
  await expectNoCrash(page);
});
