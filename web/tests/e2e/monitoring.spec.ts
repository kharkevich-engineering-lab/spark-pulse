/** Monitoring page, on the hardware this actually runs on.
 *
 * A GB10 shares one pool of memory between host and GPU, so `nvidia-smi`
 * reports `[N/A]` for GPU memory and the backend sets `memory_supported:
 * false`. The page has to render the card anyway — utilisation, temperature
 * and the process table are still real — instead of dividing by a zero total.
 *
 * The payload is stubbed rather than taken from the simulation backend: see
 * `stubMemoryEndpoints` in helpers.ts for the two reasons why.
 */

import { expect, test } from "@playwright/test";
import { UNIFIED_MEMORY_GPU, expectNoCrash, gotoPage, stubMemoryEndpoints } from "./helpers";

test("renders a GPU whose memory usage is not reported", async ({ page }) => {
  await stubMemoryEndpoints(page);
  await gotoPage(page, "/monitoring");

  await expect(page.getByRole("heading", { name: "Monitoring", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "NVIDIA GB10" })).toBeVisible();

  // No usage bar, and an explanation in its place.
  await expect(
    page.getByText("Unified memory — usage not reported by nvidia-smi"),
  ).toBeVisible();
  // No "used / total MB" readout and no "N MB free" line — there is nothing
  // to divide, and 0/0 would have rendered as a full or NaN-wide bar.
  await expect(page.getByText(/^\d+ \/ \d+ MB$/)).toHaveCount(0);
  await expect(page.getByText(/MB free$/)).toHaveCount(0);

  // What nvidia-smi does report is still shown.
  const gpu = UNIFIED_MEMORY_GPU.gpu[0];
  await expect(page.getByText(`${gpu.utilization}%`, { exact: true })).toBeVisible();
  await expect(page.getByText(`${gpu.temperature}°C`, { exact: true }).first()).toBeVisible();
  await expect(page.getByText("— W", { exact: true })).toHaveCount(2); // draw and limit
  await expect(page.getByText(gpu.uuid, { exact: true })).toBeVisible();

  await expectNoCrash(page);
  await expect(page.getByText("No data available.")).toHaveCount(0);
});

test("lists the GPU processes and marks untracked ones", async ({ page }) => {
  await stubMemoryEndpoints(page);
  await gotoPage(page, "/monitoring");

  const process = UNIFIED_MEMORY_GPU.processes[0];
  const table = page.getByRole("table");
  await expect(table).toBeVisible();
  await expect(table.getByRole("cell", { name: String(process.pid) })).toBeVisible();
  await expect(table.getByRole("cell", { name: new RegExp(process.process_name) })).toBeVisible();
  await expect(table.getByText("untracked")).toBeVisible();
  await expect(page.getByRole("button", { name: "Kill" })).toBeVisible();
  await expectNoCrash(page);
});

test("renders host CPU and disk without a GPU at all", async ({ page }) => {
  // What a CI runner or a laptop reports: no GPU, no processes.
  await stubMemoryEndpoints(page, {
    ...UNIFIED_MEMORY_GPU,
    gpu: [],
    processes: [],
  });
  await gotoPage(page, "/monitoring");

  await expect(page.getByRole("heading", { name: "Monitoring", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "CPU Memory" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "/", exact: true })).toBeVisible();
  await expect(page.getByText("64.9%", { exact: true })).toBeVisible();
  await expectNoCrash(page);
});
