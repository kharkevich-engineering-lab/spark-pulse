/** The node registry on the Cluster page.
 *
 * What this replaced: two free-text IP boxes whose contents vanished on
 * refresh. So the properties worth asserting are that the page renders the
 * nodes the backend actually holds, that adding one persists it across a
 * reload, and that forgetting one removes it again.
 *
 * The simulation backend seeds a control node plus one peer, and the specs
 * clean up after themselves over the REST API so they do not depend on each
 * other's ordering.
 */

import { expect, test, type APIRequestContext } from "@playwright/test";
import { expectNoCrash, gotoPage } from "./helpers";

// The simulated registry is one shared object in the backend process, so
// these run one at a time: a spec that adds a node must not see another
// spec's node appear or vanish mid-assertion.
test.describe.configure({ mode: "serial" });

interface Node {
  id: string;
  name: string;
  address: string;
  is_control_plane: boolean;
  ethernet_interface: string;
  infiniband_interfaces: string[];
  state: string;
}

async function listNodes(request: APIRequestContext): Promise<Node[]> {
  const response = await request.get("/api/nodes");
  expect(response.ok(), "GET /api/nodes should succeed").toBeTruthy();
  return (await response.json()) as Node[];
}

/** Forget every node at an address, so a spec starts from a known state. */
async function purgeNode(request: APIRequestContext, address: string): Promise<void> {
  for (const node of await listNodes(request)) {
    if (node.address === address && !node.is_control_plane) {
      await request.delete(`/api/nodes/${node.id}`);
    }
  }
}

test("lists the nodes the backend holds, with interfaces, role and state", async ({
  page,
  request,
}) => {
  const nodes = await listNodes(request);
  expect(nodes.length, "the control node registers itself at startup").toBeGreaterThan(0);

  await gotoPage(page, "/cluster");
  const registry = page.getByTestId("node-registry");
  await expect(registry.getByRole("heading", { name: "Nodes" })).toBeVisible();

  for (const node of nodes) {
    const row = registry.getByRole("row").filter({ hasText: node.name });
    await expect(row).toHaveCount(1);
    if (node.address) await expect(row).toContainText(node.address);
    for (const iface of [node.ethernet_interface, ...node.infiniband_interfaces]) {
      if (iface) await expect(row).toContainText(iface);
    }
  }

  // Exactly one node is the control plane, and it is marked as such.
  const control = nodes.find((n) => n.is_control_plane);
  expect(control, "a control-plane node should exist").toBeTruthy();
  await expect(
    registry.getByRole("row").filter({ hasText: control!.name }),
  ).toContainText("Control plane");

  // Three states, shown as three states — never a bare spinner.
  const peer = nodes.find((n) => !n.is_control_plane);
  if (peer) {
    const expected = { healthy: "Healthy", unknown: "Unknown", dead: "Dead" }[peer.state];
    await expect(
      registry.getByRole("row").filter({ hasText: peer.name }),
    ).toContainText(expected!);
  }

  await expectNoCrash(page);
});

test("keeps the experimental banner", async ({ page }) => {
  await gotoPage(page, "/cluster");
  await expect(
    page.getByRole("note").filter({ hasText: "Multi-node is implemented but unverified" }),
  ).toBeVisible();
  await expectNoCrash(page);
});

test("adds a node by address and keeps it across a reload", async ({ page, request }) => {
  const address = "10.42.0.7";
  await purgeNode(request, address);

  await gotoPage(page, "/cluster");
  const registry = page.getByTestId("node-registry");
  await registry.getByRole("button", { name: "Add node" }).click();

  const dialog = page.getByRole("dialog", { name: "Add node" });
  await expect(dialog).toBeVisible();
  // Manual entry always works: the address field is the primary control.
  await dialog.getByLabel("Address *").fill(address);
  await dialog.getByLabel("Name").fill("spark-e2e");
  await dialog.getByLabel("SSH user").fill("spark");
  await dialog.getByRole("button", { name: "Add node" }).click();
  await expect(dialog).toBeHidden();

  const row = registry.getByRole("row").filter({ hasText: "spark-e2e" });
  await expect(row).toContainText(address);
  await expect(row).toContainText("Peer");

  // It is persisted, not just in React state.
  expect((await listNodes(request)).some((n) => n.address === address)).toBeTruthy();
  await page.reload();
  await expect(
    page.getByTestId("node-registry").getByRole("row").filter({ hasText: "spark-e2e" }),
  ).toBeVisible();

  await expectNoCrash(page);
  await purgeNode(request, address);
});

test("offers discovered peers without ever requiring them", async ({ page, request }) => {
  const address = "10.42.0.8";
  await purgeNode(request, address);

  await gotoPage(page, "/cluster");
  await page.getByTestId("node-registry").getByRole("button", { name: "Add node" }).click();
  const dialog = page.getByRole("dialog", { name: "Add node" });

  await dialog.getByRole("button", { name: "Scan" }).click();

  const discovered = await request.get("/api/nodes/discover?timeout=1");
  const body = (await discovered.json()) as { peers: { address: string }[] };
  if (body.peers.length > 0) {
    await expect(dialog.getByRole("button", { name: new RegExp(body.peers[0].address) })).toBeVisible();
  }

  // Whatever discovery found, typing an address is still the way through.
  await dialog.getByLabel("Address *").fill(address);
  await dialog.getByRole("button", { name: "Add node" }).click();
  await expect(dialog).toBeHidden();
  await expect(
    page.getByTestId("node-registry").getByRole("row").filter({ hasText: address }),
  ).toBeVisible();

  await expectNoCrash(page);
  await purgeNode(request, address);
});

test("forgets a peer but never the control plane", async ({ page, request }) => {
  const address = "10.42.0.9";
  await purgeNode(request, address);
  const created = await request.post("/api/nodes", {
    data: { name: "spark-doomed", address },
  });
  expect(created.ok()).toBeTruthy();

  await gotoPage(page, "/cluster");
  const registry = page.getByTestId("node-registry");
  await expect(registry.getByRole("row").filter({ hasText: "spark-doomed" })).toBeVisible();

  await registry.getByRole("button", { name: "Forget spark-doomed" }).click();
  await page.getByRole("button", { name: "Forget", exact: true }).click();

  await expect(registry.getByRole("row").filter({ hasText: "spark-doomed" })).toHaveCount(0);
  expect((await listNodes(request)).some((n) => n.address === address)).toBeFalsy();

  // The control plane carries no forget button at all.
  const control = (await listNodes(request)).find((n) => n.is_control_plane)!;
  await expect(
    registry.getByRole("button", { name: `Forget ${control.name}` }),
  ).toHaveCount(0);

  await expectNoCrash(page);
});

test("names each diagnostic finding with its remedy", async ({ page, request }) => {
  const response = await request.get("/api/nodes/diagnostics");
  expect(response.ok()).toBeTruthy();
  const { findings } = (await response.json()) as {
    findings: { summary: string; remedy: string }[];
  };

  await gotoPage(page, "/cluster");
  const panel = page.getByTestId("node-diagnostics");

  if (findings.length === 0) {
    await expect(panel).toHaveCount(0);
  } else {
    for (const finding of findings) {
      await expect(panel).toContainText(finding.summary);
      // A finding without a remedy is a mystery, which is the thing this
      // panel exists to stop being.
      await expect(panel).toContainText(finding.remedy.split(".")[0]);
    }
  }
  await expectNoCrash(page);
});
