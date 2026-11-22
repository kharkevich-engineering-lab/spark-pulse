/** A node's live stats, on the hardware this actually runs on and across the
 *  cluster. Monitoring was a page of its own; `/monitoring` now opens Nodes,
 *  and what a node is doing is on the node's own page, one page per node.
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
import {
  UNIFIED_MEMORY_NODE,
  expectNoCrash,
  gotoPage,
  stubMemoryEndpoints,
} from "./helpers";

/** The stub answers for the registered nodes, so its ids are theirs. */
async function nodeIds(request: import("@playwright/test").APIRequestContext) {
  const nodes = (await (await request.get("/api/nodes")).json()) as { id: string; name: string }[];
  const byName = Object.fromEntries(nodes.map((n) => [n.name, n.id]));
  return { control: byName["spark-01"], peer: byName["spark-02"] };
}

/** Follow a node's name to its page. */
async function openNode(page: import("@playwright/test").Page, name: string) {
  await page.getByTestId("node-registry").getByRole("link", { name, exact: true }).click();
  const detail = page.locator('[data-testid^="node-detail-"]');
  await expect(detail.getByRole("heading", { level: 1, name })).toBeVisible();
  return detail;
}

test("asks every node, and says which one runs the control plane", async ({ page }) => {
  await gotoPage(page, "/monitoring");

  await expect(page.getByRole("heading", { name: "Nodes.", exact: true })).toBeVisible();
  const table = page.getByTestId("node-registry");
  await expect(table.getByRole("row", { name: "spark-01" })).toBeVisible();
  await expect(table.getByRole("row", { name: "spark-02" })).toBeVisible();
  await expect(table.getByText("Control plane", { exact: true })).toHaveCount(1);
  await expect(table.getByText("10.0.0.11", { exact: true })).toBeVisible();

  // Each answers for its own hardware, through its own agent.
  for (const name of ["spark-01", "spark-02"]) {
    const detail = await openNode(page, name);
    await expect(detail.getByText("NVIDIA GB10")).toBeVisible();
    await expect(detail.getByRole("heading", { name: "Host memory and disk" })).toBeVisible();
    await page.goBack();
  }
  await expectNoCrash(page);
});

test("marks a GPU process nothing here started", async ({ page }) => {
  await gotoPage(page, "/monitoring");
  const panel = await openNode(page, "spark-02");

  const process = panel.getByRole("row").filter({ hasText: "untracked" }).first();
  await expect(process).toBeVisible();
  await expect(process.getByRole("button", { name: /^Kill / })).toBeVisible();
  await expectNoCrash(page);
});

test("renders a GPU whose memory usage is not reported", async ({ page, request }) => {
  const { control } = await nodeIds(request);
  await stubMemoryEndpoints(page, { nodes: [{ ...UNIFIED_MEMORY_NODE, id: control }] });
  await gotoPage(page, "/monitoring");
  const panel = await openNode(page, "spark-01");

  await expect(panel.getByText("NVIDIA GB10")).toBeVisible();
  // No GPU memory cell: there is nothing to divide, and 0/0 would have drawn a
  // full or NaN-wide bar. The unified pool is the node's memory cell.
  await expect(panel.getByText("GPU memory", { exact: true })).toHaveCount(0);

  // What nvidia-smi does report is still shown.
  const gpu = UNIFIED_MEMORY_NODE.gpu[0];
  await expect(panel.getByText(`${gpu.utilization}%`, { exact: true })).toBeVisible();
  await expect(panel.getByText(`${gpu.temperature}°C`, { exact: true })).toBeVisible();
  await expect(panel.getByText("—", { exact: true })).toHaveCount(1); // power, neither drawn nor limited
  await expectNoCrash(page);
});

test("renders host memory and disk without a GPU at all", async ({ page, request }) => {
  const { control } = await nodeIds(request);
  // What a CI runner or a laptop reports: no GPU, no processes.
  await stubMemoryEndpoints(page, {
    nodes: [{ ...UNIFIED_MEMORY_NODE, id: control, gpu: [], processes: [] }],
  });
  await gotoPage(page, "/monitoring");
  const panel = await openNode(page, "spark-01");

  await expect(panel.getByRole("heading", { name: "Host memory and disk" })).toBeVisible();
  await expect(panel.getByText(/64\.9% used/)).toBeVisible();
  await expect(panel.getByText("No GPU reported.")).toBeVisible();
  await expectNoCrash(page);
});

test("keeps a node that could not be asked, and says why", async ({ page, request }) => {
  const { control, peer } = await nodeIds(request);
  // Unknown is not idle: the row stays, and it says what happened.
  await stubMemoryEndpoints(page, {
    nodes: [
      { ...UNIFIED_MEMORY_NODE, id: control },
      {
        id: peer,
        name: "spark-02",
        address: "10.0.0.11",
        is_control_plane: false,
        reachable: false,
        error: "10.0.0.11 has no enrolled agent",
        unavailable: [],
        gpu: [],
        cpu: UNIFIED_MEMORY_NODE.cpu,
        disk: [],
        processes: [],
      },
    ],
  });
  await gotoPage(page, "/monitoring");

  await expect(page.getByRole("row", { name: "spark-02" }).getByText("Could not be asked")).toBeVisible();
  const panel = await openNode(page, "spark-02");
  await expect(panel.getByText("10.0.0.11 has no enrolled agent")).toBeVisible();
  await expect(panel.getByRole("heading", { name: "Host memory and disk" })).toHaveCount(0);
  await expectNoCrash(page);
});
