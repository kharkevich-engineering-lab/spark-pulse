/** The deploy journey: launch a recipe, see the run, stop it.
 *
 * Kept as one test on purpose. Deployments are process-wide state in the
 * simulation backend, so splitting the journey across tests that Playwright is
 * free to run in parallel would make them depend on each other. The spec
 * clears its own recipe's deployments before and after, so it can run in any
 * order and be re-run on a dirty store.
 */

import { expect, test } from "@playwright/test";
import { escapeRegExp, expectNoCrash, gotoPage, listDeployments, openNav, purgeDeployments } from "./helpers";

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

test("deploys a recipe, shows it on the Runs page and stops it", async ({ page, request }) => {
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
  // The native runtime really starts a container — a simulated one, against
  // the mock Docker service — so the deployment reaches running rather than
  // sitting in a status nothing ever advances. Polled for rather than read
  // once: "running" is written where readiness is *observed*, on the
  // watcher's own thread, and the POST answers before that.
  await expect
    .poll(
      async () => (await listDeployments(request)).find((d) => d.id === deployment.id)?.status,
      { message: "the deployment should reach running" },
    )
    .toBe("running");

  await (await openNav(page)).getByRole("link", { name: "Runs" }).click();
  await expect(page.getByRole("heading", { name: "What is serving.", exact: true })).toBeVisible();

  // A live run is under Live, which is the pill the page opens on.
  await expect(page.getByRole("tab", { name: /^Live/ })).toHaveAttribute("aria-selected", "true");
  const row = page.getByTestId(`deployment-${deployment.id}`);
  await expect(row).toBeVisible();
  await expect(row.getByRole("button", { name: RECIPE_NAME, exact: true })).toBeVisible();
  await expect(row.getByText("Running", { exact: true })).toBeVisible();

  await row.getByRole("button", { name: "Stop", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "Stop this run" })).toBeVisible();
  await dialog.getByRole("button", { name: "Stop", exact: true }).last().click();

  // A stopped run leaves Live for Finished — the record stays, it is just not
  // serving any more.
  await page.getByRole("tab", { name: /^Finished/ }).click();
  await expect(row.getByText("Stopped", { exact: true })).toBeVisible();
  await expectNoCrash(page);

  const after = (await listDeployments(request)).filter((d) => d.recipe_id === RECIPE_ID);
  expect(after.map((d) => d.status)).toEqual(["stopped"]);
});

test("shows the log stream for a run", async ({ page, request }) => {
  const created = await request.post("/api/deployments", {
    data: { recipe_id: RECIPE_ID, name: RECIPE_NAME, params: {} },
  });
  expect(created.ok(), "POST /api/deployments should succeed").toBeTruthy();
  const deployment = (await created.json()) as { id: string; status: string };
  // "starting", not "running": the POST answers once the container is up and
  // the launch script has been exec'd, and readiness is what writes running.
  expect(deployment.status, "the native runtime starts a simulated container").toBe(
    "starting",
  );

  await gotoPage(page, "/jobs");
  const row = page.getByTestId(`deployment-${deployment.id}`);
  await expect(row).toBeVisible();
  // The recipe the run came from is in the expanded detail, not on the row:
  // the row says what is serving, the panel says what it was made from.
  await row.getByRole("button", { name: "Logs" }).click();
  await expect(row.getByText(RECIPE_ID, { exact: true })).toBeVisible();
  await expect(row.getByRole("button", { name: "Hide" })).toBeVisible();
  await expect(row.getByText("Streaming")).toBeVisible();
  await expectNoCrash(page);
});

/** The engine's own metrics on an open row.
 *
 * This is the one place a reader can see that the sampler is actually running:
 * it is started by the app's lifespan, discovers running deployments on its own
 * sweep, and the panel below is fed from the window it keeps in memory. The
 * predecessor of all this — a health monitor — was constructed at startup and
 * never started, so an end-to-end check that a number really arrives is the
 * point of this test rather than a nicety.
 */
test("shows the engine's own metrics for a running run", async ({ page, request }) => {
  const created = await request.post("/api/deployments", {
    data: { recipe_id: RECIPE_ID, name: RECIPE_NAME, params: {} },
  });
  expect(created.ok(), "POST /api/deployments should succeed").toBeTruthy();
  const deployment = (await created.json()) as { id: string };

  await gotoPage(page, "/jobs");
  const row = page.getByTestId(`deployment-${deployment.id}`);
  await row.getByRole("button", { name: "Logs" }).click();

  // Up to one backend sweep plus one UI poll before the first window arrives.
  await expect(row.getByText("Queued", { exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(row.getByText("KV cache", { exact: true })).toBeVisible();
  await expect(row.getByText("Preemptions", { exact: true })).toBeVisible();

  // The window is memory only, and the page says so rather than implying that
  // anything here survives a restart of the control plane. The caption sits on
  // the chart, and a chart needs two samples — so this is one sampler sweep
  // and one UI poll further out than the gauges above.
  await expect(row.getByText(/Restarting Spark Pulse loses this window/)).toBeVisible({
    timeout: 20_000,
  });
  // And it says why it shows no percentile, rather than inventing one from a
  // histogram bucket.
  await expect(row.getByText(/Latency percentiles are not shown/)).toBeVisible();
  await expectNoCrash(page);
});
