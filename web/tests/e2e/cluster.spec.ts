/** Fleet: the experimental marking, and what replaced the orchestrator.
 *
 * The cluster orchestrator and its REST surface (`/api/cluster/*`) are gone.
 * A cluster is a deployment of size N, so the page is built on the API that
 * survived it: `/api/nodes` for the machines. What runs on them was a second
 * table here, on its own fifteen-second poll, and it is the Runs page's list
 * now — one list, one poll, one place that can be wrong.
 *
 * Multi-node is implemented but has never run on real hardware, so the page
 * and its nav entry still say so — in one line here, because this page is read
 * rather than acted on. What is unproven and why belongs where an operator is
 * about to deploy across machines: the deploy form and the expanded row on
 * Runs, which have their own specs. `cluster_experimental` in /api/config
 * drives the marking, so it disappears without a code change once a second
 * Spark has verified the list.
 */

import { expect, test } from "@playwright/test";
import { expectNoCrash, gotoPage, openNav, readConfig } from "./helpers";

test("marks the cluster page and its nav entry experimental", async ({ page, request }) => {
  const config = await readConfig(request);
  await gotoPage(page, "/cluster");

  await expect(page.getByRole("heading", { name: "The machines." })).toBeVisible();

  const note = page.getByRole("note").filter({ hasText: "Multi-node is still experimental" });
  // The mark moved to the Fleet group, which is where the cluster now lives.
  const chip = (await openNav(page))
    .getByRole("link", { name: "Fleet" })
    .getByTitle(/has run on two DGX Sparks/i);

  if (config.cluster_experimental) {
    await expect(note).toBeVisible();
    // One line: no list of risks on a page nobody deploys from.
    expect(await note.getByRole("listitem").count()).toBe(0);
    await expect(chip).toBeVisible();
    await expect(chip).toHaveText("exp");
  } else {
    await expect(note).toHaveCount(0);
    await expect(chip).toHaveCount(0);
  }
  await expectNoCrash(page);
});

test("points at Runs instead of listing the deployments a second time", async ({ page }) => {
  await gotoPage(page, "/cluster");

  await expect(page.getByTestId("cluster-deployments")).toHaveCount(0);
  await page.getByRole("link", { name: "See the runs." }).click();

  await expect(page).toHaveURL(/\/jobs$/);
  await expect(page.getByRole("heading", { name: "What is serving." })).toBeVisible();
  await expectNoCrash(page);
});

test("no longer calls the deleted cluster orchestrator endpoints", async ({ page }) => {
  const attempted: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/cluster") || url.pathname === "/sse/cluster") {
      attempted.push(url.pathname);
    }
  });

  await gotoPage(page, "/cluster");
  // The page polls on an interval; give one cycle a chance to fire.
  await page.waitForTimeout(1000);

  expect(attempted, "the SPA still reaches for a deleted endpoint").toEqual([]);
  await expectNoCrash(page);
});
