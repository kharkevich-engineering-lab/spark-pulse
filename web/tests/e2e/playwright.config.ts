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
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? "html" : "list",
  timeout: 45_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://127.0.0.1:8100",
    trace: "on-first-retry",
    // The suite asserts on desktop layout: the sidebar nav is `lg:` and up.
    viewport: { width: 1280, height: 900 },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
