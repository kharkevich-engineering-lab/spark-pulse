/** Recipes, the collections' side: what a registry offers beside what is
 *  installed, and where recipes come from.
 *
 * Everything here writes files the deploy path later reads, so what is
 * asserted is that the page tells the truth about what is installed — the
 * old drawer offered Install on recipes that already were — and that the
 * writes which could lose an operator's edit ask first.
 *
 * Both projects run against one simulated backend in one process, so a spec
 * that installs something leaves it installed for the next project. The
 * specs that write pick their target from the state the server reports *now*
 * and never confirm an overwrite, so the local-edits recipe is still there for
 * every run. A row is a table row on a wide screen and a card under 1200px,
 * so rows are found by what they say, not by what they are.
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
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

interface RecipeState {
  name: string;
  recipe_id: string;
  state: "not_installed" | "installed" | "update" | "local_edits" | "removed";
  update_available: boolean;
}

interface CollectionState {
  collection: string;
  latest_version: string;
  recipes: RecipeState[];
}

const readState = async (request: APIRequestContext, name: string) => {
  const response = await request.get(`/api/oci/collections/${encodeURIComponent(name)}/state`);
  expect(response.ok(), "GET …/state should succeed").toBeTruthy();
  return (await response.json()) as CollectionState;
};

/** Every recipe of every collection, as the server reports it now. */
const readAllStates = async (request: APIRequestContext) => {
  const response = await request.get("/api/oci/collections");
  expect(response.ok(), "GET /api/oci/collections should succeed").toBeTruthy();
  const collections = (await response.json()) as Collection[];
  return Promise.all(collections.map((c) => readState(request, c.name)));
};

/** The row or card a recipe is on, wherever the width puts it. */
const where = (page: Page, recipe: RecipeState) =>
  page.locator(
    `[data-testid="recipe-row-${recipe.recipe_id}"], [data-testid="recipe-line1-${recipe.recipe_id}"]`,
  );

const action = (page: Page, verb: "Install" | "Update", recipe: RecipeState) =>
  page.getByRole("button", { name: `${verb} ${recipe.name}`, exact: true });

test("sends /oci to the sources of Recipes", async ({ page }) => {
  await gotoPage(page, "/oci");

  await expect(page).toHaveURL(/\/recipes#sources$/);
  await expect(page.getByRole("heading", { name: "Recipes.", exact: true })).toBeVisible();
  await expect(page.getByRole("tab", { name: /^Sources/, selected: true })).toBeVisible();
  await expectNoCrash(page);
});

test("lists every source, the registries beside what they offer", async ({ page, request }) => {
  const registries = (await (await request.get("/api/oci/registries")).json()) as Registry[];
  const collections = (await (await request.get("/api/oci/collections")).json()) as Collection[];
  expect(registries.length, "simulation should serve registries").toBeGreaterThan(0);
  expect(collections.length, "simulation should serve collections").toBeGreaterThan(0);

  await gotoPage(page, "/recipes#sources");

  const table = page.getByTestId("recipe-sources-table");
  await expect(table.getByTestId("source-bundled")).toBeVisible();
  await expect(table.getByTestId("source-custom")).toBeVisible();
  for (const registry of registries) {
    await expect(table.getByTestId(`registry-${registry.name}`)).toBeVisible();
    await expect(table.getByText(registry.url, { exact: true }).first()).toBeVisible();
  }
  for (const collection of collections) {
    await expect(table.getByText(collection.name, { exact: true }).first()).toBeVisible();
  }
  // The schedule it is read on lives under the table now.
  await expect(page.getByRole("heading", { name: "Auto-Update" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Add source" })).toBeVisible();
  await expectNoCrash(page);
});

test("offers what the collections carry, each with at most one action", async ({ page, request }) => {
  const states = await readAllStates(request);
  const all = states.flatMap((s) => s.recipes);
  expect(new Set(all.map((r) => r.state)).has("local_edits"), "a recipe with local edits").toBeTruthy();
  expect(new Set(all.map((r) => r.state)).has("removed"), "a recipe the collection stopped shipping").toBeTruthy();

  await gotoPage(page, "/recipes");
  await expect(page.getByRole("tab", { name: /^Available/ })).toBeVisible();

  const chip = {
    not_installed: "library-state-available",
    update: "library-state-update",
    local_edits: "library-state-local",
    removed: "library-state-removed",
    installed: "library-state-on_nodes",
  } as const;
  for (const recipe of all) {
    await expect(where(page, recipe).getByTestId(chip[recipe.state]), recipe.name).toBeVisible();
    const verbs = (await action(page, "Install", recipe).count()) + (await action(page, "Update", recipe).count());
    expect(verbs, recipe.name).toBeLessThanOrEqual(1);
    if (recipe.state === "not_installed") await expect(action(page, "Install", recipe)).toBeVisible();
  }
  // Uninstalling is not here: it is on the installed recipe's own page.
  await expect(page.getByRole("button", { name: /Uninstall/ })).toHaveCount(0);
  await expectNoCrash(page);
});

test("filters the catalogue to what is available and what is behind", async ({ page, request }) => {
  const all = (await readAllStates(request)).flatMap((s) => s.recipes);

  await gotoPage(page, "/recipes");
  await page.getByRole("tab", { name: /^Available/ }).click();
  for (const recipe of all.filter((r) => r.state === "not_installed")) {
    await expect(action(page, "Install", recipe)).toBeVisible();
  }
  await expect(page.getByTestId("library-state-on_nodes")).toHaveCount(0);

  await page.getByRole("tab", { name: /^Updates/ }).click();
  await expect(page.getByTestId("library-state-available")).toHaveCount(0);
  for (const recipe of all.filter((r) => r.state === "update")) {
    await expect(action(page, "Update", recipe)).toBeVisible();
  }
  await expectNoCrash(page);
});

test("installs one recipe from the newest version", async ({ page, request }) => {
  const state = await readState(request, "community-recipes");
  const target = state.recipes.find((r) => r.state === "not_installed");
  expect(target, "community-recipes should still have a recipe to install").toBeTruthy();

  await gotoPage(page, "/recipes");
  const posted = page.waitForResponse(
    (r) => r.url().includes("/api/oci/collections/community-recipes/apply") && r.request().method() === "POST",
  );
  await action(page, "Install", target!).click();
  const response = await posted;
  expect(response.ok(), "POST …/apply should succeed").toBeTruthy();
  expect(JSON.parse(response.request().postData() ?? "{}")).toMatchObject({
    recipes: [target!.name],
    version: state.latest_version,
  });
  // The catalogue re-reads: it is installed now, with nothing to install.
  await expect(action(page, "Install", target!)).toHaveCount(0);
  await expect(where(page, target!).getByTestId("library-state-on_nodes")).toBeVisible();
  await expectNoCrash(page);
});

/** Updating a recipe the operator edited overwrites the edit, so it asks. */
test("asks before an update overwrites local edits", async ({ page, request }) => {
  const all = (await readAllStates(request)).flatMap((s) => s.recipes);
  const edited = all.find((r) => r.state === "local_edits" && r.update_available);
  expect(edited, "simulation carries an edited recipe with an update behind it").toBeTruthy();

  await gotoPage(page, "/recipes");
  let applied = false;
  page.on("request", (r) => {
    if (r.url().includes("/apply")) applied = true;
  });
  await action(page, "Update", edited!).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(edited!.name);
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toHaveCount(0);
  expect(applied, "nothing is written until the operator confirms").toBe(false);
  await expectNoCrash(page);
});

/** Update all leaves local edits alone, and says how many it left. */
test("updates all without touching local edits", async ({ page, request }) => {
  const all = (await readAllStates(request)).flatMap((s) => s.recipes);
  const updates = all.filter((r) => r.state === "update");
  // The first project applies the update; the second finds nothing to update.
  test.skip(updates.length === 0, "an earlier project already applied the update");
  const edited = all.filter((r) => r.state === "local_edits" && r.update_available);

  await gotoPage(page, "/recipes");
  const posted = page.waitForResponse(
    (r) => r.url().includes("/api/oci/collections/") && r.url().includes("/apply") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: `Update all (${updates.length})` }).click();
  const response = await posted;
  expect(response.ok()).toBeTruthy();
  const body = JSON.parse(response.request().postData() ?? "{}");
  expect(body.overwrite_local, "Update all never overwrites").toBeUndefined();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(`${edited.length} skipped for local edits`);
  await dialog.getByRole("button", { name: "OK" }).click();
  for (const recipe of edited) {
    await expect(where(page, recipe).getByTestId("library-state-local")).toBeVisible();
  }
  await expectNoCrash(page);
});
