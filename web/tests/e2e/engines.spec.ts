/** Library, engines tab: what this cluster can run, and what a deploy waits on.
 *
 * The tab exists to separate two states that both mean "you will wait": an
 * image that was never pulled, and one whose version was republished under a
 * new digest. The spec asserts it tells them apart — and, since the Images
 * page and the Engines settings tab became one row, that an engine's own facts
 * sit beside its image's size. Where engines *come from* is configuration and
 * is the page's Sources section.
 */

import { expect, test } from "@playwright/test";
import { expectNoCrash, gotoPage } from "./helpers";

interface EngineSummary {
  engine: string;
  variant: string;
  version: string;
  image: string;
  image_ref: string;
  digest: string | null;
  enabled: boolean;
  ports: { api: number; rendezvous?: number | null };
}

interface ImageEntry {
  ref: string;
  repository: string;
  tag: string;
  engine: string;
  variant: string;
  present: boolean;
  digest_drift: boolean;
  update_available: boolean;
  local_digest: string;
  index_digest: string;
}

test("shows the engines the registry knows about", async ({ page, request }) => {
  const response = await request.get("/api/engines");
  expect(response.ok(), "GET /api/engines should succeed").toBeTruthy();
  const { engines } = (await response.json()) as { engines: EngineSummary[] };
  expect(engines.length, "simulation mode should serve an engine registry").toBeGreaterThan(0);

  await gotoPage(page, "/engines");
  await expect(page.getByRole("heading", { name: "Engines.", exact: true })).toBeVisible();

  for (const engine of engines) {
    // The row is keyed by the image reference, which is what the catalogue and
    // the registry already agree on.
    const row = page.getByTestId(`engine-${engine.image_ref}`);
    if ((await row.count()) === 0) continue; // an engine with no catalogue entry
    const label =
      engine.variant === "default" ? engine.engine : `${engine.engine} · ${engine.variant}`;
    await expect(row).toContainText(label);
    await expect(row).toContainText(`v${engine.version}`);

    // Opening the row puts its capabilities and ports in a strip under it.
    const toggle = row.getByRole("button", { name: `Details for ${engine.image_ref}` });
    await toggle.click();
    const detail = page.getByTestId(`engine-detail-${engine.image_ref}`);
    await expect(detail.getByTestId(`presence-${engine.image_ref}`)).toBeVisible();
    await expect(detail.getByText(`:${engine.ports.api}`, { exact: false }).first()).toBeVisible();
    await toggle.click();
    await expect(detail).toHaveCount(0);
  }

  await expectNoCrash(page);
});

test("lists every engine image the backend knows about", async ({ page, request }) => {
  const response = await request.get("/api/images");
  expect(response.ok(), "GET /api/images should succeed").toBeTruthy();
  const { images } = (await response.json()) as { images: ImageEntry[] };
  expect(images.length, "simulation mode should serve an image catalogue").toBeGreaterThan(0);

  await gotoPage(page, "/engines");

  for (const image of images) {
    const row = page.getByTestId(`engine-${image.ref}`);
    await expect(row).toBeVisible();
    await expect(row).toContainText(image.repository);
    await expect(row).toContainText(`:${image.tag}`);
    await expect(row).toContainText(image.engine);
    // "default" is the absence of a variant, so the badge leaves it unsaid —
    // the rule `EngineBadge` has always applied, now that the badge is what
    // this tab renders.
    if (image.variant !== "default") await expect(row).toContainText(image.variant);
  }
  await expectNoCrash(page);
});

test("distinguishes a missing image from a republished digest", async ({ page, request }) => {
  const response = await request.get("/api/images");
  const { images } = (await response.json()) as { images: ImageEntry[] };

  const missing = images.filter((i) => !i.present);
  const drifted = images.filter((i) => i.present && i.digest_drift);
  expect(missing.length, "simulation should include an image this host lacks").toBeGreaterThan(0);
  expect(drifted.length, "simulation should include an image with digest drift").toBeGreaterThan(0);

  await gotoPage(page, "/engines");

  for (const image of missing) {
    const row = page.getByTestId(`engine-${image.ref}`);
    await expect(row).toContainText("Available");
    await expect(row.getByRole("button", { name: `Pull ${image.ref}` })).toBeVisible();
    // Nothing to delete, and no size to report, for an image that is not here.
    await expect(row.getByRole("button", { name: `Delete ${image.ref}` })).toHaveCount(0);
  }

  for (const image of drifted) {
    const row = page.getByTestId(`engine-${image.ref}`);
    // One dot and a word; why is on the hover, and Pull is in the same row.
    const status = row.getByText("Update", { exact: true });
    await expect(status).toBeVisible();
    await expect(row.getByTitle(/newer digest is published/)).toBeVisible();
    await expect(row.getByRole("button", { name: `Pull ${image.ref}` })).toBeVisible();
  }

  // The filter counts what a pull would change: a republished digest.
  await expect(page.getByRole("tab", { name: `Updates (${drifted.length})` })).toBeVisible();
  await expectNoCrash(page);
});

/** Where engines come from is the page's second section, in the address. */
test("shows the engine indexes under Sources, and the section is the URL hash", async ({ page }) => {
  await gotoPage(page, "/engines");
  await page.getByRole("tab", { name: /^Sources/ }).click();
  await expect(page).toHaveURL(/\/engines#sources$/);
  await expect(page.getByTestId("engine-sources")).toBeVisible();
  await expect(page.getByLabel("Default engine")).toBeVisible();
  await expect(page.getByTestId("engines-table")).toHaveCount(0);

  await page.getByRole("button", { name: "Add index" }).click();
  await page.getByLabel(/^Index \d+$/).last().fill("ghcr.io/example/engines:latest");
  await expect(page.getByText("Unsaved changes on this page.")).toBeVisible();
  await page.getByRole("button", { name: "Discard" }).click();
  await expect(page.getByText("Unsaved changes on this page.")).toHaveCount(0);
  await expectNoCrash(page);
});

/** Deleting an image is tens of minutes to undo, so it names what it costs
 *  before it does anything — and, on a cluster, which machines it touches. */
test("asks before deleting an image, and says what re-pulling costs", async ({ page, request }) => {
  const { images } = (await (await request.get("/api/images")).json()) as { images: ImageEntry[] };
  const present = images.find((i) => i.present);
  expect(present, "simulation should include an image this host holds").toBeTruthy();

  await gotoPage(page, "/engines");
  await page.getByRole("button", { name: `Delete ${present!.ref}` }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(/Re-pulling it can take tens of minutes/);
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expectNoCrash(page);
});

test("does not scroll sideways at phone width", async ({ page }) => {
  await gotoPage(page, "/engines");
  await page.getByTestId(/^engine-/).first().waitFor();

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow, "the page body must not scroll horizontally").toBeLessThanOrEqual(1);
});
