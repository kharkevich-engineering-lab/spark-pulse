/** Recipes page: the catalogue renders and a recipe opens. */

import { expect, test, type APIRequestContext } from "@playwright/test";
import { escapeRegExp, expectNoCrash, gotoPage } from "./helpers";

interface RecipeSummary {
  id: string;
  name: string;
  model: string;
  recipe_version?: string;
  engines?: string[];
  engine_support?: { engine: string; supported: boolean; enabled: boolean }[];
}

async function recipesFromApi(request: APIRequestContext): Promise<RecipeSummary[]> {
  const response = await request.get("/api/recipes");
  expect(response.ok(), "GET /api/recipes should succeed").toBeTruthy();
  return (await response.json()) as RecipeSummary[];
}

function usableEngines(recipe: RecipeSummary): string[] {
  return (recipe.engine_support ?? [])
    .filter((e) => e.supported && e.enabled)
    .map((e) => e.engine);
}

test("lists every recipe the API serves", async ({ page, request }) => {
  const recipes = await recipesFromApi(request);
  expect(recipes.length, "simulation mode should serve bundled recipes").toBeGreaterThan(0);

  await gotoPage(page, "/");

  await expect(page.getByRole("button", { name: `Recipes (${recipes.length})` })).toBeVisible();
  for (const recipe of recipes) {
    await expect(
      page.getByRole("button", { name: new RegExp(escapeRegExp(recipe.name)) }),
    ).toBeVisible();
  }
  await expectNoCrash(page);
});

test("shows both engines on the bundled v2 recipes", async ({ page, request }) => {
  const recipes = await recipesFromApi(request);
  const multiEngine = recipes.filter((r) => usableEngines(r).length > 1);
  expect(
    multiEngine.length,
    "the bundled v2 recipes should declare more than one usable engine",
  ).toBeGreaterThan(0);
  expect(multiEngine.every((r) => r.recipe_version === "2")).toBeTruthy();

  await gotoPage(page, "/");

  for (const recipe of multiEngine) {
    const card = page.getByRole("button", { name: new RegExp(escapeRegExp(recipe.name)) });
    // One badge lists every usable engine, joined with a middle dot.
    await expect(card.getByText(usableEngines(recipe).join(" · "), { exact: true })).toBeVisible();
  }
});

test("opens a recipe drawer and closes it again", async ({ page, request }) => {
  const recipes = await recipesFromApi(request);
  const recipe = recipes.find((r) => r.id === "bundled/qwen2.5-0.5b-instruct") ?? recipes[0];

  await gotoPage(page, "/");
  await page.getByRole("button", { name: new RegExp(escapeRegExp(recipe.name)) }).click();

  const heading = page.getByRole("heading", { name: recipe.name, exact: true });
  await expect(heading).toBeVisible();
  // Drawer body: the recipe form, read-only until "Customize" is pressed.
  await expect(page.getByText("Recipe Name", { exact: true })).toBeVisible();
  await expect(page.getByText("Container", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Deploy", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Customize", exact: true })).toBeVisible();
  await expectNoCrash(page);

  await page.keyboard.press("Escape");
  await expect(heading).toHaveCount(0);
});

test("lists the mods the API serves", async ({ page, request }) => {
  const response = await request.get("/api/mods");
  expect(response.ok(), "GET /api/mods should succeed").toBeTruthy();
  const mods = (await response.json()) as { id: string }[];

  await gotoPage(page, "/");
  await page.getByRole("button", { name: `Mods (${mods.length})` }).click();

  if (mods.length === 0) {
    await expect(page.getByText("No mods found")).toBeVisible();
  }
  for (const mod of mods.slice(0, 3)) {
    await expect(page.getByRole("button", { name: new RegExp(escapeRegExp(mod.id)) })).toBeVisible();
  }
  await expectNoCrash(page);
});
