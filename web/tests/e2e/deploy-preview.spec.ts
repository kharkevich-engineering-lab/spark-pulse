/** Deploy preview: `POST /api/deployments/plan` rendered in the drawer.
 *
 * The preview exists so an operator sees the resolved engine, the exact image
 * and whether it is on the host *before* a deploy silently downloads tens of
 * gigabytes, so those three are what the spec asserts.
 */

import { expect, test } from "@playwright/test";
import { escapeRegExp, expectNoCrash, gotoPage } from "./helpers";

const RECIPE_ID = "bundled/qwen2.5-0.5b-instruct";
const RECIPE_NAME = "Qwen2.5-0.5B-Instruct";

interface DeployPlan {
  engine: string;
  variant: string;
  image_ref: string;
  image_present: boolean;
  image_size_bytes: number;
  model: string;
  port: number;
  launch_command: string;
}

async function openDeployOptions(page: import("@playwright/test").Page) {
  await gotoPage(page, "/");
  await page.getByRole("button", { name: new RegExp(escapeRegExp(RECIPE_NAME)) }).click();
  await expect(page.getByRole("heading", { name: RECIPE_NAME, exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Deploy options" }).click();
}

test("offers every engine that can run the recipe", async ({ page, request }) => {
  const response = await request.get("/api/engines");
  expect(response.ok()).toBeTruthy();
  const { engines } = (await response.json()) as {
    engines: { engine: string; version: string; enabled: boolean }[];
  };
  const enabled = engines.filter((e) => e.enabled);
  expect(enabled.length, "simulation mode should enable at least one engine").toBeGreaterThan(0);

  await openDeployOptions(page);

  const picker = page.getByLabel("Engine", { exact: true });
  await expect(picker).toBeVisible();
  const options = await picker.locator("option").allInnerTexts();
  expect(options[0]).toBe("Recipe default");
  for (const engine of enabled) {
    expect(options).toContain(`${engine.engine} · ${engine.version}`);
  }
});

test("previews a plan with the command, the image and its presence", async ({ page, request }) => {
  const planned = await request.post("/api/deployments/plan", {
    data: { recipe_id: RECIPE_ID, extra_args: [] },
  });
  expect(planned.ok(), "POST /api/deployments/plan should succeed").toBeTruthy();
  const plan = (await planned.json()) as DeployPlan;

  await openDeployOptions(page);
  await page.getByRole("button", { name: "Preview" }).click();

  const rendered = page.getByTestId("deploy-plan");
  await expect(rendered).toBeVisible();

  // Engine and image, exactly as the API resolved them.
  await expect(rendered.getByText(`${plan.engine}/${plan.variant}`, { exact: true })).toBeVisible();
  await expect(rendered.getByText(plan.image_ref, { exact: true })).toBeVisible();
  await expect(rendered.getByText(String(plan.port), { exact: true })).toBeVisible();

  // Whether a deploy would have to download the image first.
  await expect(
    rendered.getByText(plan.image_present ? /^pulled/ : /image not pulled/),
  ).toBeVisible();

  // The command that would actually run.
  const command = rendered.locator("pre");
  await expect(command).toBeVisible();
  await expect(command).toContainText(plan.launch_command);
  await expect(command).toContainText(plan.model);
  await expectNoCrash(page);
});

test("passes extra args through to the previewed command", async ({ page }) => {
  await openDeployOptions(page);

  await page.getByLabel("Extra args").fill("--max-num-seqs 4");
  await page.getByRole("button", { name: "Preview" }).click();

  const rendered = page.getByTestId("deploy-plan");
  await expect(rendered).toBeVisible();
  await expect(rendered.locator("pre")).toContainText("--max-num-seqs 4");
});
