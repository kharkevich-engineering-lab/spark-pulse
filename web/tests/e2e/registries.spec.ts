/** Library, registries tab: the collections a registry offers, and each
 *  recipe's state in one of them.
 *
 * Everything here writes files the deploy path later reads, so what is
 * asserted is that the page tells the truth about what is installed — the
 * drawer used to offer Install on recipes that already were — and that the
 * writes which could lose an operator's edit ask first.
 *
 * Both projects run against one simulated backend in one process, so a spec
 * that installs something leaves it installed for the next project. The
 * specs that write pick their target from the state the server reports *now*
 * and never confirm an overwrite, so the local-edits recipe is still there for
 * every run.
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
  latest_version: string;
  recipes: RecipeState[];
}

const readState = async (request: APIRequestContext, name: string) => {
  const response = await request.get(`/api/oci/collections/${encodeURIComponent(name)}/state`);
  expect(response.ok(), "GET …/state should succeed").toBeTruthy();
  return (await response.json()) as CollectionState;
};

const openCollection = async (page: Page, name: string) => {
  await gotoPage(page, "/oci");
  await page.getByText(name, { exact: true }).first().click();
  await expect(page.getByTestId("collection-view")).toBeVisible();
};

const row = (page: Page, recipe: RecipeState) =>
  page.getByTestId(`collection-recipe-${recipe.recipe_id}`);

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
  await expect(page.getByRole("heading", { name: "Collections", exact: true })).toBeVisible();

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

test("shows each recipe's state, with at most one action and no uninstall", async ({
  page,
  request,
}) => {
  await openCollection(page, "spark-recipes");
  const state = await readState(request, "spark-recipes");

  // Simulation carries every state; these two survive any run because no
  // spec confirms an overwrite or uninstalls.
  const states = new Set(state.recipes.map((r) => r.state));
  expect(states.has("local_edits"), "a recipe with local edits").toBeTruthy();
  expect(states.has("removed"), "a recipe the collection stopped shipping").toBeTruthy();

  for (const recipe of state.recipes) {
    const el = row(page, recipe);
    await expect(el).toHaveAttribute("data-state", recipe.state);
    expect(await el.getByRole("button").count(), recipe.name).toBeLessThanOrEqual(1);
  }
  await expect(page.getByTestId("collection-view").getByRole("button", { name: /Uninstall/ })).toHaveCount(0);
  // A non-chat recipe is marked as one.
  await expect(page.getByTestId("collection-view").getByTestId("serves-chip").first()).toBeVisible();
  await expectNoCrash(page);
});

test("filters the collection by state", async ({ page, request }) => {
  await openCollection(page, "spark-recipes");
  const state = await readState(request, "spark-recipes");

  await page.getByRole("tab", { name: /^Updates/ }).click();
  const updates = state.recipes.filter((r) => r.update_available);
  await expect(page.locator("[data-testid^='collection-recipe-']")).toHaveCount(updates.length);

  await page.getByRole("tab", { name: /^Not installed/ }).click();
  const missing = state.recipes.filter((r) => r.state === "not_installed");
  await expect(page.locator("[data-testid^='collection-recipe-']")).toHaveCount(missing.length);
  await expectNoCrash(page);
});

test("installs one recipe from the newest version", async ({ page, request }) => {
  const state = await readState(request, "community-recipes");
  const target = state.recipes.find((r) => r.state === "not_installed");
  expect(target, "community-recipes should still have a recipe to install").toBeTruthy();

  await openCollection(page, "community-recipes");
  const posted = page.waitForResponse(
    (r) => r.url().includes("/api/oci/collections/community-recipes/apply") && r.request().method() === "POST",
  );
  await row(page, target!).getByRole("button", { name: "Install", exact: true }).click();
  const response = await posted;
  expect(response.ok(), "POST …/apply should succeed").toBeTruthy();
  expect(JSON.parse(response.request().postData() ?? "{}")).toMatchObject({
    recipes: [target!.name],
    version: state.latest_version,
  });
  // The view re-reads the state and the recipe is installed, with no action.
  await expect(row(page, target!)).toHaveAttribute("data-state", "installed");
  await expect(row(page, target!).getByRole("button")).toHaveCount(0);
  await expectNoCrash(page);
});

/** Updating a recipe the operator edited overwrites the edit, so it asks. */
test("asks before an update overwrites local edits", async ({ page, request }) => {
  const state = await readState(request, "spark-recipes");
  const edited = state.recipes.find((r) => r.state === "local_edits" && r.update_available);
  expect(edited, "simulation carries an edited recipe with an update behind it").toBeTruthy();

  await openCollection(page, "spark-recipes");
  let applied = false;
  page.on("request", (r) => {
    if (r.url().includes("/apply")) applied = true;
  });
  await row(page, edited!).getByRole("button", { name: "Update", exact: true }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(edited!.name);
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toHaveCount(0);
  expect(applied, "nothing is written until the operator confirms").toBe(false);
  await expectNoCrash(page);
});

/** Installing every missing recipe writes many files, so it asks. */
test("confirms before installing all the missing recipes", async ({ page, request }) => {
  const state = await readState(request, "spark-recipes");
  const missing = state.recipes.filter((r) => r.state === "not_installed").length;
  test.skip(missing === 0, "an earlier spec installed everything");

  await openCollection(page, "spark-recipes");
  await page.getByRole("button", { name: `Install all (${missing})` }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(`Install ${missing} recipes from spark-recipes`);
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toHaveCount(0);
  await expectNoCrash(page);
});

/** Update all leaves local edits alone, and says how many it left. */
test("updates all without touching local edits", async ({ page, request }) => {
  const state = await readState(request, "spark-recipes");
  const updates = state.recipes.filter((r) => r.state === "update");
  // The first project applies the update; the second finds nothing to update.
  test.skip(updates.length === 0, "an earlier project already applied the update");
  const edited = state.recipes.filter((r) => r.state === "local_edits" && r.update_available);

  await openCollection(page, "spark-recipes");
  const posted = page.waitForResponse(
    (r) => r.url().includes("/api/oci/collections/spark-recipes/apply") && r.request().method() === "POST",
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
    await expect(row(page, recipe)).toHaveAttribute("data-state", "local_edits");
  }
  await expectNoCrash(page);
});
