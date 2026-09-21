/** Fleet: what replaced the orchestrator, and what no longer marks it.
 *
 * The cluster orchestrator and its REST surface (`/api/cluster/*`) are gone.
 * A cluster is a deployment of size N, so the page is built on the API that
 * survived it: `/api/nodes` for the machines. What runs on them was a second
 * table here, on its own fifteen-second poll, and it is the Runs page's list
 * now — one list, one poll, one place that can be wrong.
 *
 * The page and its nav entry carried an experimental marking while multi-node
 * had not been run on hardware. Two DGX Sparks have since run and been
 * measured, so neither the note nor the chip is here — the machines are the
 * page's subject and nothing disclaims them.
 */

import { expect, test } from "@playwright/test";
import { expectNoCrash, gotoPage, openNav } from "./helpers";

test("is the nodes, with nothing disclaiming them", async ({ page }) => {
  await gotoPage(page, "/cluster");

  await expect(page.getByRole("heading", { name: "The nodes." })).toBeVisible();
  // The page keeps its own notes; what is gone is the one that disclaimed the
  // machines it is about.
  await expect(
    page.getByRole("note").filter({ hasText: /experimental|unproven/i }),
  ).toHaveCount(0);

  const fleet = (await openNav(page)).getByRole("link", { name: "Fleet" });
  await expect(fleet).toBeVisible();
  await expect(fleet.getByText("exp", { exact: true })).toHaveCount(0);
  await expectNoCrash(page);
});

/* The deployments table that used to be on this page is gone: "what is
 * running on my machines" is a question about runs, and it is answered on
 * Runs, once, where the controls to act on it are. `jobs.spec.ts` and
 * `multinode.spec.ts` assert it there. What is left in its place is the
 * pointer in the page's own description, which is what this asserts — the
 * table's absence alone would still pass on a page that forgot to say where
 * the answer went. */
test("points at Runs instead of listing the deployments a second time", async ({ page }) => {
  await gotoPage(page, "/cluster");

  await expect(page.getByTestId("cluster-deployments")).toHaveCount(0);
  // Scoped to the page: the header nav carries a Runs link of its own.
  await page.getByRole("main").getByRole("link", { name: "Runs" }).click();

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
