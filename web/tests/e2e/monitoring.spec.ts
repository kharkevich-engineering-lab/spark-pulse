/** Monitoring, on the hardware this actually runs on and across the cluster.
 *
 * A GB10 shares one pool of memory between host and GPU, so `nvidia-smi`
 * reports `[N/A]` for GPU memory and the node's agent leaves the measurement
 * absent. The page has to render the card anyway — utilisation, temperature
 * and the process table are still real — instead of dividing by a zero total.
 *
 * The cluster is the other half. Every panel belongs to a node now, because
 * the page used to show whichever machine the control plane was installed on
 * and say nothing about which one that was.
 *
 * The first tests run against the simulated cluster itself: the agent answers
 * for every node, so there is a real path to check. The stubbed ones cover
 * what a simulated Spark cannot produce — a machine with no GPU, and a
 * control plane too old to answer per node.
 */

import { expect, test } from "@playwright/test";
import { UNIFIED_MEMORY_GPU, expectNoCrash, gotoPage, stubMemoryEndpoints } from "./helpers";

test("asks every node, and says which one runs the control plane", async ({ page }) => {
  await gotoPage(page, "/monitoring");

  await expect(page.getByRole("heading", { name: "Monitoring", exact: true })).toBeVisible();
  // The registry's two Sparks, each with its own section.
  await expect(page.getByRole("heading", { name: "spark-01" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "spark-02" })).toBeVisible();
  await expect(page.getByText("Control plane", { exact: true })).toHaveCount(1);
  await expect(page.getByText("10.0.0.11", { exact: true })).toBeVisible();

  // And each answers for its own hardware, through its own agent.
  await expect(page.getByRole("heading", { name: "NVIDIA GB10" })).toHaveCount(2);
  await expect(page.getByRole("heading", { name: "CPU Memory" })).toHaveCount(2);
  await expectNoCrash(page);
});

test("marks a GPU process nothing here started", async ({ page }) => {
  await gotoPage(page, "/monitoring");

  const table = page.getByRole("table").first();
  await expect(table).toBeVisible();
  await expect(table.getByText("untracked").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Kill" }).first()).toBeVisible();
  await expectNoCrash(page);
});

test("renders a GPU whose memory usage is not reported", async ({ page }) => {
  await stubMemoryEndpoints(page);
  await gotoPage(page, "/monitoring");

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

test("keeps the section of a node that could not be asked", async ({ page }) => {
  // Unknown is not idle: the row stays, and it says what happened.
  await stubMemoryEndpoints(page, {
    ...UNIFIED_MEMORY_GPU,
    nodes: [
      {
        id: "control",
        name: "spark-01",
        address: "192.168.1.100",
        is_control_plane: true,
        reachable: true,
        error: null,
        unavailable: [],
        ...UNIFIED_MEMORY_GPU,
      },
      {
        id: "peer",
        name: "spark-02",
        address: "10.0.0.11",
        is_control_plane: false,
        reachable: false,
        error: "10.0.0.11 has no enrolled agent",
        unavailable: [],
        gpu: [],
        cpu: UNIFIED_MEMORY_GPU.cpu,
        disk: [],
        processes: [],
      },
    ],
  });
  await gotoPage(page, "/monitoring");

  await expect(page.getByText("Could not be asked")).toBeVisible();
  await expect(page.getByText("10.0.0.11 has no enrolled agent")).toBeVisible();
  // One machine answered, so exactly one CPU card, not two.
  await expect(page.getByRole("heading", { name: "CPU Memory" })).toHaveCount(1);
  await expectNoCrash(page);
});
