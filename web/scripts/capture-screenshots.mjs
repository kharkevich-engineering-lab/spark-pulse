/**
 * Capture the documentation screenshots from a running Spark Pulse.
 *
 * The images in `docs/assets/screenshots` are the ones the docs site shows, so
 * they have to be *regenerable* rather than dragged out of somebody's browser
 * once: a page that changes and a screenshot that does not is documentation
 * that lies, and nobody notices until an operator follows it.
 *
 *   npm --prefix web run build                   # the SPA the backend serves
 *   ./scripts/run-backend.sh --port 8123         # simulation, auth forced off
 *   node web/scripts/capture-screenshots.mjs --base http://127.0.0.1:8123
 *
 * `run-backend.sh` sets `SPARK_PULSE_AUTH_ENABLED=false` itself, which is what
 * this needs: with auth on — and a `~/.config/spark-pulse/settings.json` from
 * `run-dev-oidc-full.sh` is enough to turn it on — every page here waits on a
 * selector behind a login it cannot pass, and the capture hangs rather than
 * failing.
 *
 * It lives under `web/` so it resolves Playwright from the frontend's own
 * node_modules — the same one the e2e suite uses, at the same version.
 *
 * Simulation mode is the point: the numbers are a DGX Spark's, no real cluster
 * is touched, and the same images come out on any machine.
 */

import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUT = path.join(ROOT, "docs", "assets", "screenshots");

const args = process.argv.slice(2);
const base = args.includes("--base") ? args[args.indexOf("--base") + 1] : "http://127.0.0.1:8123";
const theme = args.includes("--theme") ? args[args.indexOf("--theme") + 1] : "dark";

/** One shot per page: the route, the file, and what has to be on screen first.
 *
 * `settle` is a selector rather than a timeout because these pages fill in
 * from several requests — a fixed wait either flakes or is always too long. */
const SHOTS = [
  { name: "recipes", path: "/", settle: "text=Recipes and mods." },
  // Expanded, because a collapsed row shows a name and a badge; what the
  // page is *for* — the ranks, the engine's own metrics, the live log — is
  // what opens underneath it. Needs a deployment to exist: POST one to
  // /api/deployments against the simulation backend first.
  { name: "jobs", path: "/jobs", settle: "text=What is serving.", expand: 'button:has-text("Logs")' },
  // The Benchmarks tab of that same page, on its Summary sub-view. Needs
  // `benchmarking_enabled` on — `SPARK_PULSE_BENCHMARKING_ENABLED=true` on the
  // backend — or the tab is not there and this shot fails loudly rather than
  // capturing a page with a tab missing from it.
  {
    name: "benchmarking",
    path: "/benchmarking",
    settle: "text=What is serving.",
    expand: '[role="tab"]:has-text("Summary")',
  },
  { name: "fleet", path: "/cluster", settle: "[data-testid=node-registry]" },
  // Full page: the answer covers every node, and one node in a viewport is
  // the picture this page was rebuilt to stop showing. Monitoring is a tab of
  // Fleet, so the settle is a node's own section rather than the page title,
  // which both tabs share.
  {
    name: "fleet-monitoring",
    path: "/monitoring",
    settle: "text=CPU Memory",
    fullPage: true,
  },
  // Library is one page under four addresses; each tab is its own shot,
  // because a reader looking for "where are my images" is looking for the
  // tab, not for the page that contains it.
  { name: "library", path: "/models", settle: "text=What is on disk." },
  { name: "library-engines", path: "/engines", settle: "text=Engine indexes are configured" },
  { name: "library-registries", path: "/oci", settle: "text=Registries" },
  // There is no MCP shot any more: `/mcp` is a tab of Settings, and a page
  // that is one tab of another page is documented by that page's screenshot.
  { name: "settings", path: "/settings", settle: "text=Settings." },
  // One phone-width shot, because the shell is the change a reader most needs
  // to see at 390: the header collapses to a menu button and nothing overlaps.
  {
    name: "mobile-runs",
    path: "/jobs",
    settle: "text=What is serving.",
    viewport: { width: 390, height: 844 },
  },
];

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
  colorScheme: theme === "light" ? "light" : "dark",
});

// The theme is browser-local, so it is set the way an operator would rather
// than by a query parameter the app does not have.
await context.addInitScript((mode) => {
  window.localStorage.setItem("spark-pulse-theme", mode);
}, theme);

await mkdir(OUT, { recursive: true });

const page = await context.newPage();
const failures = [];

const DESKTOP_VIEWPORT = { width: 1440, height: 900 };

for (const shot of SHOTS) {
  const url = `${base}${shot.path}`;
  try {
    // A shot that asks for its own width gets it; everything else is reset to
    // the desktop one, so the order of this list cannot change an image.
    await page.setViewportSize(shot.viewport ?? DESKTOP_VIEWPORT);
    // Not `networkidle`: Monitoring, Models and Engines hold an SSE stream
    // open for as long as the page is up, so the network is never idle and a
    // wait for it is a wait for the timeout.
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30_000 });
    await page.waitForSelector(shot.settle, { timeout: 15_000 });
    if (shot.expand) {
      await page.click(shot.expand);
      await page.waitForTimeout(1_200);
    }
    // Long enough for the fade-ins every card runs on mount, and for the
    // second render when a page's slower query lands.
    await page.waitForTimeout(1_500);
    const file = path.join(OUT, `${shot.name}.png`);
    await page.screenshot({ path: file, fullPage: Boolean(shot.fullPage) });
    console.log(`captured ${shot.name.padEnd(12)} ${url}`);
  } catch (error) {
    failures.push(`${shot.name}: ${error.message.split("\n")[0]}`);
    console.error(`FAILED   ${shot.name.padEnd(12)} ${url}`);
  }
}

await browser.close();

if (failures.length > 0) {
  console.error(`\n${failures.length} screenshot(s) failed:`);
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}
console.log(`\n${SHOTS.length} screenshots written to docs/assets/screenshots`);
