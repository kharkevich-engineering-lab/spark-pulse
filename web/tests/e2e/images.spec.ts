/** Engine images: what this host has, and what a deploy would have to pull.
 *
 * The page exists to separate two states that both mean "you will wait": an
 * image that was never pulled, and one whose version was republished under a
 * new digest. The spec asserts the page tells them apart.
 */

import { expect, test } from "@playwright/test";
import { expectNoCrash, gotoPage } from "./helpers";

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

function shortDigest(digest: string | null | undefined): string {
  if (!digest) return "—";
  const body = digest.startsWith("sha256:") ? digest.slice(7) : digest;
  return body.slice(0, 12);
}

test("lists every engine image the backend knows about", async ({ page, request }) => {
  const response = await request.get("/api/images");
  expect(response.ok(), "GET /api/images should succeed").toBeTruthy();
  const { images } = (await response.json()) as { images: ImageEntry[] };
  expect(images.length, "simulation mode should serve an image catalogue").toBeGreaterThan(0);

  await gotoPage(page, "/images");
  await expect(page.getByRole("heading", { name: "Engine images", exact: true })).toBeVisible();

  for (const image of images) {
    const row = page.getByTestId(`image-${image.ref}`);
    await expect(row).toBeVisible();
    await expect(row).toContainText(image.repository);
    await expect(row).toContainText(`:${image.tag}`);
    await expect(row).toContainText(image.engine);
    await expect(row).toContainText(image.variant);
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

  await gotoPage(page, "/images");

  for (const image of missing) {
    const row = page.getByTestId(`image-${image.ref}`);
    await expect(row).toContainText("not pulled");
    await expect(row.getByRole("button", { name: `Pull ${image.ref}` })).toBeVisible();
    // Nothing to delete, and no size to report, for an image that is not here.
    await expect(row.getByRole("button", { name: `Delete ${image.ref}` })).toHaveCount(0);
  }

  for (const image of drifted) {
    const row = page.getByTestId(`image-${image.ref}`);
    await expect(row.getByText("present", { exact: true })).toBeVisible();
    await expect(row.getByText("newer digest published", { exact: true })).toBeVisible();
    // The digest column shows local → index, so the two can be compared by eye.
    await expect(row).toContainText(shortDigest(image.local_digest));
    await expect(row).toContainText(shortDigest(image.index_digest));
    await expect(row.getByRole("button", { name: `Pull ${image.ref}` })).toBeVisible();
  }

  const needsAttention = images.filter((i) => i.update_available).length;
  await expect(page.getByText(`${needsAttention} need attention`)).toBeVisible();
  await expectNoCrash(page);
});
