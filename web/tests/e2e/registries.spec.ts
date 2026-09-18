/** Library, registries tab: the collections a registry offers, and installing
 *  one of them.
 *
 * `/oci` had no end-to-end coverage at all, which is how it kept a Settings
 * sub-tab that saved a cron expression once per keystroke. It is a tab of
 * Library now, and everything it does writes files the deploy path later
 * reads, so what is asserted here is that the writes confirm first.
 */

import { expect, test } from "@playwright/test";
import { expectNoCrash, gotoPage } from "./helpers";

interface Collection {
  name: string;
  version: string;
  registry: string;
  recipe_count: number;
}

interface Registry {
  name: string;
  url: string;
}

test("lists the collections a registry offers", async ({ page, request }) => {
  const response = await request.get("/api/oci/collections");
  expect(response.ok(), "GET /api/oci/collections should succeed").toBeTruthy();
  const collections = (await response.json()) as Collection[];
  expect(collections.length, "simulation should serve collections").toBeGreaterThan(0);

  await gotoPage(page, "/oci");
  await expect(page.getByRole("heading", { name: "What is on disk.", exact: true })).toBeVisible();
  await expect(page.getByRole("tab", { name: /^Registries/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );

  for (const collection of collections) {
    await expect(page.getByText(collection.name, { exact: true }).first()).toBeVisible();
  }
  await expectNoCrash(page);
});

test("lists the registries themselves, beside what they offer", async ({ page, request }) => {
  const response = await request.get("/api/oci/registries");
  expect(response.ok(), "GET /api/oci/registries should succeed").toBeTruthy();
  const registries = (await response.json()) as Registry[];
  expect(registries.length, "simulation should serve registries").toBeGreaterThan(0);

  await gotoPage(page, "/oci");
  await expect(page.getByRole("heading", { name: "Registries", exact: true })).toBeVisible();

  for (const registry of registries) {
    await expect(page.getByText(registry.name, { exact: true }).first()).toBeVisible();
    await expect(page.getByText(registry.url, { exact: true }).first()).toBeVisible();
  }
  // The schedule is configuration, and has left for Settings.
  await expect(page.getByText("The update schedule is configured in Settings.")).toBeVisible();
  await expectNoCrash(page);
});

test("opens a collection and installs one recipe from it", async ({ page, request }) => {
  const collections = (await (await request.get("/api/oci/collections")).json()) as Collection[];
  const collection = collections[0];

  await gotoPage(page, "/oci");
  await page.getByText(collection.name, { exact: true }).first().click();

  // The drawer lists what the collection carries, at the version it is pinned
  // to — which is what an install writes.
  const recipe = page.locator("[data-testid^='collection-recipe-']").first();
  await expect(recipe).toBeVisible();

  const posted = page.waitForResponse(
    (r) => r.url().includes("/api/oci/recipes/install") && r.request().method() === "POST",
  );
  await recipe.getByRole("button", { name: "Install", exact: true }).click();
  const response = await posted;
  expect(response.ok(), "POST /api/oci/recipes/install should succeed").toBeTruthy();
  expect(JSON.parse(response.request().postData() ?? "{}")).toMatchObject({
    collection: collection.name,
    version: collection.version,
  });
  await expectNoCrash(page);
});

/** Installing a whole collection writes every recipe in it, so it asks. */
test("confirms before installing a whole collection", async ({ page, request }) => {
  const collections = (await (await request.get("/api/oci/collections")).json()) as Collection[];
  const collection = collections[0];

  await gotoPage(page, "/oci");
  await page.getByText(collection.name, { exact: true }).first().click();
  await page.getByRole("button", { name: /Install All Recipes/ }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(collection.name);
  await expect(dialog).toContainText(`${collection.recipe_count}`);

  const posted = page.waitForResponse(
    (r) => r.url().includes("/api/oci/install") && r.request().method() === "POST",
  );
  await dialog.getByRole("button", { name: "Install", exact: true }).click();
  expect((await posted).ok(), "POST /api/oci/install should succeed").toBeTruthy();
  await expectNoCrash(page);
});

test("shows what is installed on its own sub-tab", async ({ page }) => {
  await gotoPage(page, "/oci");
  await page.getByRole("tab", { name: /^Installed/ }).click();

  // Either a list of installed recipes or the empty state — never a blank
  // panel, which is what a tab with nothing in it used to render.
  await expect(
    page
      .locator("[data-testid^='installed-']")
      .first()
      .or(page.getByText("No OCI recipes installed")),
  ).toBeVisible();
  await expectNoCrash(page);
});
