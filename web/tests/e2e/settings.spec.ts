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
  for (const heading of ["Deployment Defaults", "Docker", "Job History", "Engines", "Secrets"]) {
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
  }

  // The form is populated from /api/settings, so every configured value should
  // be sitting in one of its fields.
  const values = await page
    .locator("input")
    .evaluateAll((nodes) => nodes.map((node) => (node as HTMLInputElement).value));

  expect(values).toContain(settings.spark_vllm_path);
  expect(values).toContain(settings.default_container);
  expect(values).toContain(settings.default_engine);
  expect(values).toContain(String(settings.default_gpu_mem_util));
  expect(values).toContain(String(settings.default_port_range_start));
  expect(values).toContain(String(settings.default_port_range_end));

  // Save is inert until something changes, so this spec cannot write to the
  // settings file of whoever is running it.
  await expect(page.getByRole("button", { name: /Save settings/ })).toBeDisabled();
  await expectNoCrash(page);
});
