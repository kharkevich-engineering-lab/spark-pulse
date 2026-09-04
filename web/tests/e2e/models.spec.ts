/** Models page: the cached catalogue, and a download that reports progress. */

import { expect, test } from "@playwright/test";
import { expectNoCrash, gotoPage } from "./helpers";

interface ModelEntry {
  id: string;
  size_bytes: number;
  revision: string | null;
}

test("lists the cached model catalogue", async ({ page, request }) => {
  const response = await request.get("/api/models");
  expect(response.ok(), "GET /api/models should succeed").toBeTruthy();
  const { models } = (await response.json()) as { models: ModelEntry[] };
  expect(models.length, "simulation mode should serve a model catalogue").toBeGreaterThan(0);

  await gotoPage(page, "/models");
  await expect(page.getByRole("heading", { name: "Models", exact: true })).toBeVisible();

  const table = page.getByRole("table");
  await expect(table).toBeVisible();
  for (const model of models) {
    await expect(table.getByRole("cell", { name: model.id, exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: `Delete ${model.id}` })).toBeVisible();
  }
  await expectNoCrash(page);
});

test("starts a download and reports its progress to completion", async ({ page }) => {
  // A model id unique to this run, so the spec never collides with a re-run or
  // with another download already in the backend's job list.
  const modelId = `e2e-org/e2e-model-${Date.now().toString(36)}`;

  await gotoPage(page, "/models");
  await page.getByLabel("Model id").fill(modelId);
  await page.getByRole("button", { name: "Download" }).click();

  // The job appears with a progress bar wired to the download's byte counts.
  // `.first()`: the POST response and the SSE "queued" frame can both add the
  // job, so the row is sometimes rendered twice.
  const bar = page.getByRole("progressbar", { name: `${modelId} progress` }).first();
  await expect(bar).toBeVisible();

  const job = page.locator("[data-testid^='job-']").filter({ hasText: modelId }).first();
  await expect(job).toBeVisible();
  // Progress arrives over /sse/models; the mock walks the job to completion.
  await expect(job).toContainText("completed", { timeout: 20_000 });
  await expect(bar).toHaveAttribute("aria-valuenow", "100");
  // The byte counter ends at the full estimated size, not "0 B / ?".
  await expect(job).not.toContainText("/ ?");
  await expectNoCrash(page);
});

test("shows the configured model sources", async ({ page, request }) => {
  const response = await request.get("/api/models/sources");
  expect(response.ok(), "GET /api/models/sources should succeed").toBeTruthy();
  const { sources } = (await response.json()) as { sources: { name: string; type: string }[] };

  await gotoPage(page, "/models");
  await expect(page.getByRole("heading", { name: "Model sources" })).toBeVisible();

  if (sources.length === 0) {
    await expect(page.getByText("No sources configured.")).toBeVisible();
    return;
  }
  for (const [index, source] of sources.entries()) {
    await expect(page.getByLabel(`Source ${index + 1} name`)).toHaveValue(source.name);
    await expect(page.getByLabel(`Source ${index + 1} type`)).toHaveValue(source.type);
  }
});
