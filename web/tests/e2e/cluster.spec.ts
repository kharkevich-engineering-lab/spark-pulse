/** Cluster page: the experimental marking, and what replaced the orchestrator.
 *
 * The cluster orchestrator and its REST surface (`/api/cluster/*`) are gone.
 * A cluster is a deployment of size N, so the page is built on the two APIs
 * that survived it: `/api/nodes` for the machines and `/api/deployments` for
 * what runs on them. These specs assert exactly that — the page shows what
 * those endpoints hold, and nothing on it calls an endpoint that no longer
 * exists.
 *
 * Multi-node bring-up has never run on real hardware, so the page and its nav
 * entry still say so. `cluster_experimental` in /api/config drives both.
 */

import { expect, test } from "@playwright/test";
import { expectNoCrash, gotoPage, readConfig } from "./helpers";

test("marks the cluster page and its nav entry experimental", async ({ page, request }) => {
  const config = await readConfig(request);
  await gotoPage(page, "/cluster");

  await expect(page.getByRole("heading", { name: "Cluster Orchestration" })).toBeVisible();

  const banner = page.getByRole("note").filter({ hasText: "Cluster orchestration is experimental" });
  const chip = page.getByRole("navigation").getByRole("link", { name: "Cluster" }).getByTitle(/experimental/i);

  if (config.cluster_experimental) {
    await expect(banner).toBeVisible();
    // The banner says what has and has not been exercised, not just the word.
    await expect(banner).toContainText("never been run on real hardware");
    await expect(chip).toBeVisible();
    await expect(chip).toHaveText("exp");
  } else {
    await expect(banner).toHaveCount(0);
    await expect(chip).toHaveCount(0);
  }
  await expectNoCrash(page);
});

test("shows the deployments the backend reports, with their placement", async ({
  page,
  request,
}) => {
  const response = await request.get("/api/deployments");
  expect(response.ok(), "GET /api/deployments should succeed").toBeTruthy();
  const deployments = (await response.json()) as {
    name: string;
    status: string;
    nodes: string[] | null;
    node_count?: number;
  }[];
  const live = deployments.filter((d) => d.status !== "stopped" && d.status !== "error");

  await gotoPage(page, "/cluster");
  const panel = page.getByTestId("cluster-deployments");
  await expect(panel.getByRole("heading", { name: "Deployments" })).toBeVisible();

  if (live.length === 0) {
    await expect(panel).toContainText("Nothing is running");
  } else {
    for (const deployment of live) {
      const row = panel.getByRole("row").filter({ hasText: deployment.name });
      await expect(row.first()).toBeVisible();
      // A deployment of size one says so as a size, not as a separate mode.
      const ranks = deployment.node_count || deployment.nodes?.length || 1;
      await expect(row.first()).toContainText(String(ranks));
    }
  }
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
