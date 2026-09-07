/** Settings page: the engine registry and the live configuration.
 *
 * Read-only on purpose: saving here writes ~/.config/spark-pulse/settings.json
 * on the machine running the suite, which would change what every other spec
 * sees.
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

interface Settings {
  spark_vllm_path: string;
  default_container: string;
  default_gpu_mem_util: number;
  default_port_range_start: number;
  default_port_range_end: number;
  default_engine: string;
  docker: { shm_size_gb: number; pids_limit: number };
}

test("shows the engines the registry knows about", async ({ page, request }) => {
  const response = await request.get("/api/engines");
  expect(response.ok(), "GET /api/engines should succeed").toBeTruthy();
  const { engines, default_engine } = (await response.json()) as {
    engines: EngineSummary[];
    default_engine: string;
  };
  expect(engines.length, "simulation mode should serve an engine registry").toBeGreaterThan(0);

  await gotoPage(page, "/settings");
  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
  // The engine list lives on its own tab now; nothing is asserted about it
  // until that tab is the one on screen.
  await page.getByRole("tab", { name: "Engines" }).click();
  await expect(page.getByRole("heading", { name: "Engines", exact: true })).toBeVisible();

  for (const engine of engines) {
    const reference = engine.digest
      ? `${engine.image}@${engine.digest.slice(0, 19)}…`
      : engine.image_ref;
    // One list entry per engine; the image reference identifies it uniquely.
    const item = page.getByRole("listitem").filter({ hasText: reference });
    await expect(item).toHaveCount(1);

    const label =
      engine.variant === "default" ? engine.engine : `${engine.engine} · ${engine.variant}`;
    await expect(item).toContainText(label);
    await expect(item.getByText(`v${engine.version}`, { exact: true })).toBeVisible();
    await expect(item).toContainText(`:${engine.ports.api}`);

    // The default engine carries a "default" chip, and only it does.
    const isDefault = engine.engine === default_engine && engine.variant === "default";
    await expect(item.getByText("default", { exact: true })).toHaveCount(isDefault ? 1 : 0);
  }
  await expectNoCrash(page);
});

test("shows the configuration the backend is running with", async ({ page, request }) => {
  const response = await request.get("/api/settings");
  expect(response.ok(), "GET /api/settings should succeed").toBeTruthy();
  const settings = (await response.json()) as Settings;

  await gotoPage(page, "/settings");

  // Every tab renders, and each is reached by name.
  for (const tab of ["Deployment", "Containers", "Cluster", "Engines", "Secrets", "Environment"]) {
    await expect(page.getByRole("tab", { name: tab, exact: true })).toBeVisible();
  }

  // The form is populated from /api/settings, so every configured value should
  // be sitting in a field on the tab it belongs to.
  const valuesOnScreen = async () =>
    page
      .locator("input")
      .evaluateAll((nodes) => nodes.map((node) => (node as HTMLInputElement).value));

  await expect(page.getByRole("heading", { name: "Deployment Defaults", exact: true })).toBeVisible();
  const deployment = await valuesOnScreen();
  expect(deployment).toContain(settings.spark_vllm_path);
  expect(deployment).toContain(settings.default_container);
  expect(deployment).toContain(String(settings.default_gpu_mem_util));
  expect(deployment).toContain(String(settings.default_port_range_start));
  expect(deployment).toContain(String(settings.default_port_range_end));

  // The Docker block used to render its defaults from literals in the page, so
  // it showed numbers the backend had never heard of. It comes from the API now.
  await page.getByRole("tab", { name: "Containers" }).click();
  await expect(page.getByRole("heading", { name: "Container Limits", exact: true })).toBeVisible();
  const containers = await valuesOnScreen();
  expect(containers).toContain(String(settings.docker.shm_size_gb));
  expect(containers).toContain(String(settings.docker.pids_limit));

  await page.getByRole("tab", { name: "Engines" }).click();
  expect(await valuesOnScreen()).toContain(settings.default_engine);

  // Read-only by design: what this tab reports is exactly what a browser must
  // not be able to change.
  await page.getByRole("tab", { name: "Environment" }).click();
  await expect(page.getByRole("heading", { name: "Access", exact: true })).toBeVisible();
  expect(await page.locator("input").count()).toBe(0);
  await expect(page.getByRole("button", { name: /Save settings/ })).toHaveCount(0);

  await page.getByRole("tab", { name: "Deployment" }).click();

  // Save is inert until something changes, so this spec cannot write to the
  // settings file of whoever is running it.
  await expect(page.getByRole("button", { name: /Save settings/ })).toBeDisabled();
  await expectNoCrash(page);
});
