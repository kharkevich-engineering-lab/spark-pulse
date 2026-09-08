/**
 * Capture the documentation screenshots from a running Spark Pulse.
 *
 * The images in `docs/assets/screenshots` are the ones the docs site shows, so
 * they have to be *regenerable* rather than dragged out of somebody's browser
 * once: a page that changes and a screenshot that does not is documentation
 * that lies, and nobody notices until an operator follows it.
 *
 *   ./scripts/run-backend.sh --port 8123        # simulation, serves the built UI
 *   node web/scripts/capture-screenshots.mjs --base http://127.0.0.1:8123
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
  { name: "recipes", path: "/", settle: "text=Recipes" },
  // Expanded, because a collapsed row shows a name and a badge; what the
  // page is *for* — the ranks, the engine's own metrics, the live log — is
  // what opens underneath it.
  { name: "jobs", path: "/jobs", settle: "text=Inference", expand: "text=qwen3.8-27b" },
  { name: "cluster", path: "/cluster", settle: "text=Cluster" },
  // Full page: the answer covers every node, and one node in a viewport is
  // the picture this page was rebuilt to stop showing.
  { name: "monitoring", path: "/monitoring", settle: "text=Monitoring", fullPage: true },
  { name: "models", path: "/models", settle: "text=Models" },
  { name: "engines", path: "/engines", settle: "text=Engines" },
  { name: "oci", path: "/oci", settle: "text=Collections" },
  { name: "cache", path: "/cache", settle: "text=Cache" },
  { name: "mcp", path: "/mcp", settle: "text=MCP" },
  { name: "settings", path: "/settings", settle: "text=Settings" },
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

for (const shot of SHOTS) {
  const url = `${base}${shot.path}`;
  try {
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
