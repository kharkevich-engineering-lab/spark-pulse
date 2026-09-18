/** Library, models tab: the cached catalogue, a download, and the caches.
 *
 * `/models`, `/cache`, `/engines` and `/oci` were four pages answering one
 * question — what is on this disk. They are one page with three tabs, and the
 * caches are a section of the first rather than a destination of their own, so
 * this spec covers the tab and the section together. The old addresses still
 * resolve: that is what `/cache` is asserted for here.
 */

import { expect, test } from "@playwright/test";
import { expectNoCrash, gotoPage } from "./helpers";

interface ModelEntry {
  id: string;
  size_bytes: number;
  revision: string | null;
}

interface CacheEntry {
  name: string;
  path: string;
  size_bytes: number;
}

interface CacheNode {
  node_id: string;
  name: string;
  reachable: boolean;
  total_bytes: number;
  dirs: CacheEntry[];
}

/** Cards under 900px, a table above it — one layout at a time, never both. */
async function isNarrow(page: import("@playwright/test").Page): Promise<boolean> {
  return (page.viewportSize()?.width ?? 1280) < 900;
}

test("lists the cached model catalogue", async ({ page, request }) => {
  const response = await request.get("/api/models");
  expect(response.ok(), "GET /api/models should succeed").toBeTruthy();
  const { models } = (await response.json()) as { models: ModelEntry[] };
  expect(models.length, "simulation mode should serve a model catalogue").toBeGreaterThan(0);

  await gotoPage(page, "/models");
  await expect(page.getByRole("heading", { name: "What is on disk.", exact: true })).toBeVisible();
  await expect(page.getByRole("tab", { name: /^Models/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );

  for (const model of models) {
    const row = page.getByTestId(`model-${model.id}`);
    await expect(row).toBeVisible();
    await expect(row).toContainText(model.id);
    // One markup for the row, so exactly one control per verb — a table hidden
    // behind CSS beside a card list would name every button twice.
    await expect(page.getByRole("button", { name: `Remove ${model.id}` })).toHaveCount(1);
  }

  // Where the copies are, decided from presence rather than from the catalogue
  // this machine happens to hold.
  await expect(page.getByTestId(`model-${models[0].id}`)).toContainText(/node/);

  if (await isNarrow(page)) {
    await expect(page.getByRole("table")).toHaveCount(0);
  } else {
    await expect(page.getByRole("table").first()).toBeVisible();
  }
  await expectNoCrash(page);
});

test("starts a download and reports its progress to completion", async ({ page }) => {
  // A model id unique to this run, so the spec never collides with a re-run or
  // with another download already in the backend's job list.
  const modelId = `e2e-org/e2e-model-${Date.now().toString(36)}`;

  await gotoPage(page, "/models");
  await page.getByLabel("Model id").fill(modelId);
  await page.getByRole("button", { name: "Download", exact: true }).click();

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

test("offers the configured sources, and says where they are edited", async ({ page, request }) => {
  const response = await request.get("/api/models/sources");
  expect(response.ok(), "GET /api/models/sources should succeed").toBeTruthy();
  const { sources } = (await response.json()) as { sources: { name: string; type: string }[] };

  await gotoPage(page, "/models");

  // The editor moved to Settings; what stays here is the choice a download
  // needs, and one line saying where the list itself is kept.
  await expect(page.getByText("Model sources are configured in Settings.")).toBeVisible();
  const select = page.getByLabel("Source", { exact: true });
  for (const source of sources.filter((s) => s.type === "hf_hub")) {
    await expect(select.locator(`option[value="${source.name}"]`)).toHaveCount(1);
  }
  await expectNoCrash(page);
});

test("keeps the old /cache address, on the caches section, one node at a time", async ({
  page,
  request,
}) => {
  const response = await request.get("/api/cache");
  expect(response.ok(), "GET /api/cache should succeed").toBeTruthy();
  const { nodes } = (await response.json()) as { nodes: CacheNode[] };
  // The simulated fleet is two Sparks, and the section is about which machine
  // is holding the bytes — one node would not exercise that at all.
  expect(nodes.length, "simulation mode should serve two nodes").toBeGreaterThan(1);

  await gotoPage(page, "/cache");
  await expect(page.getByRole("heading", { name: "What is on disk.", exact: true })).toBeVisible();
  await expect(page.getByRole("tab", { name: /^Models/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );

  const caches = page.getByRole("heading", { name: "Caches", exact: true });
  await expect(caches).toBeVisible();
  for (const node of nodes) {
    const section = page.getByTestId(`cache-node-${node.node_id}`);
    await expect(section).toContainText(node.name);
    for (const entry of node.dirs) {
      const card = page.getByTestId(`cache-${node.node_id}-${entry.name}`);
      // The path the *node* resolved, not the `~/` form that was sent to it.
      await expect(card).toContainText(entry.path);
      expect(entry.path.startsWith("~")).toBeFalsy();
    }
  }
  await expectNoCrash(page);
});

test("confirms before emptying one cache, and empties it on the node named", async ({
  page,
  request,
}) => {
  const { nodes } = (await (await request.get("/api/cache")).json()) as { nodes: CacheNode[] };
  // The peer, not the control node: a clean that names no machine is exactly
  // what this section stopped doing, and the control node would hide it.
  const node = nodes.filter((one) => one.reachable).at(-1)!;
  const target = node.dirs.find((entry) => entry.name === "Triton Cache")!;

  await gotoPage(page, "/cache");
  await page
    .getByRole("button", { name: `Clean the ${target.name} cache on ${node.name}` })
    .click();

  // Nothing goes until it is confirmed, and the dialog names the cache and the
  // machine it is on.
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(target.name);
  await expect(dialog).toContainText(node.name);

  const posted = page.waitForResponse(
    (r) => r.url().includes("/api/cache/clean") && r.request().method() === "POST",
  );
  await dialog.getByRole("button", { name: "Clean", exact: true }).click();
  const response = await posted;
  expect(response.ok(), "POST /api/cache/clean should succeed").toBeTruthy();
  expect(JSON.parse(response.request().postData() ?? "{}")).toEqual({
    node: node.node_id,
    name: target.name,
  });
  await expectNoCrash(page);
});

test("backs out of emptying every cache on a node", async ({ page, request }) => {
  const { nodes } = (await (await request.get("/api/cache")).json()) as { nodes: CacheNode[] };
  const node = nodes[0];

  await gotoPage(page, "/models");
  await page
    .getByTestId(`cache-node-${node.node_id}`)
    .getByRole("button", { name: "Clean all on this node" })
    .click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(node.name);
  await expect(dialog).toContainText(/except the downloaded models/);
  await dialog.getByRole("button", { name: "Cancel" }).click();

  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expectNoCrash(page);
});

test("does not scroll sideways at phone width", async ({ page }) => {
  await gotoPage(page, "/models");
  await page.getByTestId(/^model-/).first().waitFor();

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow, "the page body must not scroll horizontally").toBeLessThanOrEqual(1);
});
