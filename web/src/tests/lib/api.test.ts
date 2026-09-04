/** The whole `lib/api.ts` surface, and the parts of it that are not wrappers.
 *
 * Nearly every export is one line: `json<T>(path, init)`. A one-line wrapper
 * has exactly one way to be wrong — the wrong path, the wrong verb, a body
 * the backend does not recognise, or an id interpolated raw into a URL it
 * has to be escaped for — and none of those fail loudly in the browser. They
 * come back as a 404 or a 422 that the page renders as "something went
 * wrong". So the table below calls every request function and pins the
 * request it makes; it is a spelling test, and spelling is the entire
 * failure mode.
 *
 * The rest of the file covers what `json()` itself does: the CSRF header, the
 * 401 redirect, the shapes `ApiError` has to survive, the FormData upload
 * that goes around `json()` entirely, and the two EventSource streams.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import * as api from "@/lib/api";
import { ApiError } from "@/lib/api";

/** A resolved response with a JSON body, which is what `json()` expects. */
const ok = (body: unknown) =>
  Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);

const fetchMock = () => globalThis.fetch as unknown as ReturnType<typeof vi.fn>;

/** One request function, and the request it is supposed to make. */
interface Case {
  /** How the call reads at a call site. */
  name: string;
  call: () => Promise<unknown>;
  path: string;
  method: string;
  /** The parsed request body, for the calls that send one. */
  body?: unknown;
  /** What the server answers — only when the wrapper unwraps an envelope. */
  response?: unknown;
}

const CASES: Case[] = [
  // ── Recipes ───────────────────────────────────────────────────────────────
  { name: "fetchRecipes", call: () => api.fetchRecipes(), path: "/api/recipes", method: "GET" },
  {
    name: "fetchRecipe",
    call: () => api.fetchRecipe("bundled/qwen3-8b"),
    path: "/api/recipes/bundled/qwen3-8b",
    method: "GET",
  },
  {
    name: "importRecipes",
    call: () => api.importRecipes({ url: "https://example.invalid/repo.git", ref: "main" }),
    path: "/api/recipes/import",
    method: "POST",
    body: { url: "https://example.invalid/repo.git", ref: "main" },
  },
  {
    name: "fetchRecipeImportStatus",
    call: () => api.fetchRecipeImportStatus(),
    path: "/api/recipes/import/status",
    method: "GET",
  },

  // ── Deployments ───────────────────────────────────────────────────────────
  { name: "fetchDeployments", call: () => api.fetchDeployments(), path: "/api/deployments", method: "GET" },
  {
    name: "createDeployment",
    call: () => api.createDeployment({ recipe_id: "r", name: "n", params: {} }),
    path: "/api/deployments",
    method: "POST",
    body: { recipe_id: "r", name: "n", params: {} },
  },
  {
    name: "planDeployment",
    call: () => api.planDeployment({ recipe_id: "r" }),
    path: "/api/deployments/plan",
    method: "POST",
    body: { recipe_id: "r" },
  },
  {
    name: "fetchDeployment",
    call: () => api.fetchDeployment("abc123"),
    path: "/api/deployments/abc123",
    method: "GET",
  },
  {
    name: "runPreflight",
    call: () => api.runPreflight({ recipe_id: "r", nodes: ["10.0.0.11"] }),
    path: "/api/preflight/run",
    method: "POST",
    body: { recipe_id: "r", nodes: ["10.0.0.11"] },
  },
  {
    name: "stopDeployment",
    call: () => api.stopDeployment("abc123"),
    path: "/api/deployments/abc123",
    method: "DELETE",
  },
  {
    name: "fetchLogs defaults to the last 200 lines",
    call: () => api.fetchLogs("abc123"),
    path: "/api/deployments/abc123/logs?lines=200",
    method: "GET",
  },
  {
    name: "fetchLogs with an explicit line count",
    call: () => api.fetchLogs("abc123", 20),
    path: "/api/deployments/abc123/logs?lines=20",
    method: "GET",
  },

  // ── Memory and cache ──────────────────────────────────────────────────────
  { name: "fetchMemory", call: () => api.fetchMemory(), path: "/api/memory", method: "GET" },
  {
    name: "killGpuProcess",
    call: () => api.killGpuProcess(98251),
    path: "/api/memory/processes/98251",
    method: "DELETE",
  },
  { name: "fetchCache", call: () => api.fetchCache(), path: "/api/cache", method: "GET" },
  {
    name: "cleanCache",
    call: () => api.cleanCache(["hf", "torch"]),
    path: "/api/cache/clean",
    method: "POST",
    body: { targets: ["hf", "torch"] },
  },

  // ── Settings and secrets ──────────────────────────────────────────────────
  { name: "fetchSettings", call: () => api.fetchSettings(), path: "/api/settings", method: "GET" },
  {
    name: "updateSettings",
    call: () => api.updateSettings({ webui_port: 8100 }),
    path: "/api/settings",
    method: "PUT",
    body: { webui_port: 8100 },
  },
  { name: "fetchSecrets", call: () => api.fetchSecrets(), path: "/api/settings/secrets", method: "GET" },
  {
    name: "saveSecrets",
    call: () => api.saveSecrets({ hf_token: "hf_xxx" }),
    path: "/api/settings/secrets",
    method: "PUT",
    body: { hf_token: "hf_xxx" },
  },
  {
    name: "deleteSecret",
    call: () => api.deleteSecret("hf_token"),
    path: "/api/settings/secrets/hf_token",
    method: "DELETE",
  },

  // ── Mods ──────────────────────────────────────────────────────────────────
  { name: "fetchMods", call: () => api.fetchMods(), path: "/api/mods", method: "GET" },
  {
    name: "fetchMod escapes the mod id",
    call: () => api.fetchMod("bundled/flash attn"),
    path: "/api/mods/bundled%2Fflash%20attn",
    method: "GET",
  },
  {
    name: "fetchRecipeCustomization escapes the recipe id",
    call: () => api.fetchRecipeCustomization("bundled/qwen3-8b"),
    path: "/api/recipes/customize/bundled%2Fqwen3-8b",
    method: "GET",
  },
  {
    name: "saveRecipeCustomization",
    call: () => api.saveRecipeCustomization("bundled/qwen3-8b", { model: "Qwen/Qwen3-8B" }),
    path: "/api/recipes/customize/bundled%2Fqwen3-8b",
    method: "PUT",
    body: { model: "Qwen/Qwen3-8B" },
  },
  {
    name: "deleteRecipeCustomization",
    call: () => api.deleteRecipeCustomization("bundled/qwen3-8b"),
    path: "/api/recipes/customize/bundled%2Fqwen3-8b",
    method: "DELETE",
  },
  {
    name: "validateMod",
    call: () => api.validateMod({ path: "/mods/flash" }),
    path: "/api/mods/validate",
    method: "POST",
    body: { path: "/mods/flash" },
  },
  {
    name: "applyMod",
    call: () => api.applyMod({ mod_name: "flash", mod_path: "/mods/flash", target: "all" }),
    path: "/api/mods/apply",
    method: "POST",
    body: { mod_name: "flash", mod_path: "/mods/flash", target: "all" },
  },
  {
    name: "rollbackMod",
    call: () =>
      api.rollbackMod({
        mod_name: "flash",
        mod_path: "/mods/flash",
        target: "all",
        completed_nodes: ["10.0.0.11"],
      }),
    path: "/api/mods/rollback",
    method: "POST",
    body: {
      mod_name: "flash",
      mod_path: "/mods/flash",
      target: "all",
      completed_nodes: ["10.0.0.11"],
    },
  },

  // ── Custom files ──────────────────────────────────────────────────────────
  {
    name: "listCustomRecipes",
    call: () => api.listCustomRecipes(),
    path: "/api/custom-files/recipes/list",
    method: "GET",
  },
  {
    name: "getCustomRecipeContent",
    call: () => api.getCustomRecipeContent("my-recipe"),
    path: "/api/custom-files/recipes/my-recipe",
    method: "GET",
  },
  {
    name: "saveCustomRecipe",
    call: () => api.saveCustomRecipe("my-recipe", "name: mine\n"),
    path: "/api/custom-files/recipes/my-recipe",
    method: "PUT",
    body: { content: "name: mine\n" },
  },
  {
    name: "deleteCustomRecipe",
    call: () => api.deleteCustomRecipe("my-recipe"),
    path: "/api/custom-files/recipes/my-recipe",
    method: "DELETE",
  },
  {
    name: "listCustomMods",
    call: () => api.listCustomMods(),
    path: "/api/custom-files/mods/list",
    method: "GET",
  },
  {
    name: "getCustomModFiles",
    call: () => api.getCustomModFiles("my-mod"),
    path: "/api/custom-files/mods/my-mod",
    method: "GET",
  },
  {
    name: "saveCustomModFiles",
    call: () => api.saveCustomModFiles("my-mod", { "Dockerfile": "FROM scratch" }),
    path: "/api/custom-files/mods/my-mod",
    method: "PUT",
    body: { Dockerfile: "FROM scratch" },
  },
  {
    name: "deleteCustomMod",
    call: () => api.deleteCustomMod("my-mod"),
    path: "/api/custom-files/mods/my-mod",
    method: "DELETE",
  },

  // ── Benchmarks ────────────────────────────────────────────────────────────
  { name: "fetchBenchmarks", call: () => api.fetchBenchmarks(), path: "/api/benchmarks", method: "GET" },
  {
    name: "fetchBenchmark",
    call: () => api.fetchBenchmark("run-1"),
    path: "/api/benchmarks/run-1",
    method: "GET",
  },
  {
    name: "fetchLatestByRecipe",
    call: () => api.fetchLatestByRecipe(),
    path: "/api/benchmarks/latest-by-recipe",
    method: "GET",
  },
  {
    name: "compareRuns",
    call: () => api.compareRuns(["run-1", "run-2"]),
    path: "/api/benchmarks/compare",
    method: "POST",
    body: { run_ids: ["run-1", "run-2"] },
  },
  {
    name: "runBenchmark",
    call: () => api.runBenchmark({ deployment_id: "abc123", recipe_id: "r" }),
    path: "/api/benchmarks",
    method: "POST",
    body: { deployment_id: "abc123", recipe_id: "r" },
  },

  // ── OCI registry ──────────────────────────────────────────────────────────
  {
    name: "fetchOciRegistries",
    call: () => api.fetchOciRegistries(),
    path: "/api/oci/registries",
    method: "GET",
  },
  {
    name: "addOciRegistry",
    call: () => api.addOciRegistry({ name: "ghcr" }),
    path: "/api/oci/registries",
    method: "POST",
    body: { name: "ghcr" },
  },
  {
    name: "updateOciRegistry escapes the registry name",
    call: () => api.updateOciRegistry("my registry", { name: "my registry" }),
    path: "/api/oci/registries/my%20registry",
    method: "PUT",
    body: { name: "my registry" },
  },
  {
    name: "removeOciRegistry",
    call: () => api.removeOciRegistry("ghcr"),
    path: "/api/oci/registries/ghcr",
    method: "DELETE",
  },
  {
    name: "testOciRegistry",
    call: () => api.testOciRegistry("ghcr"),
    path: "/api/oci/registries/ghcr/test-connection",
    method: "GET",
  },
  {
    name: "fetchOciRegistryVersions",
    call: () => api.fetchOciRegistryVersions("ghcr"),
    path: "/api/oci/registries/ghcr/versions",
    method: "GET",
  },
  {
    name: "fetchOciCollections without filters asks for no query string",
    call: () => api.fetchOciCollections(),
    path: "/api/oci/collections",
    method: "GET",
  },
  {
    name: "fetchOciCollections with a registry and a version",
    call: () => api.fetchOciCollections("ghcr", "1.2.0"),
    path: "/api/oci/collections?registry=ghcr&version=1.2.0",
    method: "GET",
  },
  {
    name: "installOciCollection",
    call: () => api.installOciCollection("bundled", "1.2.0", "ghcr"),
    path: "/api/oci/install",
    method: "POST",
    body: { name: "bundled", version: "1.2.0", registry: "ghcr" },
  },
  {
    name: "checkOciUpdates without filters",
    call: () => api.checkOciUpdates(),
    path: "/api/oci/check",
    method: "GET",
  },
  {
    name: "checkOciUpdates for one collection",
    call: () => api.checkOciUpdates("bundled", "ghcr"),
    path: "/api/oci/check?collection=bundled&registry=ghcr",
    method: "GET",
  },
  {
    name: "applyOciUpdates",
    call: () =>
      api.applyOciUpdates([{ collection: "bundled", target_version: "1.3.0", registry: "ghcr" }]),
    path: "/api/oci/update",
    method: "POST",
    body: { updates: [{ collection: "bundled", target_version: "1.3.0", registry: "ghcr" }] },
  },
  { name: "fetchOciMeta", call: () => api.fetchOciMeta(), path: "/api/oci/recipes/meta", method: "GET" },
  {
    name: "fetchOciMetaByName escapes the recipe name",
    call: () => api.fetchOciMetaByName("bundled/qwen3-8b"),
    path: "/api/oci/recipes/meta/bundled%2Fqwen3-8b",
    method: "GET",
  },
  {
    name: "fetchOciCollectionRecipes without filters",
    call: () => api.fetchOciCollectionRecipes("bundled"),
    path: "/api/oci/collections/bundled/recipes",
    method: "GET",
  },
  {
    name: "fetchOciCollectionRecipes with a version and a registry",
    call: () => api.fetchOciCollectionRecipes("bundled", "1.2.0", "ghcr"),
    path: "/api/oci/collections/bundled/recipes?version=1.2.0&registry=ghcr",
    method: "GET",
  },
  {
    name: "fetchOciAutoUpdateSettings",
    call: () => api.fetchOciAutoUpdateSettings(),
    path: "/api/oci/auto-update/settings",
    method: "GET",
  },
  {
    name: "updateOciAutoUpdateSettings",
    call: () => api.updateOciAutoUpdateSettings({ enabled: true }),
    path: "/api/oci/auto-update/settings",
    method: "PUT",
    body: { enabled: true },
  },
  {
    name: "runOciAutoUpdate",
    call: () => api.runOciAutoUpdate(),
    path: "/api/oci/auto-update/run",
    method: "POST",
  },
  {
    name: "installOciRecipe",
    call: () => api.installOciRecipe({ collection: "bundled", recipe: "qwen3-8b" }),
    path: "/api/oci/recipes/install",
    method: "POST",
    body: { collection: "bundled", recipe: "qwen3-8b" },
  },
  {
    name: "updateOciRecipe escapes the recipe name",
    call: () => api.updateOciRecipe("bundled/qwen3-8b", { collection: "bundled" }),
    path: "/api/oci/recipes/update/bundled%2Fqwen3-8b",
    method: "POST",
    body: { collection: "bundled" },
  },
  {
    name: "uninstallOciRecipe escapes the recipe name",
    call: () => api.uninstallOciRecipe("bundled/qwen3-8b"),
    path: "/api/oci/recipes/bundled%2Fqwen3-8b",
    method: "DELETE",
  },
  {
    name: "clearOciCache without a key clears everything",
    call: () => api.clearOciCache(),
    path: "/api/oci/cache/clear",
    method: "POST",
    body: {},
  },
  {
    name: "clearOciCache with a key clears one entry",
    call: () => api.clearOciCache("ghcr:bundled"),
    path: "/api/oci/cache/clear",
    method: "POST",
    body: { key: "ghcr:bundled" },
  },
  {
    name: "startOciBackgroundUpdater",
    call: () => api.startOciBackgroundUpdater(),
    path: "/api/oci/background/start",
    method: "POST",
  },
  {
    name: "stopOciBackgroundUpdater",
    call: () => api.stopOciBackgroundUpdater(),
    path: "/api/oci/background/stop",
    method: "POST",
  },

  // ── Discovery and launch scripts ──────────────────────────────────────────
  { name: "runDiscovery", call: () => api.runDiscovery(), path: "/api/discovery", method: "POST" },
  {
    name: "getValidation",
    call: () => api.getValidation(),
    path: "/api/discovery/validation",
    method: "GET",
  },
  {
    name: "resolveLaunchScript",
    call: () => api.resolveLaunchScript({ path: "/opt/run.sh" }),
    path: "/api/launch-script/resolve",
    method: "POST",
    body: { path: "/opt/run.sh" },
  },
  {
    name: "analyzeLaunchScript",
    call: () => api.analyzeLaunchScript({ path: "/opt/run.sh" }),
    path: "/api/launch-script/analyze",
    method: "POST",
    body: { path: "/opt/run.sh" },
  },
  {
    name: "validateLaunchScript",
    call: () => api.validateLaunchScript({ path: "/opt/run.sh" }),
    path: "/api/launch-script/validate",
    method: "POST",
    body: { path: "/opt/run.sh" },
  },
  {
    name: "patchLaunchScript",
    call: () => api.patchLaunchScript({ path: "/opt/run.sh", total_nodes: 2 }),
    path: "/api/launch-script/patch",
    method: "POST",
    body: { path: "/opt/run.sh", total_nodes: 2 },
  },

  // ── Engines ───────────────────────────────────────────────────────────────
  { name: "fetchEngines", call: () => api.fetchEngines(), path: "/api/engines", method: "GET" },
  {
    name: "fetchEngine defaults the variant",
    call: () => api.fetchEngine("vllm"),
    path: "/api/engines/vllm/default",
    method: "GET",
  },
  {
    name: "refreshEngines",
    call: () => api.refreshEngines(),
    path: "/api/engines/refresh",
    method: "POST",
  },
  {
    name: "renderLaunch",
    call: () => api.renderLaunch({ recipe_id: "r" }),
    path: "/api/engines/render",
    method: "POST",
    body: { recipe_id: "r" },
  },

  // ── Models ────────────────────────────────────────────────────────────────
  {
    name: "fetchModels unwraps the models envelope",
    call: () => api.fetchModels(),
    path: "/api/models",
    method: "GET",
    response: { models: [] },
  },
  {
    name: "fetchModel",
    call: () => api.fetchModel("Qwen--Qwen3-8B"),
    path: "/api/models/Qwen--Qwen3-8B",
    method: "GET",
  },
  {
    name: "fetchModelSources unwraps the sources envelope",
    call: () => api.fetchModelSources(),
    path: "/api/models/sources",
    method: "GET",
    response: { sources: [] },
  },
  {
    name: "saveModelSources",
    call: () => api.saveModelSources([{ name: "hf", type: "hf_hub" }]),
    path: "/api/models/sources",
    method: "PUT",
    body: { sources: [{ name: "hf", type: "hf_hub" }] },
    response: { sources: [] },
  },
  {
    name: "startModelDownload",
    call: () => api.startModelDownload({ model: "Qwen/Qwen3-8B", source: "hf" }),
    path: "/api/models/download",
    method: "POST",
    body: { model: "Qwen/Qwen3-8B", source: "hf" },
  },
  {
    name: "fetchModelDownloads unwraps the jobs envelope",
    call: () => api.fetchModelDownloads(),
    path: "/api/models/downloads",
    method: "GET",
    response: { jobs: [] },
  },
  {
    name: "fetchModelDownload",
    call: () => api.fetchModelDownload("job-1"),
    path: "/api/models/downloads/job-1",
    method: "GET",
  },
  {
    name: "cancelModelDownload",
    call: () => api.cancelModelDownload("job-1"),
    path: "/api/models/downloads/job-1/cancel",
    method: "POST",
  },
  {
    name: "syncModelToNodes",
    call: () => api.syncModelToNodes("Qwen--Qwen3-8B", ["10.0.0.11"], "spark"),
    path: "/api/models/Qwen--Qwen3-8B/sync",
    method: "POST",
    body: { nodes: ["10.0.0.11"], ssh_user: "spark" },
  },
  {
    name: "fetchModelPresence escapes the node list",
    call: () => api.fetchModelPresence("Qwen--Qwen3-8B", ["10.0.0.11", "10.0.0.12"]),
    path: "/api/models/Qwen--Qwen3-8B/presence?nodes=10.0.0.11%2C10.0.0.12",
    method: "GET",
  },
  {
    name: "deleteModel",
    call: () => api.deleteModel("Qwen--Qwen3-8B"),
    path: "/api/models/Qwen--Qwen3-8B",
    method: "DELETE",
  },

  // ── Engine images ─────────────────────────────────────────────────────────
  {
    name: "fetchImages unwraps the images envelope",
    call: () => api.fetchImages(),
    path: "/api/images",
    method: "GET",
    response: { images: [] },
  },
  {
    name: "startImagePull",
    call: () => api.startImagePull("ghcr.io/example/vllm:0.1.0"),
    path: "/api/images/pull",
    method: "POST",
    body: { ref: "ghcr.io/example/vllm:0.1.0" },
  },
  {
    name: "fetchImagePulls unwraps the jobs envelope",
    call: () => api.fetchImagePulls(),
    path: "/api/images/pulls",
    method: "GET",
    response: { jobs: [] },
  },
  {
    name: "fetchImagePull",
    call: () => api.fetchImagePull("job-1"),
    path: "/api/images/pulls/job-1",
    method: "GET",
  },
  {
    name: "cancelImagePull",
    call: () => api.cancelImagePull("job-1"),
    path: "/api/images/pulls/job-1/cancel",
    method: "POST",
  },
  // An image ref carries slashes and a colon, so it is escaped into the query
  // rather than the path — the comment in api.ts says so, and this is the
  // assertion that keeps it true.
  {
    name: "deleteImage escapes the ref into the query",
    call: () => api.deleteImage("ghcr.io/example/vllm:0.1.0"),
    path: "/api/images?ref=ghcr.io%2Fexample%2Fvllm%3A0.1.0",
    method: "DELETE",
  },
  {
    name: "syncImageToNodes",
    call: () => api.syncImageToNodes("ghcr.io/example/vllm:0.1.0", ["10.0.0.11"], "spark"),
    path: "/api/images/sync",
    method: "POST",
    body: { ref: "ghcr.io/example/vllm:0.1.0", nodes: ["10.0.0.11"], ssh_user: "spark" },
  },
  {
    name: "fetchImagePresence escapes both the ref and the node list",
    call: () => api.fetchImagePresence("ghcr.io/example/vllm:0.1.0", ["10.0.0.11"]),
    path: "/api/images/presence?ref=ghcr.io%2Fexample%2Fvllm%3A0.1.0&nodes=10.0.0.11",
    method: "GET",
  },

  // ── Node registry ─────────────────────────────────────────────────────────
  { name: "fetchNodes", call: () => api.fetchNodes(), path: "/api/nodes", method: "GET" },
  // The id is minted server-side; a client that sends one is refused, so the
  // body this posts must carry only the fields an operator supplied.
  {
    name: "addNode",
    call: () => api.addNode({ address: "10.0.0.11", ssh_user: "spark" }),
    path: "/api/nodes",
    method: "POST",
    body: { address: "10.0.0.11", ssh_user: "spark" },
  },
  {
    name: "updateNode PATCHes only the changed fields",
    call: () => api.updateNode("node-1", { state: "healthy" }),
    path: "/api/nodes/node-1",
    method: "PATCH",
    body: { state: "healthy" },
  },
  {
    name: "removeNode",
    call: () => api.removeNode("node-1"),
    path: "/api/nodes/node-1",
    method: "DELETE",
  },
  {
    name: "discoverNodes defaults to a three-second browse",
    call: () => api.discoverNodes(),
    path: "/api/nodes/discover?timeout=3",
    method: "GET",
  },
  {
    name: "discoverNodes with an explicit timeout",
    call: () => api.discoverNodes(1),
    path: "/api/nodes/discover?timeout=1",
    method: "GET",
  },
  {
    name: "fetchNodeDiagnostics",
    call: () => api.fetchNodeDiagnostics(),
    path: "/api/nodes/diagnostics",
    method: "GET",
  },
];

describe("api request surface", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    // The module keeps one CSRF token for the process; clear it so the header
    // assertions below are about the call, not about test ordering.
    document.head.innerHTML = "";
    api.initCsrfToken();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  for (const testCase of CASES) {
    it(`${testCase.name} → ${testCase.method} ${testCase.path}`, async () => {
      fetchMock().mockReturnValue(ok(testCase.response ?? {}));

      await testCase.call();

      expect(fetchMock()).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock().mock.calls[0] as [string, RequestInit | undefined];
      expect(url).toBe(testCase.path);
      expect((init?.method ?? "GET").toUpperCase()).toBe(testCase.method);
      if (testCase.body === undefined) {
        expect(init?.body).toBeUndefined();
      } else {
        expect(JSON.parse(init?.body as string)).toEqual(testCase.body);
      }
      // Cookie auth: every call has to carry the session cookie or it is a 401.
      expect(init?.credentials).toBe("include");
    });
  }

  it("covers every request function api.ts exports", () => {
    // A new endpoint added without a case here is a wrapper nothing checks
    // the spelling of, which is the one way these one-liners break.
    const exported = Object.entries(api)
      .filter(([name, value]) => typeof value === "function" && !EXEMPT.has(name))
      .map(([name]) => name);
    const tested = new Set(CASES.map((c) => c.name.split(" ")[0]));
    expect([...exported].filter((name) => !tested.has(name))).toEqual([]);
  });
});

/** Exports that are not `json()` wrappers, and are tested on their own below. */
const EXEMPT = new Set([
  "ApiError",
  "initCsrfToken",
  "connectLogStream",
  "connectMetricsStream",
  "uploadCustomRecipe",
]);

describe("json(): the things every call inherits", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    document.head.innerHTML = "";
    api.initCsrfToken();
  });
  afterEach(() => {
    document.head.innerHTML = "";
    api.initCsrfToken();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("reads the CSRF token out of the meta tag the server rendered", async () => {
    document.head.innerHTML = '<meta name="csrf-token" content="tok-123">';
    api.initCsrfToken();

    fetchMock().mockReturnValue(ok({}));
    await api.updateSettings({ webui_port: 8100 });

    const [, init] = fetchMock().mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok-123");
  });

  // A GET carries no token because it changes nothing; sending one anyway
  // would be the kind of detail that quietly starts being *required*.
  it("sends the CSRF token on writes and not on reads", async () => {
    document.head.innerHTML = '<meta name="csrf-token" content="tok-123">';
    api.initCsrfToken();
    fetchMock().mockReturnValue(ok({}));

    await api.fetchSettings();
    await api.updateSettings({ webui_port: 1 });
    await api.createDeployment({ recipe_id: "r", name: "n", params: {} });
    await api.updateNode("n1", { state: "healthy" });
    await api.stopDeployment("abc");

    const header = (index: number) => {
      const [, init] = fetchMock().mock.calls[index] as [string, RequestInit];
      return (init.headers as Record<string, string>)["X-CSRF-Token"];
    };
    expect(header(0)).toBeUndefined(); // GET
    expect(header(1)).toBe("tok-123"); // PUT
    expect(header(2)).toBe("tok-123"); // POST
    expect(header(3)).toBe("tok-123"); // PATCH
    expect(header(4)).toBe("tok-123"); // DELETE
  });

  it("omits the header entirely when the page rendered no token", async () => {
    fetchMock().mockReturnValue(ok({}));
    await api.updateSettings({ webui_port: 8100 });

    const [, init] = fetchMock().mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBeUndefined();
  });

  it("lets a caller add its own headers without losing the content type", async () => {
    fetchMock().mockReturnValue(ok({}));
    await api.saveCustomRecipe("mine", "name: mine");

    const [, init] = fetchMock().mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  // A cookie session that has expired looks like every other 401. The page
  // cannot recover from it, so the wrapper sends the operator to the login
  // page rather than rendering an error nobody can act on.
  it("sends the browser to /login on a 401 and still rejects", async () => {
    const original = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: { href: "" } as unknown as Location,
    });
    try {
      fetchMock().mockReturnValue(Promise.resolve({ ok: false, status: 401 } as Response));
      await expect(api.fetchRecipes()).rejects.toThrow("Unauthorized");
      expect(window.location.href).toBe("/login");
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        writable: true,
        value: original,
      });
    }
  });
});

describe("ApiError", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    document.head.innerHTML = "";
    api.initCsrfToken();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const failing = (status: number, text: string) =>
    Promise.resolve({ ok: false, status, text: () => Promise.resolve(text) } as Response);

  /** Catch and return, so a test can assert on the error rather than a throw. */
  async function caught(run: () => Promise<unknown>): Promise<ApiError> {
    try {
      await run();
    } catch (e) {
      return e as ApiError;
    }
    throw new Error("the call was expected to reject");
  }

  it("carries the status and stays an Error, so old call sites keep working", async () => {
    fetchMock().mockReturnValue(failing(404, JSON.stringify({ detail: "No such recipe" })));
    const error = await caught(() => api.fetchRecipe("nope"));

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toBeInstanceOf(Error);
    expect(error.name).toBe("ApiError");
    expect(error.status).toBe(404);
  });

  it("uses FastAPI's string detail as the message", async () => {
    fetchMock().mockReturnValue(
      failing(400, JSON.stringify({ detail: "10.99.99.99 is not in the node registry" })),
    );
    const error = await caught(() => api.addNode({ address: "10.99.99.99" }));
    expect(error.message).toBe("API 400: 10.99.99.99 is not in the node registry");
  });

  // The pre-flight gate answers with a structured detail; the message is one
  // field of it and the report is another, and both have to survive.
  it("uses detail.message when the detail is structured, and keeps the payload", async () => {
    const payload = { detail: { message: "no docker on spark-02", preflight: { verdict: "blocked" } } };
    fetchMock().mockReturnValue(failing(409, JSON.stringify(payload)));

    const error = await caught(() => api.createDeployment({ recipe_id: "r", name: "n", params: {} }));
    expect(error.message).toBe("API 409: no docker on spark-02");
    expect(error.payload).toEqual(payload);
  });

  it("falls back to the raw body when the response is not JSON at all", async () => {
    fetchMock().mockReturnValue(failing(502, "<html>Bad Gateway</html>"));
    const error = await caught(() => api.fetchRecipes());
    expect(error.message).toBe("API 502: <html>Bad Gateway</html>");
    expect(error.payload).toBe("<html>Bad Gateway</html>");
  });

  it("falls back to the raw body when a JSON body has no detail to read", async () => {
    fetchMock().mockReturnValue(failing(500, JSON.stringify({ oops: true })));
    const error = await caught(() => api.fetchRecipes());
    expect(error.message).toBe('API 500: {"oops":true}');
    expect(error.payload).toEqual({ oops: true });
  });

  it("falls back to the raw body when detail is neither a string nor a message object", async () => {
    fetchMock().mockReturnValue(failing(422, JSON.stringify({ detail: [{ loc: ["body"] }] })));
    const error = await caught(() => api.fetchRecipes());
    expect(error.message).toContain("API 422:");
    expect(error.message).toContain("loc");
  });
});

/** The one upload that does not go through `json()`, and so repeats its rules. */
describe("uploadCustomRecipe", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    document.head.innerHTML = "";
    api.initCsrfToken();
  });
  afterEach(() => {
    document.head.innerHTML = "";
    api.initCsrfToken();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const file = () => new File(["name: mine\n"], "mine.yaml", { type: "text/yaml" });

  // The browser has to set the multipart boundary itself, so this request must
  // NOT carry the JSON content type every other call sends.
  it("posts multipart form data and lets the browser set the content type", async () => {
    fetchMock().mockReturnValue(ok({ id: "mine", name: "mine" }));

    const result = await api.uploadCustomRecipe(file());
    expect(result.id).toBe("mine");

    const [url, init] = fetchMock().mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/custom-files/recipes/upload");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get("file")).toBeInstanceOf(File);
    expect((init.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
  });

  it("still carries the CSRF token", async () => {
    document.head.innerHTML = '<meta name="csrf-token" content="tok-123">';
    api.initCsrfToken();
    fetchMock().mockReturnValue(ok({ id: "mine", name: "mine" }));

    await api.uploadCustomRecipe(file());
    const [, init] = fetchMock().mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok-123");
  });

  it("sends the browser to /login on a 401, like every other call", async () => {
    const original = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: { href: "" } as unknown as Location,
    });
    try {
      fetchMock().mockReturnValue(Promise.resolve({ ok: false, status: 401 } as Response));
      await expect(api.uploadCustomRecipe(file())).rejects.toThrow("Unauthorized");
      expect(window.location.href).toBe("/login");
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        writable: true,
        value: original,
      });
    }
  });

  it("reports the server's refusal rather than a bare failure", async () => {
    fetchMock().mockReturnValue(
      Promise.resolve({
        ok: false,
        status: 400,
        text: () => Promise.resolve("not a recipe"),
      } as Response),
    );
    await expect(api.uploadCustomRecipe(file())).rejects.toThrow("API 400: not a recipe");
  });
});

/** The SSE mock setupTests installs, reachable as the constructed instance. */
interface FakeEventSource {
  url: string;
  readyState: number;
  _listeners: Record<string, Array<(event: MessageEvent) => void>>;
}

/** Deliver one server-sent event to whatever the stream subscribed. */
function emit(source: FakeEventSource, type: string, data: string): void {
  for (const listener of source._listeners[type] ?? []) {
    listener({ data } as MessageEvent);
  }
}

describe("SSE streams", () => {
  let created: FakeEventSource[];
  let RealEventSource: typeof EventSource;

  beforeEach(() => {
    created = [];
    RealEventSource = globalThis.EventSource;
    globalThis.EventSource = class extends RealEventSource {
      constructor(url: string) {
        super(url);
        created.push(this as unknown as FakeEventSource);
      }
    } as unknown as typeof EventSource;
  });
  afterEach(() => {
    globalThis.EventSource = RealEventSource;
  });

  it("subscribes a deployment's log stream to logs, status and errors", () => {
    const seen: Array<[string, unknown]> = [];
    const close = api.connectLogStream("abc123", (event, data) => seen.push([event, data]));

    const source = created[0];
    expect(source.url).toBe("/sse/logs/abc123");
    expect(Object.keys(source._listeners).sort()).toEqual(["error", "log", "status"]);

    emit(source, "log", JSON.stringify({ text: "starting engine" }));
    emit(source, "status", JSON.stringify({ status: "running" }));
    emit(source, "error", JSON.stringify({ message: "container exited" }));

    expect(seen).toEqual([
      ["log", { text: "starting engine" }],
      ["status", { status: "running" }],
      ["error", { message: "container exited" }],
    ]);

    close();
    expect(source.readyState).toBe(2);
  });

  // A truncated frame is a half-written line, not a log line reading "null":
  // delivering it would put `null` in the operator's log pane — or, on the
  // status channel, a status change to nothing that clears the badge.
  it("drops a frame it cannot parse instead of delivering null", () => {
    const seen: Array<[string, unknown]> = [];
    api.connectLogStream("abc123", (event, data) => seen.push([event, data]));

    emit(created[0], "log", "{not json");
    emit(created[0], "status", "{not json");
    emit(created[0], "error", "{not json");
    expect(seen).toEqual([]);
  });

  it("subscribes the metrics stream to metrics and errors only", () => {
    const seen: Array<[string, unknown]> = [];
    const close = api.connectMetricsStream((event, data) => seen.push([event, data]));

    const source = created[0];
    expect(source.url).toBe("/sse/metrics");
    expect(Object.keys(source._listeners).sort()).toEqual(["error", "metrics"]);

    emit(source, "metrics", JSON.stringify({ cpu: { usage_percent: 12 } }));
    emit(source, "error", JSON.stringify({ message: "gone" }));
    emit(source, "metrics", "}{");
    emit(source, "error", "}{");

    expect(seen).toEqual([
      ["metrics", { cpu: { usage_percent: 12 } }],
      ["error", { message: "gone" }],
    ]);

    close();
    expect(source.readyState).toBe(2);
  });
});
