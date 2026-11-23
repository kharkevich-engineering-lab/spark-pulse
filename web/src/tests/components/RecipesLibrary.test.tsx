/** Recipes as a library page: the catalogue of what collections offer beside
 *  what is installed, and the sources it comes from.
 *
 * What is held to account: one row per recipe with one state and at most one
 * action; an update that would overwrite a local edit asks first; Update all
 * names what it skipped and what failed; a registry that will not answer
 * leaves the installed recipes where they are; and every way a source is
 * changed refreshes what the catalogue offers.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import RecipesPage, { catalogueRows } from "@/pages/RecipesPage";
import { actionOf, describeOutcome, libraryStateOf } from "@/components/library/recipeCollections";
import { translatorFor } from "@/lib/i18n";
import type {
  OciCollection,
  OciCollectionApplyResult,
  OciCollectionState,
  OciRecipeMeta,
  OciRecipeState,
  OciRegistry,
  RecipeSummary,
  Settings,
} from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ApiError: actual.ApiError,
    fetchRecipes: vi.fn(),
    fetchRecipe: vi.fn(),
    fetchDeployments: vi.fn(() => Promise.resolve([])),
    fetchScheduledDeploys: vi.fn(() => Promise.resolve([])),
    fetchModelDownloads: vi.fn(() => Promise.resolve([])),
    fetchSettings: vi.fn(),
    fetchRecipeCustomization: vi.fn(),
    listCustomRecipes: vi.fn(() => Promise.resolve([])),
    uninstallOciRecipe: vi.fn(),
    fetchOciRegistries: vi.fn(),
    fetchOciCollections: vi.fn(),
    fetchOciCollectionState: vi.fn(),
    applyOciCollection: vi.fn(),
    fetchOciMeta: vi.fn(),
    fetchOciRegistryVersions: vi.fn(),
    fetchOciAutoUpdateSettings: vi.fn(),
    updateOciAutoUpdateSettings: vi.fn(),
    runOciAutoUpdate: vi.fn(),
    addOciRegistry: vi.fn(),
    updateOciRegistry: vi.fn(),
    removeOciRegistry: vi.fn(),
    testOciRegistry: vi.fn(),
  };
});

import {
  addOciRegistry,
  applyOciCollection,
  fetchOciAutoUpdateSettings,
  fetchOciCollectionState,
  fetchOciCollections,
  fetchOciMeta,
  fetchOciRegistries,
  fetchOciRegistryVersions,
  fetchRecipes,
  fetchSettings,
  removeOciRegistry,
  runOciAutoUpdate,
  testOciRegistry,
  updateOciAutoUpdateSettings,
  updateOciRegistry,
} from "@/lib/api";

const summary = (over: Partial<RecipeSummary> = {}): RecipeSummary => ({
  id: "bundled/qwen3-8b",
  name: "Qwen3 8B",
  model: "Qwen/Qwen3-8B",
  container: "vllm-node",
  description: "Qwen3 at 8B",
  solo_only: true,
  cluster_only: false,
  mods: [],
  defaults: {},
  is_customized: false,
  recipe_version: "2",
  engine: "vllm",
  engines: ["vllm"],
  params: {},
  source: "bundled",
  engine_support: [],
  ...over,
});

const recipe = (name: string, state: OciRecipeState["state"], over: Partial<OciRecipeState> = {}): OciRecipeState => ({
  name,
  recipe_id: `oci-${name.toLowerCase().replace(/[^a-z0-9.]+/g, "-")}`,
  description: `${name} described`,
  model: "",
  container: "vllm-node",
  solo_only: false,
  cluster_only: false,
  serves: "chat",
  state,
  installed_version: state === "not_installed" ? "" : "1.1.0",
  update_available: state === "update",
  local_changes: state === "local_edits",
  ...over,
});

const BEHIND = recipe("Behind", "update");
const EDITED = recipe("Edited", "local_edits", { update_available: true });
const CURRENT = recipe("Current", "installed");
const NEW = recipe("Fresh", "not_installed");
const GONE = recipe("Gone", "removed");

const STATE: OciCollectionState = {
  collection: "spark-recipes",
  registry: "ghcr",
  description: "",
  latest_version: "1.2.0",
  display_version: "v1.2.0",
  installed_version: "1.1.0",
  checked: true,
  recipes: [BEHIND, EDITED, CURRENT, NEW, GONE],
};

const COLLECTION: OciCollection = {
  name: "spark-recipes",
  version: "1.2.0",
  display_version: "v1.2.0",
  description: "",
  vendor: "acme",
  license: "Apache-2.0",
  recipe_count: 5,
  digest: "sha256:aaaa",
  registry: "ghcr",
};

const REGISTRY: OciRegistry = {
  name: "ghcr",
  url: "ghcr.io/acme/recipes",
  enabled: true,
  default: false,
  auth_type: "none",
  connected: true,
};

const META = (name: string): OciRecipeMeta => ({
  name,
  source: "ghcr",
  collection: "spark-recipes",
  version: "1.1.0",
  digest: "sha256:bbbb",
  installed_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  local_changes: false,
});

/** Installed: one bundled, one custom, and the four a collection knows. */
const INSTALLED: RecipeSummary[] = [
  summary(),
  summary({ id: "custom-mine", name: "Mine", source: "custom" }),
  ...[BEHIND, EDITED, CURRENT, GONE].map((r) =>
    summary({ id: r.recipe_id, name: r.name, source: "oci", model: "acme/model" }),
  ),
];

const applied = (...results: OciCollectionApplyResult["results"]): OciCollectionApplyResult => ({
  collection: "spark-recipes",
  version: "1.2.0",
  results,
});

const ok = (r: OciRecipeState, action: "installed" | "updated" | "skipped_local_edits" = "updated") => ({
  recipe: r.name,
  recipe_id: r.recipe_id,
  success: true,
  action,
});

const renderPage = (entry = "/recipes") =>
  render(
    <MemoryRouter initialEntries={[entry]}>
      <RecipesPage />
    </MemoryRouter>,
  );

describe("RecipesPage as a library", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    // Cards by default; a test that wants the table says so.
    Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 1024 });
    vi.mocked(fetchRecipes).mockResolvedValue(INSTALLED);
    vi.mocked(fetchSettings).mockResolvedValue({ cluster: { available: true, node_count: 2, forced: false } } as Settings);
    vi.mocked(fetchOciRegistries).mockResolvedValue([REGISTRY]);
    vi.mocked(fetchOciCollections).mockResolvedValue([COLLECTION]);
    vi.mocked(fetchOciCollectionState).mockResolvedValue(STATE);
    vi.mocked(fetchOciMeta).mockResolvedValue([META("behind.yaml"), META("current.yaml")]);
    vi.mocked(fetchOciRegistryVersions).mockResolvedValue({ versions: ["1.2.0", "1.1.0"] });
    vi.mocked(fetchOciAutoUpdateSettings).mockResolvedValue({ enabled: false, schedule: "0 3 * * *", overwrite_local: false });
    vi.mocked(applyOciCollection).mockResolvedValue(applied(ok(BEHIND)));
    vi.mocked(updateOciRegistry).mockResolvedValue(REGISTRY);
    vi.mocked(removeOciRegistry).mockResolvedValue(undefined);
    vi.mocked(addOciRegistry).mockResolvedValue(REGISTRY);
    vi.mocked(testOciRegistry).mockResolvedValue({ ok: true });
    vi.mocked(updateOciAutoUpdateSettings).mockResolvedValue({ enabled: true, schedule: "0 3 * * *", overwrite_local: false });
  });

  describe("the catalogue", () => {
    it("lists what is installed beside what the collections offer", async () => {
      renderPage();

      expect(await screen.findByText("Fresh")).toBeInTheDocument();
      for (const name of ["Qwen3 8B", "Mine", "Behind", "Edited", "Current", "Gone"]) {
        expect(screen.getByText(name)).toBeInTheDocument();
      }
      // One chip per row, in the library's one vocabulary.
      expect(screen.getAllByTestId("library-state-available")).toHaveLength(1);
      expect(screen.getAllByTestId("library-state-update")).toHaveLength(1);
      expect(screen.getAllByTestId("library-state-removed")).toHaveLength(1);
      // Edited here, and a custom recipe: both are local.
      expect(screen.getAllByTestId("library-state-local")).toHaveLength(2);
      expect(screen.getAllByText("Installed").length).toBeGreaterThan(0);
    });

    it("filters to Installed, Available and Updates, counted", async () => {
      const user = userEvent.setup();
      renderPage();
      await screen.findByText("Fresh");

      expect(screen.getByRole("tab", { name: /^All \(7\)/ })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /^Installed \(6\)/ })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /^Available \(1\)/ })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /^Updates \(1\)/ })).toBeInTheDocument();

      await user.click(screen.getByRole("tab", { name: /^Available/ }));
      expect(screen.getByText("Fresh")).toBeInTheDocument();
      expect(screen.queryByText("Current")).toBeNull();

      await user.click(screen.getByRole("tab", { name: /^Updates/ }));
      expect(screen.getByText("Behind")).toBeInTheDocument();
      expect(screen.queryByText("Fresh")).toBeNull();

      await user.click(screen.getByRole("tab", { name: /^Installed/ }));
      expect(screen.queryByText("Fresh")).toBeNull();
      expect(screen.getByText("Current")).toBeInTheDocument();
    });

    it("keeps the installed recipes when no collection can be read", async () => {
      vi.mocked(fetchOciCollections).mockRejectedValue(new Error("registry down"));
      renderPage();

      expect(await screen.findByText("Qwen3 8B")).toBeInTheDocument();
      expect(screen.queryByText("Fresh")).toBeNull();
      expect(screen.getAllByTestId("library-state-on_nodes").length).toBeGreaterThan(0);
    });

    it("skips a collection whose state cannot be read and shows the others", async () => {
      vi.mocked(fetchOciCollections).mockResolvedValue([
        COLLECTION,
        { ...COLLECTION, name: "broken" },
      ]);
      vi.mocked(fetchOciCollectionState).mockImplementation((name) =>
        name === "broken" ? Promise.reject(new Error("no index")) : Promise.resolve(STATE),
      );
      renderPage();

      expect(await screen.findByText("Fresh")).toBeInTheDocument();
    });

    it("offers one action a row on a wide screen: Install, Update, or Deploy", async () => {
      Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 1400 });
      renderPage();
      await screen.findByText("Fresh");
      expect(screen.getByTestId("recipes-table")).toBeInTheDocument();

      expect(screen.getByRole("button", { name: "Install Fresh" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Update Behind" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Update Edited" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Deploy Current" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Deploy Gone" })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Deploy Fresh" })).toBeNull();
      // The name of a recipe nothing holds is not a way in.
      expect(screen.queryByRole("button", { name: "Fresh" })).toBeNull();
    });

    it("installs from a table row too", async () => {
      Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 1400 });
      const user = userEvent.setup();
      renderPage();

      await user.click(await screen.findByRole("button", { name: "Install Fresh" }));

      await waitFor(() => expect(applyOciCollection).toHaveBeenCalled());
    });

    it("installs an offered recipe from the newest version and refreshes", async () => {
      const user = userEvent.setup();
      vi.mocked(applyOciCollection).mockResolvedValue(applied(ok(NEW, "installed")));
      renderPage();

      await user.click(await screen.findByRole("button", { name: "Install Fresh" }));

      await waitFor(() =>
        expect(applyOciCollection).toHaveBeenCalledWith("spark-recipes", {
          recipes: ["Fresh"],
          version: "1.2.0",
          registry: "ghcr",
          overwrite_local: undefined,
        }),
      );
      await waitFor(() => expect(fetchRecipes).toHaveBeenCalledTimes(2));
      expect(fetchOciCollectionState).toHaveBeenCalledTimes(2);
    });

    it("updates a recipe without local edits without asking", async () => {
      const user = userEvent.setup();
      renderPage();

      await user.click(await screen.findByRole("button", { name: "Update Behind" }));

      await waitFor(() => expect(applyOciCollection).toHaveBeenCalled());
      expect(screen.queryByText("Overwrite local edits?")).toBeNull();
    });

    it("asks before an update overwrites a local edit, and sends the overwrite only then", async () => {
      const user = userEvent.setup();
      renderPage();

      await user.click(await screen.findByRole("button", { name: "Update Edited" }));
      expect(await screen.findByText("Overwrite local edits?")).toBeInTheDocument();
      expect(applyOciCollection).not.toHaveBeenCalled();

      await user.click(screen.getByRole("button", { name: "Cancel" }));
      expect(applyOciCollection).not.toHaveBeenCalled();

      await user.click(screen.getByRole("button", { name: "Update Edited" }));
      await user.click(await screen.findByRole("button", { name: "Update" }));
      await waitFor(() =>
        expect(applyOciCollection).toHaveBeenCalledWith(
          "spark-recipes",
          expect.objectContaining({ recipes: ["Edited"], overwrite_local: true }),
        ),
      );
    });

    it("names the recipe that failed rather than a bare error", async () => {
      const user = userEvent.setup();
      vi.mocked(applyOciCollection).mockResolvedValue(
        applied({ recipe: "Fresh", recipe_id: NEW.recipe_id, success: false, error: "digest mismatch" }),
      );
      renderPage();

      await user.click(await screen.findByRole("button", { name: "Install Fresh" }));

      expect(await screen.findByText(/Fresh: digest mismatch/)).toBeInTheDocument();
    });

    it("reports a request that never reached the registry", async () => {
      const user = userEvent.setup();
      vi.mocked(applyOciCollection).mockRejectedValue(new Error("connection refused"));
      renderPage();

      await user.click(await screen.findByRole("button", { name: "Install Fresh" }));

      expect(await screen.findByText("connection refused")).toBeInTheDocument();
    });

    it("updates all, leaves local edits alone and says how many it skipped", async () => {
      const user = userEvent.setup();
      vi.mocked(applyOciCollection).mockResolvedValue(applied(ok(BEHIND), ok(EDITED, "skipped_local_edits")));
      renderPage();

      await user.click(await screen.findByRole("button", { name: "Update all (1)" }));

      await waitFor(() =>
        expect(applyOciCollection).toHaveBeenCalledWith("spark-recipes", {
          recipes: ["Behind", "Edited"],
          version: "1.2.0",
          registry: "ghcr",
          overwrite_local: undefined,
        }),
      );
      expect(await screen.findByText("Update finished")).toBeInTheDocument();
      expect(screen.getByText("1 done · 1 skipped for local edits · 0 failed")).toBeInTheDocument();
    });

    it("reports an update-all that failed outright", async () => {
      const user = userEvent.setup();
      vi.mocked(applyOciCollection).mockRejectedValue(new Error("boom"));
      renderPage();

      await user.click(await screen.findByRole("button", { name: "Update all (1)" }));

      expect(await screen.findByText("boom")).toBeInTheDocument();
    });

    it("offers no update-all when nothing is behind", async () => {
      vi.mocked(fetchOciCollectionState).mockResolvedValue({ ...STATE, recipes: [CURRENT, NEW] });
      renderPage();

      await screen.findByText("Fresh");
      expect(screen.queryByRole("button", { name: /Update all/ })).toBeNull();
    });

    it("does not open a recipe nothing holds", async () => {
      const user = userEvent.setup();
      renderPage();

      await user.click(await screen.findByText("Fresh"));

      expect(screen.queryByRole("heading", { name: "Fresh" })).toBeNull();
    });
  });

  describe("the sources", () => {
    it("opens from the URL, titled Sources, with one row for each place recipes come from", async () => {
      renderPage("/recipes#sources");

      const table = await screen.findByTestId("recipe-sources-table");
      expect(within(table).getByTestId("source-bundled")).toBeInTheDocument();
      expect(within(table).getByTestId("source-custom")).toBeInTheDocument();
      const reg = await within(table).findByTestId("registry-ghcr");
      expect(within(reg).getByText("ghcr.io/acme/recipes")).toBeInTheDocument();
      expect(within(reg).getByText("Connected")).toBeInTheDocument();
      expect(within(reg).getByText("spark-recipes")).toBeInTheDocument();
      expect(within(reg).getByText("v1.2.0")).toBeInTheDocument();
      expect(await within(reg).findByText("2 versions")).toBeInTheDocument();
      // Two of its five recipes are installed.
      expect(within(reg).getByText("5")).toBeInTheDocument();
      expect(within(reg).getByText("2")).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /^Sources \(3\)/ })).toBeInTheDocument();
      expect(screen.queryByText("Fresh")).toBeNull();
    });

    it("says there are no registries rather than showing nothing", async () => {
      vi.mocked(fetchOciRegistries).mockResolvedValue([]);
      renderPage("/recipes#sources");

      expect(await screen.findByText("No registries configured")).toBeInTheDocument();
    });

    it("tests a registry, and names one that does not answer", async () => {
      const user = userEvent.setup();
      vi.mocked(testOciRegistry).mockResolvedValue({ ok: false });
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("button", { name: "Test the connection to ghcr" }));

      expect(await screen.findByText("ghcr is not reachable from here.")).toBeInTheDocument();
    });

    it("reports a test that threw", async () => {
      const user = userEvent.setup();
      vi.mocked(testOciRegistry).mockRejectedValue(new Error("timeout"));
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("button", { name: "Test the connection to ghcr" }));

      expect(await screen.findByText("timeout")).toBeInTheDocument();
    });

    it("disables a registry and refreshes what the catalogue offers", async () => {
      const user = userEvent.setup();
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("button", { name: "Disable ghcr" }));

      await waitFor(() => expect(updateOciRegistry).toHaveBeenCalledWith("ghcr", { enabled: false }));
      await waitFor(() => expect(fetchRecipes).toHaveBeenCalledTimes(2));
    });

    it("reports a toggle the backend refused", async () => {
      const user = userEvent.setup();
      vi.mocked(updateOciRegistry).mockRejectedValue(new Error("nope"));
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("button", { name: "Disable ghcr" }));

      expect(await screen.findByText("nope")).toBeInTheDocument();
    });

    it("saves an edited URL and re-tests it", async () => {
      const user = userEvent.setup();
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("button", { name: "Edit ghcr" }));
      const url = screen.getByLabelText("URL");
      await user.clear(url);
      await user.type(url, "ghcr.io/acme/v2");
      await user.click(screen.getByRole("button", { name: "Save" }));

      await waitFor(() =>
        expect(updateOciRegistry).toHaveBeenCalledWith("ghcr", { url: "ghcr.io/acme/v2" }),
      );
      await waitFor(() => expect(testOciRegistry).toHaveBeenCalledWith("ghcr"));
    });

    it("still saves when the re-test throws", async () => {
      const user = userEvent.setup();
      vi.mocked(testOciRegistry).mockRejectedValue(new Error("down"));
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("button", { name: "Edit ghcr" }));
      const url = screen.getByLabelText("URL");
      await user.clear(url);
      await user.type(url, "ghcr.io/acme/v2");
      await user.click(screen.getByRole("button", { name: "Save" }));

      await waitFor(() => expect(updateOciRegistry).toHaveBeenCalled());
    });

    it("removes a registry only after asking", async () => {
      const user = userEvent.setup();
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("button", { name: "Remove ghcr" }));
      expect(removeOciRegistry).not.toHaveBeenCalled();
      await user.click(await screen.findByRole("button", { name: "Delete" }));

      await waitFor(() => expect(removeOciRegistry).toHaveBeenCalledWith("ghcr"));
    });

    it("reports a removal the backend refused", async () => {
      const user = userEvent.setup();
      vi.mocked(removeOciRegistry).mockRejectedValue(new Error("in use"));
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("button", { name: "Remove ghcr" }));
      await user.click(await screen.findByRole("button", { name: "Delete" }));

      expect(await screen.findByText("in use")).toBeInTheDocument();
    });

    it("offers no removal for the default registry", async () => {
      vi.mocked(fetchOciRegistries).mockResolvedValue([{ ...REGISTRY, default: true }]);
      renderPage("/recipes#sources");

      await screen.findByRole("button", { name: "Edit ghcr" });
      expect(screen.queryByRole("button", { name: "Remove ghcr" })).toBeNull();
    });

    it("adds a source from the header, enabled", async () => {
      const user = userEvent.setup();
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("button", { name: "Add source" }));
      await user.type(screen.getByLabelText("Name"), "mine");
      await user.type(screen.getByLabelText("URL"), "ghcr.io/me/recipes");
      await user.click(screen.getByRole("button", { name: "Add" }));

      await waitFor(() =>
        expect(addOciRegistry).toHaveBeenCalledWith({
          name: "mine",
          url: "ghcr.io/me/recipes",
          enabled: true,
          default: false,
          auth_type: "none",
        }),
      );
    });

    it("will not add a source without a name and a URL, and can be abandoned", async () => {
      const user = userEvent.setup();
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("button", { name: "Add source" }));
      expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
      await user.click(screen.getByRole("button", { name: "Cancel" }));

      expect(screen.queryByLabelText("Name")).toBeNull();
      expect(addOciRegistry).not.toHaveBeenCalled();
    });

    it("reports a source the backend would not accept", async () => {
      const user = userEvent.setup();
      vi.mocked(addOciRegistry).mockRejectedValue(new Error("duplicate"));
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("button", { name: "Add source" }));
      await user.type(screen.getByLabelText("Name"), "mine");
      await user.type(screen.getByLabelText("URL"), "x");
      await user.click(screen.getByRole("button", { name: "Add" }));

      expect(await screen.findByText("duplicate")).toBeInTheDocument();
    });

    it("carries on when a registry will not report its versions", async () => {
      vi.mocked(fetchOciRegistryVersions).mockRejectedValue(new Error("no"));
      renderPage("/recipes#sources");

      expect(await screen.findByTestId("registry-ghcr")).toBeInTheDocument();
    });

    it("turns the update schedule on and saves an edited cron on blur", async () => {
      const user = userEvent.setup();
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("switch", { name: "Enable auto-update" }));
      await waitFor(() => expect(updateOciAutoUpdateSettings).toHaveBeenCalledWith({ enabled: true }));

      const schedule = screen.getByLabelText("Schedule (cron)");
      await user.clear(schedule);
      await user.type(schedule, "0 4 * * *");
      expect(updateOciAutoUpdateSettings).toHaveBeenCalledTimes(1);
      await user.tab();
      await waitFor(() => expect(updateOciAutoUpdateSettings).toHaveBeenCalledWith({ schedule: "0 4 * * *" }));
    });

    it("saves nothing when the schedule is left as it was", async () => {
      const user = userEvent.setup();
      renderPage("/recipes#sources");

      const schedule = await screen.findByLabelText("Schedule (cron)");
      await user.click(schedule);
      await user.tab();

      expect(updateOciAutoUpdateSettings).not.toHaveBeenCalled();
    });

    it("reports a schedule or a switch the backend refused", async () => {
      const user = userEvent.setup();
      vi.mocked(updateOciAutoUpdateSettings).mockRejectedValue(new Error("bad cron"));
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("switch", { name: "Enable auto-update" }));
      expect(await screen.findByText("bad cron")).toBeInTheDocument();

      const schedule = screen.getByLabelText("Schedule (cron)");
      await user.type(schedule, "x");
      await user.tab();
      await waitFor(() => expect(updateOciAutoUpdateSettings).toHaveBeenCalledTimes(2));
    });

    it("runs the check now and says what it did", async () => {
      const user = userEvent.setup();
      vi.mocked(runOciAutoUpdate).mockResolvedValue({ success: true, updated: 2 });
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("button", { name: "Run now" }));

      expect(await screen.findByText("2 recipe(s) updated.")).toBeInTheDocument();
      await waitFor(() => expect(fetchRecipes).toHaveBeenCalledTimes(2));
    });

    it("says a check was skipped, and why a failed one failed", async () => {
      const user = userEvent.setup();
      vi.mocked(runOciAutoUpdate).mockResolvedValueOnce({ skipped: true, reason: "disabled" });
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("button", { name: "Run now" }));
      expect(await screen.findByText("disabled")).toBeInTheDocument();

      vi.mocked(runOciAutoUpdate).mockResolvedValueOnce({ success: false, error: "registry down" });
      await user.click(screen.getByRole("button", { name: "Run now" }));
      expect(await screen.findByText("registry down")).toBeInTheDocument();

      vi.mocked(runOciAutoUpdate).mockRejectedValueOnce(new Error("lost"));
      await user.click(screen.getByRole("button", { name: "Run now" }));
      expect(await screen.findByText("lost")).toBeInTheDocument();
    });

    it("says a skip with no reason in its own words", async () => {
      const user = userEvent.setup();
      vi.mocked(runOciAutoUpdate).mockResolvedValue({ skipped: true });
      renderPage("/recipes#sources");

      await user.click(await screen.findByRole("button", { name: "Run now" }));

      expect(await screen.findByText("Skipped.")).toBeInTheDocument();
    });

    it("shows the header's Add source only on the sources", async () => {
      renderPage();
      await screen.findByText("Fresh");
      expect(screen.queryByRole("button", { name: "Add source" })).toBeNull();
      expect(screen.getByRole("button", { name: "New recipe" })).toBeInTheDocument();
    });
  });
});

describe("catalogueRows", () => {
  it("lays a collection's state over the recipe the listing carries, and nothing else", () => {
    const listed = [summary({ id: BEHIND.recipe_id, name: "Behind", source: "oci" })];
    const rows = catalogueRows(listed, [{ ...STATE, recipes: [BEHIND, CURRENT] }]);
    expect(rows.map((r) => [r.summary.name, r.library, r.action])).toEqual([["Behind", "update", "update"]]);
  });

  it("does not offer a recipe that is already installed under the same id", () => {
    const rows = catalogueRows([summary({ id: NEW.recipe_id, name: "Fresh", source: "oci" })], [STATE]);
    expect(rows.filter((r) => r.library === "available")).toHaveLength(0);
  });

  it("offers a recipe once per collection that carries it", () => {
    const rows = catalogueRows([], [STATE, { ...STATE, collection: "other" }]);
    expect(rows.filter((r) => r.library === "available")).toHaveLength(2);
  });

  it("reads an installed recipe's state from the first collection that knows it", () => {
    const rows = catalogueRows(
      [summary({ id: CURRENT.recipe_id, name: "Current", source: "oci" })],
      [{ ...STATE, recipes: [CURRENT] }, { ...STATE, recipes: [{ ...CURRENT, state: "update" }] }],
    );
    expect(rows[0].library).toBe("on_nodes");
  });
});

describe("recipeCollections", () => {
  it("names each state in the library's words", () => {
    expect(libraryStateOf("installed")).toBe("on_nodes");
    expect(libraryStateOf("not_installed")).toBe("available");
    expect(libraryStateOf("update")).toBe("update");
    expect(libraryStateOf("local_edits")).toBe("local");
    expect(libraryStateOf("removed")).toBe("removed");
  });

  it("offers an action only where there is something to do", () => {
    expect(actionOf(NEW)).toBe("install");
    expect(actionOf(BEHIND)).toBe("update");
    expect(actionOf(EDITED)).toBe("update");
    expect(actionOf(recipe("Edited only", "local_edits"))).toBeNull();
    expect(actionOf(CURRENT)).toBeNull();
    expect(actionOf(GONE)).toBeNull();
  });

  it("counts across collections and names each failure, with a reason or without", () => {
    const { t } = translatorFor("en");
    const text = describeOutcome(
      [
        applied(ok(BEHIND), ok(EDITED, "skipped_local_edits")),
        applied({ recipe: "A", recipe_id: "oci-a", success: false, error: "bad" }, { recipe: "B", recipe_id: "oci-b", success: false }),
      ],
      t,
    );
    expect(text).toBe("1 done · 1 skipped for local edits · 2 failed\nA: bad\nB: Unknown error");
  });
});
