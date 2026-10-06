/** Sharing a node: a run that does not fit beside the one already there is
 *  blocked before it starts, and the preview says how much does fit.
 *
 * The simulated control node is a GB10 — about 121 GiB, unified — so a run
 * holding 0.80 of it leaves room for a little under 0.17 once the host's own
 * reserve is set aside. 0.5 is refused; the suggestion the line offers is
 * then the one that goes through.
 */

import { expect, test, type APIRequestContext } from "@playwright/test";
import { escapeRegExp, expectNoCrash, gotoPage, listDeployments, purgeDeployments } from "./helpers";

const RECIPE_ID = "bundled/qwen2.5-0.5b-instruct";
const RECIPE_NAME = "Qwen2.5-0.5B-Instruct";
const HOLDER = "memory-budget-holder";

/** Gone, not merely asked to go: a run still stopping holds its 0.80, and the
 *  next spec to deploy this recipe would be refused beside it. */
async function clear(request: APIRequestContext) {
  await purgeDeployments(request, RECIPE_ID);
  await expect
    .poll(
      async () =>
        (await listDeployments(request)).filter(
          (d) => d.recipe_id === RECIPE_ID && !["stopped", "error"].includes(d.status),
        ).length,
      { message: "the holder should be gone before the next spec" },
    )
    .toBe(0);
}

test.beforeEach(async ({ request }) => clear(request));
test.afterEach(async ({ request }) => clear(request));

test("a run that does not fit beside another is blocked, and the fit is offered", async ({
  page,
  request,
}) => {
  const created = await request.post("/api/deployments", {
    data: { recipe_id: RECIPE_ID, name: HOLDER, params: { gpu_memory_utilization: 0.8 } },
  });
  expect(created.ok(), "the first run fits on an empty node").toBeTruthy();

  await gotoPage(page, "/");
  await page.getByRole("button", { name: new RegExp(escapeRegExp(RECIPE_NAME)) }).click();
  await expect(page.getByRole("heading", { name: RECIPE_NAME, exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Deploy options" }).click();

  const field = page.getByLabel("GPU memory");
  await field.fill("0.5");
  await page.getByRole("button", { name: "Preview" }).click();

  const line = page.getByTestId("deploy-memory-budget");
  await expect(line).toContainText(`${HOLDER} (0.80)`);
  await expect(line).toContainText(/Up to 0\.\d\d fits\./);
  await expect(page.getByTestId("preflight-verdict")).toHaveText("Blocked");
  await expect(page.getByTestId("preflight-check-fail")).toContainText("Shared memory");
  await expect(page.getByTestId("preflight-check-fail")).toContainText(
    "Lower gpu_memory_utilization",
  );

  await page.getByTestId("deploy-memory-apply").click();
  await expect(field).toHaveValue(/^0\.\d\d$/);
  await page.getByRole("button", { name: "Preview" }).click();

  await expect(page.getByTestId("preflight-verdict")).not.toHaveText("Blocked");
  await expect(page.getByTestId("deploy-memory-apply")).toHaveCount(0);
  await expectNoCrash(page);
});
