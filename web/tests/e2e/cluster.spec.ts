/** Cluster page: the experimental marking, which a config flag drives.
 *
 * Multi-node bring-up has never run on real hardware, so the page and its nav
 * entry say so. `cluster_experimental` in /api/config is what turns both on.
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

test("lists the clusters the backend reports", async ({ page, request }) => {
  const response = await request.get("/api/cluster/list");
  expect(response.ok(), "GET /api/cluster/list should succeed").toBeTruthy();
  const clusters = (await response.json()) as { name: string }[];

  await gotoPage(page, "/cluster");

  if (clusters.length === 0) {
    await expect(page.getByText("No clusters yet.")).toBeVisible();
  } else {
    await expect(page.getByRole("heading", { name: "Clusters" })).toBeVisible();
    for (const cluster of clusters) {
      await expect(page.getByText(cluster.name, { exact: true }).first()).toBeVisible();
    }
  }
  await expect(page.getByRole("button", { name: "New Cluster" })).toBeVisible();
  await expectNoCrash(page);
});
