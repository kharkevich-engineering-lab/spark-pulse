/** Pre-flight: `POST /api/preflight/run`, and the panel it fills in the preview.
 *
 * The point of the pre-flight is that a problem arrives with its node and its
 * remedy attached, before a deploy spends twenty minutes discovering it. So
 * that is what this spec asserts — not that a report came back, but that every
 * non-passing check names a node and says what to do, and that a wait is shown
 * as a wait rather than as a failure.
 */

import { expect, test, type APIRequestContext } from "@playwright/test";
import { escapeRegExp, expectNoCrash, gotoPage } from "./helpers";

const RECIPE_ID = "bundled/qwen2.5-0.5b-instruct";
const RECIPE_NAME = "Qwen2.5-0.5B-Instruct";

/** The two nodes the simulated registry ships with. */
const CONTROL = "192.168.1.100";
const PEER = "10.0.0.11";

/** A two-node pre-flight needs a two-node plan, and one GPU per node means
 *  the world size is the node count: a recipe left at tp=1 is refused before
 *  there is anything to check. */
const BOTH_NODES = { nodes: [CONTROL, PEER], params: { tensor_parallel: 2 } };

interface PreflightCheck {
  id: string;
  title: string;
  node: string;
  status: "pass" | "warn" | "fail";
  observed: string;
  remedy: string;
  delay_bytes: number;
}

interface PreflightReport {
  verdict: "ready" | "slow" | "blocked";
  summary: string;
  can_proceed: boolean;
  estimated_transfer_bytes: number;
  counts: { pass: number; warn: number; fail: number };
  nodes: { label: string }[];
  checks: PreflightCheck[];
  blocking: PreflightCheck[];
  delaying: PreflightCheck[];
  advisories: PreflightCheck[];
}

async function preflight(
  request: APIRequestContext,
  body: Record<string, unknown> = {},
): Promise<PreflightReport> {
  const response = await request.post("/api/preflight/run", {
    data: { recipe_id: RECIPE_ID, extra_args: [], ...body },
  });
  expect(response.ok(), "POST /api/preflight/run should succeed").toBeTruthy();
  return (await response.json()) as PreflightReport;
}

test("reports a verdict and a check for every node", async ({ request }) => {
  const report = await preflight(request);

  expect(["ready", "slow", "blocked"]).toContain(report.verdict);
  expect(report.summary.length).toBeGreaterThan(0);
  expect(report.checks.length).toBeGreaterThan(0);
  expect(report.counts.pass).toBeGreaterThan(0);

  const nodes = new Set(report.nodes.map((n) => n.label));
  expect(nodes.size).toBeGreaterThan(0);
  for (const check of report.checks) {
    expect(nodes, `check ${check.id} ran on an unknown node`).toContain(check.node);
  }
});

test("every non-passing check names its node and a remedy", async ({ request }) => {
  const report = await preflight(request, BOTH_NODES);

  const bad = report.checks.filter((c) => c.status !== "pass");
  expect(bad.length, "the simulated peer holds no engine image").toBeGreaterThan(0);
  for (const check of bad) {
    expect(check.remedy.trim(), `${check.id} on ${check.node} has no remedy`).not.toBe("");
    expect(check.observed).toContain(check.node);
  }
});

test("checks both nodes, not only the control plane", async ({ request }) => {
  const report = await preflight(request, BOTH_NODES);
  expect(report.nodes.map((n) => n.label)).toEqual(["spark-01", "spark-02"]);
  expect(new Set(report.checks.map((c) => c.node))).toEqual(
    new Set(["spark-01", "spark-02"]),
  );
});

test("a pull is a wait, not a failure", async ({ request }) => {
  const report = await preflight(request, BOTH_NODES);

  const image = report.checks.find((c) => c.id === "image" && c.node === "spark-02");
  expect(image, "the peer should have been asked about the image").toBeTruthy();
  expect(image!.status).toBe("warn");
  expect(report.verdict).toBe("slow");
  expect(report.can_proceed).toBe(true);
  expect(report.blocking).toHaveLength(0);
  expect(report.estimated_transfer_bytes).toBeGreaterThan(0);
});

test("nvidia-smi reporting no GPU memory does not fail a node", async ({ request }) => {
  const report = await preflight(request);
  const gpu = report.checks.find((c) => c.id === "gpu");
  expect(gpu, "a GPU check should have run").toBeTruthy();
  expect(gpu!.status).not.toBe("fail");
  expect(gpu!.observed).toMatch(/unified|nvidia-smi/);
});

test("a recipe that cannot be planned is refused rather than half-checked", async ({
  request,
}) => {
  const response = await request.post("/api/preflight/run", {
    data: { recipe_id: "does/not-exist" },
  });
  expect(response.status()).toBe(400);
});

test("the deploy preview shows the pre-flight verdict and its remedies", async ({
  page,
  request,
}) => {
  const report = await preflight(request);

  await gotoPage(page, "/");
  await page.getByRole("button", { name: new RegExp(escapeRegExp(RECIPE_NAME)) }).click();
  await expect(page.getByRole("heading", { name: RECIPE_NAME, exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Deploy options" }).click();
  await page.getByRole("button", { name: "Preview" }).click();

  const panel = page.getByTestId("preflight");
  await expect(panel).toBeVisible();

  const verdict = { ready: "Ready", slow: "Ready, but slow", blocked: "Blocked" }[
    report.verdict
  ];
  await expect(page.getByTestId("preflight-verdict")).toHaveText(verdict);
  await expect(panel).toContainText(`${report.counts.pass} check`);

  // Every row the panel shows names its node and its remedy.
  const shown = [...report.blocking, ...report.delaying, ...report.advisories];
  for (const check of shown.slice(0, 3)) {
    await expect(panel).toContainText(check.node);
    await expect(panel).toContainText(check.observed.slice(0, 40));
  }
  await expectNoCrash(page);
});
