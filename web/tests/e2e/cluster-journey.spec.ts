/** The multi-node journey, walked the way an operator walks it.
 *
 * `nodes.spec.ts` covers the registry on its own, `preflight.spec.ts` the
 * report on its own and `multinode.spec.ts` what the API refuses. What none of
 * them does is *walk the whole thing in a browser*: enrol a peer, pick it in
 * the deploy form, set the shape, preview, deploy, look at the ranks, stop,
 * and meet the refusals on the way. That walk is what ships, so that walk is
 * what this file asserts — and only through what the page renders, never
 * through the API, except where a fact is deliberately not on the page and the
 * comment says so.
 *
 * One fact about this hardware shapes every test here: one GPU per node means
 * the world size *is* the node count, so `tp*pp*dp` has to equal it exactly.
 * Every bundled recipe ships at `tensor_parallel: 1`, so a second node is only
 * reachable if the form can raise the parallelism — it could not, which is why
 * this file used to lean on the one seeded recipe that happened to declare
 * `tp=2` and then override its model to something the catalogue held. It no
 * longer does: the deploy form carries the parallelism controls, ticking a
 * peer proposes the shape that fits, and the journey below is a stock bundled
 * recipe taken to two machines without editing a recipe file.
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import {
  escapeRegExp,
  expectNoCrash,
  gotoPage,
  listDeployments,
  purgeDeployments,
} from "./helpers";

// One simulated node registry and one deployment store, both process-wide.
test.describe.configure({ mode: "serial" });

/** A stock bundled recipe, shipped at `tensor_parallel: 1` like all of them.
 *  Nothing about it is special, which is the point: the journey has to work
 *  for the recipe an operator actually finds on the page. */
const RECIPE_ID = "bundled/qwen2.5-0.5b-instruct";
const RECIPE_NAME = "Qwen2.5-0.5B-Instruct";

/** A model the simulated catalogue holds, big enough that spreading it over
 *  two Sparks is the reason to go multi-node at all. Doubles as the cover for
 *  the Model override field, which nothing else in the e2e suite exercises. */
const CATALOGUED_MODEL = "Intel/Qwen3.5-397B-INT4-AutoRound";

/** The seeded registry: a control node and one peer. */
const CONTROL = "192.168.1.100";
const PEER = "10.0.0.11";
const PEER_NAME = "spark-02";

interface Node {
  id: string;
  name: string;
  address: string;
  is_control_plane: boolean;
}

async function listNodes(request: APIRequestContext): Promise<Node[]> {
  const response = await request.get("/api/nodes");
  expect(response.ok(), "GET /api/nodes should succeed").toBeTruthy();
  return (await response.json()) as Node[];
}

/** Forget every peer at an address, so a test starts from a known registry. */
async function forgetAddress(request: APIRequestContext, address: string): Promise<void> {
  for (const node of await listNodes(request)) {
    if (node.address === address && !node.is_control_plane) {
      await request.delete(`/api/nodes/${node.id}`);
    }
  }
}

/** Open a recipe's deploy drawer and expand the deploy options. */
async function openDeployOptions(page: Page, recipeName: string): Promise<void> {
  await gotoPage(page, "/");
  await page.getByRole("button", { name: new RegExp(escapeRegExp(recipeName)) }).first().click();
  await expect(page.getByRole("heading", { name: recipeName, exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Deploy options" }).click();
  await expect(page.getByTestId("deploy-node-selector")).toBeVisible();
}

/** Set the tensor width by hand, the way an operator overrides the proposal. */
async function setTensorParallel(page: Page, value: string): Promise<void> {
  await page.getByLabel("Tensor parallel").fill(value);
}

/** Tick a peer's box in the deploy form's node selector. */
async function selectNode(page: Page, address: string): Promise<void> {
  await page
    .getByTestId("deploy-nodes")
    .locator("label")
    .filter({ hasText: address })
    .getByRole("checkbox")
    .check();
}

test("enrol, select, preview, deploy, see the ranks, stop", async ({ page, request }) => {
  const enrolled = "10.77.0.5";
  await forgetAddress(request, enrolled);
  await purgeDeployments(request, RECIPE_ID);

  // ── 1. Enrol a peer, and see the registry hold it ────────────────────────
  await gotoPage(page, "/cluster");
  const registry = page.getByTestId("node-registry");
  await registry.getByRole("button", { name: "Add node" }).click();

  const dialog = page.getByRole("dialog", { name: "Add node" });
  await dialog.getByLabel("Address *").fill(enrolled);
  await dialog.getByLabel("Name").fill("spark-journey");
  await dialog.getByLabel("SSH user").fill("spark");
  await dialog.getByRole("button", { name: "Add node" }).click();
  await expect(dialog).toBeHidden();

  const enrolledRow = registry.getByRole("row").filter({ hasText: "spark-journey" });
  await expect(enrolledRow).toContainText(enrolled);
  await expect(enrolledRow).toContainText("Peer");

  // Interface names are not an operator-typed field — they come from the
  // node's own probe — so the row that has them is the peer the backend
  // already knows, and having them is what makes it deployable: NCCL pinning
  // is find-or-fail against exactly these names.
  const peerRow = registry.getByRole("row").filter({ hasText: PEER_NAME });
  await expect(peerRow).toContainText("eth0");
  await expect(peerRow).toContainText("ib0");

  // Forget the one this test enrolled, so the deploy form below offers the
  // seeded peer and nothing else.
  await registry.getByRole("button", { name: "Forget spark-journey" }).click();
  await page.getByRole("button", { name: "Forget", exact: true }).click();
  await expect(registry.getByRole("row").filter({ hasText: "spark-journey" })).toHaveCount(0);

  // ── 2. Choose more than one node, and see the world size ─────────────────
  await openDeployOptions(page, RECIPE_NAME);
  const worldSize = page.getByTestId("deploy-world-size");
  await expect(worldSize).toHaveText("1 node, ranks 0-0");

  // Rank 0 is not a choice: the control node is ticked and cannot be unticked,
  // because the plan assigns ranks by array order and the head is this machine.
  const controlBox = page
    .getByTestId("deploy-nodes")
    .locator("label")
    .filter({ hasText: "control node" })
    .getByRole("checkbox");
  await expect(controlBox).toBeChecked();
  await expect(controlBox).toBeDisabled();

  // Solo, the recipe's own shape occupies the one node it has.
  const occupancy = page.getByTestId("deploy-occupancy");
  await expect(page.getByLabel("Tensor parallel")).toHaveValue("1");
  await expect(occupancy).toContainText("tp=1 pp=1 occupies 1 node");

  await selectNode(page, PEER);
  await expect(worldSize).toHaveText("2 nodes, ranks 0-1");
  // Ticking a peer proposes the shape that fits it. Without this the form
  // offered a topology it could not express: two nodes at tp=1, refused by
  // the server, with no control on the page able to raise it.
  await expect(page.getByLabel("Tensor parallel")).toHaveValue("2");
  await expect(occupancy).toContainText("tp=2 pp=1 occupies 2 nodes");
  // Two machines is the experimental case; one is not.
  await expect(page.getByTestId("deploy-node-selector").getByRole("note")).toBeVisible();

  // Changing your mind puts you back where you started — including the
  // marking, which is what says whether this deploy is the unproven kind.
  await page
    .getByTestId("deploy-nodes")
    .locator("label")
    .filter({ hasText: PEER })
    .getByRole("checkbox")
    .uncheck();
  await expect(worldSize).toHaveText("1 node, ranks 0-0");
  await expect(page.getByTestId("deploy-node-selector").getByRole("note")).toHaveCount(0);
  // Including the shape: a form left asking for two nodes' worth of tensor
  // width on one node is a form that will be refused for the opposite reason.
  await expect(page.getByLabel("Tensor parallel")).toHaveValue("1");
  await selectNode(page, PEER);
  await expect(worldSize).toHaveText("2 nodes, ranks 0-1");
  await expect(page.getByLabel("Tensor parallel")).toHaveValue("2");

  // ── 3. Preview: the plan's node count, and a per-node pre-flight ─────────
  // Point the recipe at weights that are the reason for the second machine.
  await page.getByLabel("Model override").fill(CATALOGUED_MODEL);
  await page.getByRole("button", { name: "Preview" }).click();

  const plan = page.getByTestId("deploy-plan");
  await expect(plan).toBeVisible();
  // The node count reaches the operator as the command that will actually
  // run: two nodes, this rank's number, and the address everything else
  // rendezvouses through.
  const command = plan.locator("pre");
  await expect(command).toContainText("--nnodes 2");
  // The shape the form was showing is the shape that was planned — a preview
  // of some other parallelism is a preview of another deployment.
  await expect(command).toContainText("--tensor-parallel-size 2");
  await expect(command).toContainText(CATALOGUED_MODEL);
  await expect(command).toContainText("--node-rank 0");
  await expect(command).toContainText(`--master-addr ${CONTROL}`);
  await expect(plan).toContainText("never been run on hardware");

  const preflight = page.getByTestId("preflight");
  await expect(preflight).toBeVisible();
  // Per-node: the count says how many nodes were checked and names them, and
  // the one row that is not a pass names the node it is about.
  await expect(preflight).toContainText("across 2 nodes (spark-01, spark-02)");
  const warned = preflight.getByTestId("preflight-check-warn");
  await expect(warned.first()).toContainText(PEER_NAME);
  await expect(page.getByTestId("preflight-verdict")).toHaveText("Ready, but slow");

  // ── 4. Deploy: one deployment, marked as spanning two machines ───────────
  await page.getByRole("button", { name: "Deploy", exact: true }).click();
  await expect(page.getByRole("heading", { name: RECIPE_NAME, exact: true })).toHaveCount(0);

  const created = (await listDeployments(request)).filter((d) => d.recipe_id === RECIPE_ID);
  expect(created, "the deploy should have created exactly one deployment").toHaveLength(1);
  const deployment = created[0];

  // Let the ranks finish coming up before asking a page what is running.
  //
  // Not politeness: `list_deployments` reconciles a record against the
  // containers that exist, and it does that for a record still in `pulling`
  // too — rank 0's container does not exist yet, so the very first listing
  // during the pull window writes the deployment `stopped`. The create
  // overwrites it with `running` a moment later, so what a page shows in
  // between is a flicker rather than the deployment's state. Asserting
  // through it would be asserting on that bug.
  await expect
    .poll(
      async () => {
        const all = await listDeployments(request);
        return all.find((d) => d.id === deployment.id)?.status;
      },
      { message: "the deployment should reach running" },
    )
    .toBe("running");

  // The Cluster page answers "what is running on my machines", so it names
  // the machines rather than counting them.
  await gotoPage(page, "/cluster");
  const clusterRow = page
    .getByTestId("cluster-deployments")
    .getByRole("row")
    .filter({ hasText: RECIPE_NAME })
    .first();
  await expect(clusterRow).toContainText(CONTROL);
  await expect(clusterRow).toContainText(PEER);

  await gotoPage(page, "/jobs");
  const row = page.getByTestId(`deployment-${deployment.id}`);
  await expect(row).toBeVisible();
  await expect(row).toContainText("2 nodes");

  // ── 5. The ranks, with their nodes, rank 0 the head ──────────────────────
  await row.getByText(RECIPE_NAME, { exact: true }).click();
  const rankZero = page.getByTestId("rank-row-0");
  const rankOne = page.getByTestId("rank-row-1");
  await expect(rankZero).toContainText("rank 0");
  await expect(rankZero).toContainText("head");
  await expect(rankZero).toContainText(CONTROL);
  await expect(rankOne).toContainText("rank 1");
  await expect(rankOne).toContainText(PEER);
  // Exactly one head. Two would mean two rendezvous points and a hang.
  await expect(rankOne).not.toContainText("head");

  // ── 6. Stop it, and see every rank torn down ─────────────────────────────
  await row.getByTitle("Stop", { exact: true }).click();
  const stopDialog = page.getByRole("dialog");
  await expect(stopDialog.getByRole("heading", { name: "Stop Deployment" })).toBeVisible();
  await stopDialog.getByRole("button", { name: "Stop", exact: true }).last().click();

  await expect(row.getByText("Stopped", { exact: true })).toBeVisible();
  // The page's own evidence that both ranks went: an orphan is a rank we asked
  // to stop and could not confirm gone, and it is rendered right here when
  // there is one. None means both containers were confirmed removed.
  await expect(page.getByTestId("rank-orphan")).toHaveCount(0);

  // `GET /api/deployments` carries the rank list but not each rank's live
  // container, so per-rank teardown is not on the page to assert — the record
  // is where it lives, and it is checked here rather than left unchecked.
  const detail = await request.get(`/api/deployments/${deployment.id}`);
  const record = (await detail.json()) as {
    ranks: { rank: number; container?: { running: boolean; status: string } }[];
  };
  expect(record.ranks).toHaveLength(2);
  for (const rank of record.ranks) {
    expect(rank.container?.running, `rank ${rank.rank} is still running`).toBe(false);
    expect(rank.container?.status, `rank ${rank.rank} still has a container`).toBe("missing");
  }

  await expectNoCrash(page);
  await purgeDeployments(request, RECIPE_ID);
});

/** The refusal is still reachable — it just takes saying so now.
 *
 *  The proposal is only for a form still showing the recipe's own number; an
 *  operator who types one is telling the form something, and the form must not
 *  quietly replace it. So a shape that genuinely does not fit is exactly what
 *  this test builds: two nodes ticked, tensor width put back to 1 by hand. */
test("says why a two-node run its parallelism cannot fill is refused — three times", async ({
  page,
  request,
}) => {
  await purgeDeployments(request, RECIPE_ID);
  await openDeployOptions(page, RECIPE_NAME);
  await selectNode(page, PEER);
  await expect(page.getByTestId("deploy-world-size")).toHaveText("2 nodes, ranks 0-1");
  await expect(page.getByLabel("Tensor parallel")).toHaveValue("2");

  // Lower it by hand. The form keeps what was typed rather than proposing
  // over the top of it, and says what that shape would do.
  await setTensorParallel(page, "1");
  const occupancy = page.getByTestId("deploy-occupancy");
  await expect(occupancy).toContainText("only occupies 1 of the 2 nodes selected");
  await expect(occupancy).toContainText("the launch would hang");
  await expect(occupancy).toContainText("raise the parallelism until tp*pp is 2");

  // The preview is the second place this is met, and the first that costs a
  // round trip: the server is the authority, and the form does not pretend to
  // be it by disabling the button.
  await page.getByRole("button", { name: "Preview" }).click();
  const previewRefusal = page.getByTestId("deploy-plan-error");
  await expect(previewRefusal).toBeVisible();
  await expect(previewRefusal).toContainText("only occupies 1 of them");
  await expect(previewRefusal).toContainText("raise the parallelism");
  await expect(page.getByTestId("deploy-plan")).toHaveCount(0);

  // And an operator who presses Deploy anyway gets the same sentence, not a
  // status code: what is wrong, what it costs (a launch that hangs), and the
  // two ways out.
  await page.getByRole("button", { name: "Deploy", exact: true }).click();
  const alert = page.getByRole("dialog");
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("only occupies 1");
  await expect(alert).toContainText("the launch would hang");
  await expect(alert).toContainText("raise the parallelism");

  expect(
    (await listDeployments(request)).filter((d) => d.recipe_id === RECIPE_ID),
    "a refused deploy should not leave a record behind",
  ).toHaveLength(0);
  await expectNoCrash(page);
});

/** The mirror image: a shape too big for the machines ticked. Same rule, the
 *  other side of it, and the wording an operator gets has to say which way to
 *  move — this one is fixed by adding a machine, not by raising a number. */
test("says why a shape larger than the nodes selected is refused", async ({ page, request }) => {
  await purgeDeployments(request, RECIPE_ID);
  await openDeployOptions(page, RECIPE_NAME);
  await setTensorParallel(page, "4");

  const occupancy = page.getByTestId("deploy-occupancy");
  await expect(occupancy).toContainText("tp=4 pp=1 does not fit 1 node: it needs 4");
  await expect(occupancy).toContainText("deploy across 4 nodes");

  await page.getByRole("button", { name: "Preview" }).click();
  const refusal = page.getByTestId("deploy-plan-error");
  await expect(refusal).toBeVisible();
  await expect(refusal).toContainText("does not fit 1 node");
  await expect(refusal).toContainText("lower the parallelism");
  await expect(page.getByTestId("deploy-plan")).toHaveCount(0);

  expect(
    (await listDeployments(request)).filter((d) => d.recipe_id === RECIPE_ID),
    "a preview should never create a deployment",
  ).toHaveLength(0);
  await expectNoCrash(page);
});

test("refuses more machines than this hardware has a published topology for", async ({
  page,
  request,
}) => {
  const extras = ["10.77.1.2", "10.77.1.3", "10.77.1.4"];
  for (const address of extras) {
    await forgetAddress(request, address);
    const created = await request.post("/api/nodes", {
      data: { name: `spark-${address}`, address, ssh_user: "spark" },
    });
    expect(created.ok(), await created.text()).toBeTruthy();
  }

  try {
    await openDeployOptions(page, RECIPE_NAME);
    for (const address of [PEER, ...extras]) await selectNode(page, address);
    await expect(page.getByTestId("deploy-world-size")).toHaveText("5 nodes, ranks 0-4");

    await page.getByRole("button", { name: "Deploy", exact: true }).click();
    const alert = page.getByRole("dialog");
    await expect(alert).toBeVisible();
    // multinode.spec.ts asserts the API's wording; this asserts that the
    // operator meets it — named as a hardware limit with a number to act on,
    // not as "invalid" and not as a status code.
    await expect(alert).toContainText("nothing above four");
    await expect(alert).toContainText("at most 4 nodes");
  } finally {
    for (const address of extras) await forgetAddress(request, address);
    await purgeDeployments(request, RECIPE_ID);
  }
  await expectNoCrash(page);
});

test("cannot be asked for a node the registry has never seen", async ({ page, request }) => {
  const peers = (await listNodes(request)).filter((n) => !n.is_control_plane);

  await openDeployOptions(page, RECIPE_NAME);
  const selector = page.getByTestId("deploy-nodes");

  // The refusal an operator would otherwise meet — "not in the node registry"
  // — is unreachable from this form by construction: every node is a tick box
  // over a registry record, and there is nowhere to type an address. That is
  // the fix, so it is what gets asserted here; the API's wording for anything
  // that does ask for one is asserted in multinode.spec.ts.
  await expect(selector.getByRole("textbox")).toHaveCount(0);
  await expect(selector.getByRole("checkbox")).toHaveCount(peers.length + 1);
  for (const peer of peers) await expect(selector).toContainText(peer.address);

  await expectNoCrash(page);
});

/** The last refusal in the journey is the pre-flight *gate*: a `blocked`
 *  report makes `POST /api/deployments` answer 409, and the Recipes page
 *  turns that into the report plus a "Deploy anyway" override rather than a
 *  one-line alert.
 *
 *  It cannot be reached from a browser here. The simulated pre-flight runs the
 *  real checks against an invented DGX Spark that always answers, and the one
 *  seam that makes a node unreachable — `mock.preflight.UNREACHABLE` — is a
 *  module-level set with no REST surface: "tests add to it; nothing else
 *  does", and those tests are the Python ones (`tests/test_router_preflight.py`
 *  is where a blocked verdict is produced). Faking a 409 by routing the request
 *  in the browser would assert the fixture, not the gate, so the gate is left
 *  there, and the panel it renders to `src/tests/components/PreflightPanel.test.tsx`. */
test.skip("shows the pre-flight gate and its override", () => {});

test("refuses a duplicate address where it was typed", async ({ page }) => {
  await gotoPage(page, "/cluster");
  const registry = page.getByTestId("node-registry");
  await registry.getByRole("button", { name: "Add node" }).click();

  const dialog = page.getByRole("dialog", { name: "Add node" });
  await dialog.getByLabel("Address *").fill(PEER);
  await dialog.getByRole("button", { name: "Add node" }).click();

  // The dialog stays open holding what was typed, with the reason next to it —
  // a closed dialog and a lost address is how an operator ends up registering
  // the same machine twice under two names.
  await expect(dialog.getByRole("alert")).toContainText("already registered");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("Address *")).toHaveValue(PEER);

  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(registry.getByRole("row").filter({ hasText: PEER })).toHaveCount(1);
  await expectNoCrash(page);
});
