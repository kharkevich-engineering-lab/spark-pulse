import { defineConfig, devices } from "@playwright/test";

/** End-to-end suite, run against a SIMULATION_MODE=1 backend.
 *
 * `scripts/run-e2e-tests.sh` builds the UI, starts that backend and points
 * this config at it; CI does the same steps by hand. The specs therefore talk
 * to the FastAPI app that serves the built SPA — same origin, no Vite proxy.
 */
export default defineConfig({
  testDir: ".",
  testMatch: "**/*.spec.ts",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  // One worker, everywhere. The backend under test holds its state in one
  // process — a single node registry and a single deployment store — so specs
  // in different files racing each other is not a hypothetical: `nodes.spec`
  // asserting the registry it just read while `multinode.spec` enrols three
  // peers into it failed on roughly every parallel run. `mode: "serial"` only
  // orders a file against itself, and CI already ran with one worker, so this
  // makes a local run mean what a CI run means. It costs nothing: the whole
  // suite takes about the same wall-clock time either way.
  workers: 1,
  reporter: process.env.CI ? "html" : "list",
  timeout: 45_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://127.0.0.1:8100",
    trace: "on-first-retry",
    // The default project asserts on desktop layout: the header nav shows at
    // 900px and up, the menu button below it.
    viewport: { width: 1280, height: 900 },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    // A phone, at the width the audit measured. Chromium at an iPhone 13's
    // viewport rather than `devices["iPhone 13"]`, which is WebKit: CI installs
    // chromium alone, and the shell is a layout question rather than an engine
    // one.
    //
    // Every spec, rather than a list of the ones that survive 390px. That list
    // existed because most pages did not, and it grew a line per PR as each
    // page was reshaped; with the deploy form — the last thing laying out two
    // columns unconditionally — it had nothing left to leave out. Matching
    // everything is also what makes a regression fail here: a page kept off a
    // list is not passing on a phone, it is unmeasured.
    {
      name: "mobile",
      testMatch: "**/*.spec.ts",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 390, height: 844 },
        isMobile: false,
        hasTouch: true,
      },
    },
  ],
});
