/** The app shell: it loads, and its nav lists exactly the enabled routes. */

import { expect, test } from "@playwright/test";
import {
  NAV_ITEMS,
  expectNoCrash,
  expectedNavLabels,
  gotoPage,
  navLabel,
  readConfig,
  stubMemoryEndpoints,
} from "./helpers";

test("loads the SPA and renders the sidebar", async ({ page }) => {
  await gotoPage(page, "/");

  await expect(page.getByRole("heading", { name: "Spark Pulse" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Recipes & Mods" })).toBeVisible();
  await expectNoCrash(page);
});

test("nav lists every enabled route and nothing else", async ({ page, request }) => {
  const config = await readConfig(request);
  await gotoPage(page, "/");

  const links = page.getByRole("navigation").getByRole("link");
  const expected = expectedNavLabels(config);

  await expect(links).toHaveCount(expected.length);
  expect((await links.allInnerTexts()).map(navLabel)).toEqual(expected);
});

test("every nav entry navigates to its page", async ({ page, request }) => {
  const config = await readConfig(request);
  // The Monitoring page is the one route whose data has to be stubbed; see
  // stubMemoryEndpoints for why.
  await stubMemoryEndpoints(page);

  const enabled = NAV_ITEMS.filter(
    (item) => item.href !== "/benchmarking" || config.benchmarking_enabled,
  );

  await gotoPage(page, "/");
  const nav = page.getByRole("navigation");

  for (const item of enabled) {
    await nav.getByRole("link", { name: item.label }).click();
    await expect(page).toHaveURL(new RegExp(`${item.href.replace(/\//g, "\\/")}$`));
    await expect(page.getByRole("heading", { name: item.heading, exact: true })).toBeVisible();
    await expectNoCrash(page);
  }
});

test("reports the backend version in the sidebar", async ({ page, request }) => {
  const response = await request.get("/version");
  expect(response.ok()).toBeTruthy();
  const { version } = (await response.json()) as { version: string };

  await gotoPage(page, "/");
  await expect(page.getByText(version, { exact: true })).toBeVisible();
});
