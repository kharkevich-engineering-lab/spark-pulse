/** Multi-node, end to end against the simulation backend.
 *
 * **None of this is evidence that multi-node works on hardware.** There is one
 * DGX Spark, so what a browser and a simulated backend can show is what is
 * rendered, what is refused, what is recorded, and — the point of this file —
 * that an operator who can reach the feature is told it is unverified.
 *
 * The refusals are asserted through the REST API rather than the form, because
 * a refusal an operator never sees is the failure mode: the deploy form only
 * offers registry nodes, so the only way to ask for an impossible topology is
 * over the API, and that is where the message has to be good.
 */

import { expect, test, type APIRequestContext } from "@playwright/test";
import {
  escapeRegExp,
  expectNoCrash,
  gotoPage,
  listDeployments,
  purgeDeployments,
} from "./helpers";

// The simulated node registry is one shared object in the backend process.
test.describe.configure({ mode: "serial" });

const RECIPE = "bundled/qwen2.5-0.5b-instruct";
const RECIPE_NAME = "Qwen2.5-0.5B-Instruct";

/** The seeded control node and peer. */
const CONTROL = "192.168.1.100";
const PEER = "10.0.0.11";

interface Node {
  id: string;
  address: string;
  is_control_plane: boolean;
}

async function listNodes(request: APIRequestContext): Promise<Node[]> {
  const response = await request.get("/api/nodes");
  expect(response.ok(), "GET /api/nodes should succeed").toBeTruthy();
  return (await response.json()) as Node[];
}

/** Enroll a peer and hand back a callback that forgets it again. */
async function enrol(
  request: APIRequestContext,
  address: string,
): Promise<() => Promise<void>> {
  for (const node of await listNodes(request)) {
    if (node.address === address && !node.is_control_plane) {
      await request.delete(`/api/nodes/${node.id}`);
    }
  }
  const created = await request.post("/api/nodes", {
    data: {
      name: `spark-${address}`,
      address,
      ssh_user: "spark",
      ethernet_interface: "enp1s0",
      infiniband_interfaces: ["ib0", "ib1"],
    },
  });
  expect(created.ok(), `POST /api/nodes ${address}: ${await created.text()}`).toBeTruthy();
  const node = (await created.json()) as Node;
  return async () => {
    await request.delete(`/api/nodes/${node.id}`);
  };
}

async function plan(request: APIRequestContext, nodes: string[], parallel = nodes.length) {
  return request.post("/api/deployments/plan", {
    data: { recipe_id: RECIPE, nodes, params: { tensor_parallel: parallel } },
  });
}

test("a two-node plan renders one rank per machine, and says it is unproven", async ({
  request,
}) => {
  const response = await plan(request, [CONTROL, PEER]);
  expect(response.ok(), await response.text()).toBeTruthy();
  const body = (await response.json()) as {
    node_count: number;
    warnings: string[];
    ranks: { node_rank: number; host: string; command: string; env: Record<string, string> }[];
  };

  expect(body.node_count).toBe(2);
  expect(body.ranks.map((r) => r.node_rank)).toEqual([0, 1]);
  for (const [rank, entry] of body.ranks.entries()) {
    expect(entry.command).toContain("--nnodes 2");
    expect(entry.command).toContain(`--node-rank ${rank}`);
    // Every rank rendezvouses through rank zero.
    expect(entry.command).toContain(`--master-addr ${CONTROL}`);
    // Interface pinning, which a solo plan does not get at all.
    expect(entry.env.NCCL_SOCKET_IFNAME).toBeTruthy();
  }
  expect(body.warnings.join(" ")).toContain("never been run on hardware");
});

test("a solo plan is unchanged: no pinning, no warning", async ({ request }) => {
  const response = await request.post("/api/deployments/plan", {
    data: { recipe_id: RECIPE },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  const body = (await response.json()) as {
    node_count: number;
    warnings: string[];
    ranks: { command: string; env: Record<string, string> }[];
  };

  expect(body.node_count).toBe(1);
  expect(body.warnings).toEqual([]);
  expect(body.ranks[0].command).toContain("--nnodes 1");
  expect(body.ranks[0].env.NCCL_SOCKET_IFNAME).toBeUndefined();
  expect(body.ranks[0].env.GLOO_SOCKET_IFNAME).toBe("lo");
});

test("refuses a topology larger than anything NVIDIA documents", async ({ request }) => {
  const cleanups = [
    await enrol(request, "10.44.0.2"),
    await enrol(request, "10.44.0.3"),
    await enrol(request, "10.44.0.4"),
  ];
  try {
    const response = await plan(request, [CONTROL, PEER, "10.44.0.2", "10.44.0.3", "10.44.0.4"]);
    expect(response.status()).toBe(400);
    const detail = String((await response.json()).detail);
    expect(detail).toContain("nothing above four");
  } finally {
    for (const cleanup of cleanups) await cleanup();
  }
});

test("refuses an address the registry has never seen, by name", async ({ request }) => {
  const response = await plan(request, [CONTROL, "10.99.99.99"]);
  expect(response.status()).toBe(400);
  const detail = String((await response.json()).detail);
  expect(detail).toContain("10.99.99.99");
  expect(detail).toContain("not in the node registry");
});

test("refuses a node count the parallelism does not occupy", async ({ request }) => {
  const response = await plan(request, [CONTROL, PEER], 1);
  expect(response.status()).toBe(400);
  const detail = String((await response.json()).detail);
  expect(detail).toContain("only occupies 1");
});

test("the deploy form marks its node selector, and names the risks once used", async ({
  page,
  request,
}) => {
  const nodes = await listNodes(request);
  test.skip(nodes.length < 2, "the node selector only appears with a peer to select");

  await gotoPage(page, "/");
  await page.getByRole("button", { name: new RegExp(escapeRegExp(RECIPE_NAME)) }).click();
  await expect(page.getByRole("heading", { name: RECIPE_NAME, exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Deploy options" }).click();
  const selector = page.getByTestId("deploy-node-selector");
  await expect(selector).toBeVisible();
  await expect(selector.getByText("exp", { exact: true })).toBeVisible();

  // Solo is not experimental, so nothing shouts until a peer is picked.
  await expect(selector.getByRole("note")).toHaveCount(0);

  const peer = nodes.find((n) => !n.is_control_plane);
  await selector.getByRole("checkbox", { checked: false }).first().check();
  expect(peer, "a peer should have been available to select").toBeTruthy();

  const note = selector.getByRole("note");
  await expect(note).toBeVisible();
  await expect(note).toContainText("Only one DGX Spark exists");
  await expect(note).toContainText("rendezvous forms across machines");
  await expectNoCrash(page);
});

test("a running multi-node deployment is marked wherever it is listed", async ({
  page,
  request,
}) => {
  await purgeDeployments(request, RECIPE);
  const created = await request.post("/api/deployments", {
    data: {
      recipe_id: RECIPE,
      name: "e2e multi-node",
      nodes: [CONTROL, PEER],
      params: { tensor_parallel: 2 },
      skip_preflight: true,
    },
  });
  expect(created.ok(), await created.text()).toBeTruthy();

  try {
    const listed = (await listDeployments(request)).find((d) => d.name === "e2e multi-node");
    expect(listed, "the deployment should be listed").toBeTruthy();

    await gotoPage(page, "/jobs");
    const row = page.getByTestId(`deployment-${listed!.id}`);
    await expect(row).toBeVisible();
    await expect(row).toContainText("2 nodes");
    await expect(row.getByText("exp", { exact: true })).toBeVisible();

    // The Cluster page's own list marks it too.
    await gotoPage(page, "/cluster");
    const clusterRow = page
      .getByTestId("cluster-deployments")
      .getByRole("row")
      .filter({ hasText: "e2e multi-node" });
    await expect(clusterRow.first()).toBeVisible();
    await expect(clusterRow.first().getByText("exp", { exact: true })).toBeVisible();
    await expectNoCrash(page);
  } finally {
    await purgeDeployments(request, RECIPE);
  }
});
