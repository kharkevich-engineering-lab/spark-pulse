/** The app shell: one header, five groups, and a menu that fits a phone.
 *
 * Runs under both projects. The desktop project reads the header nav; the
 * mobile one presses the menu button first, which is the whole difference —
 * every assertion below holds at 1280 and at 390.
 */

import { expect, test } from "@playwright/test";
import {
  NAV_GROUPS,
  expectNoCrash,
  expectedNavLabels,
  gotoPage,
  navLabel,
  openNav,
  stubMemoryEndpoints,
} from "./helpers";

test("loads the SPA and renders the shell", async ({ page }) => {
  await gotoPage(page, "/");

  await expect(page.getByRole("link", { name: /Spark Pulse home/ })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Recipes and mods." })).toBeVisible();
  await expectNoCrash(page);
});

test("the nav lists every group and nothing else", async ({ page }) => {
  await gotoPage(page, "/");

  const links = (await openNav(page)).getByRole("link");
  const expected = expectedNavLabels();

  await expect(links).toHaveCount(expected.length);
  expect((await links.allInnerTexts()).map(navLabel)).toEqual(expected);
});

test("every group navigates to its page", async ({ page }) => {
  // The Monitoring page is the one route whose data has to be stubbed; see
  // stubMemoryEndpoints for why.
  await stubMemoryEndpoints(page);
  await gotoPage(page, "/");

  for (const group of NAV_GROUPS) {
    const nav = await openNav(page);
    await nav.getByRole("link", { name: group.label }).click();
    await expect(page).toHaveURL(new RegExp(`${group.href.replace(/\//g, "\\/")}$`));
    await expect(page.getByRole("heading", { name: group.heading, exact: true })).toBeVisible();
    await expectNoCrash(page);
  }
});

/** A group speaks for more than its own route: reading `/monitoring` with
 *  nothing marked is how an operator loses track of where they are. */
test("marks the group a route belongs to, not only the route itself", async ({ page }) => {
  await stubMemoryEndpoints(page);

  for (const [path, label] of [
    ["/monitoring", "Fleet"],
    ["/engines", "Library"],
    ["/mcp", "Settings"],
  ] as const) {
    await gotoPage(page, path);
    const nav = await openNav(page);
    await expect(nav.getByRole("link", { name: label })).toHaveAttribute("aria-current", "page");
  }
});

test("reports the backend version in the brand lockup", async ({ page, request }) => {
  const response = await request.get("/version");
  expect(response.ok()).toBeTruthy();
  const { version } = (await response.json()) as { version: string };

  await gotoPage(page, "/");
  await expect(page.getByText(version, { exact: true })).toBeVisible();
});

/** The old shell put a hamburger at `fixed top-4 left-4` and the user chip at
 *  `fixed top-4 right-4`, both over whatever the page had drawn there. Nothing
 *  in the shell may overlap the page's own title. */
test("the shell does not overlap the page", async ({ page }) => {
  await gotoPage(page, "/");

  const title = page.getByRole("heading", { name: "Recipes and mods." });
  const titleBox = await title.boundingBox();
  expect(titleBox).not.toBeNull();

  for (const control of [
    page.getByRole("link", { name: /Spark Pulse home/ }),
    page.getByRole("button", { name: "Menu" }),
  ]) {
    if (!(await control.isVisible())) continue;
    const box = await control.boundingBox();
    if (!box) continue;
    const overlaps =
      box.x < titleBox!.x + titleBox!.width &&
      box.x + box.width > titleBox!.x &&
      box.y < titleBox!.y + titleBox!.height &&
      box.y + box.height > titleBox!.y;
    expect(overlaps, "a shell control sits on top of the page title").toBe(false);
  }
});

test.describe("the menu", () => {
  test.skip(({ viewport }) => (viewport?.width ?? 0) >= 900, "the menu is under 900px only");

  test("opens, reaches every group, and closes on a choice", async ({ page }) => {
    await gotoPage(page, "/");

    const button = page.getByRole("button", { name: "Menu" });
    await expect(button).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByTestId("mobile-nav")).toHaveCount(0);

    await button.click();
    await expect(button).toHaveAttribute("aria-expanded", "true");
    const nav = page.getByTestId("mobile-nav");
    for (const group of NAV_GROUPS) {
      await expect(nav.getByRole("link", { name: group.label })).toBeVisible();
    }

    await nav.getByRole("link", { name: "Fleet" }).click();
    await expect(page).toHaveURL(/\/cluster$/);
    await expect(page.getByTestId("mobile-nav")).toHaveCount(0);
  });

  test("closes on Escape", async ({ page }) => {
    await gotoPage(page, "/");

    await page.getByRole("button", { name: "Menu" }).click();
    await expect(page.getByTestId("mobile-nav")).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(page.getByTestId("mobile-nav")).toHaveCount(0);
  });
});
