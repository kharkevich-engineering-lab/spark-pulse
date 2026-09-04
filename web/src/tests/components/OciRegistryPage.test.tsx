/** The OCI page: registries, collections, and keeping installed recipes current.
 *
 * Everything on this page mutates what the deploy path will later read, so
 * the properties worth holding are the refusals and the reports: an update
 * that would overwrite a locally-edited recipe is held back, a failed
 * install says which one failed, and nothing is claimed to have been
 * installed that the backend did not confirm.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import OciRegistryPage from "@/pages/OciRegistryPage";
import type {
  OciAutoUpdateSettings,
  OciCollection,
  OciCollectionRecipe,
  OciRecipeMeta,
  OciRegistry,
  OciUpdateCheck,
} from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchOciRegistries: vi.fn(),
  fetchOciCollections: vi.fn(),
  fetchOciMeta: vi.fn(),
  fetchOciAutoUpdateSettings: vi.fn(),
  updateOciAutoUpdateSettings: vi.fn(),
  installOciCollection: vi.fn(),
  checkOciUpdates: vi.fn(),
  applyOciUpdates: vi.fn(),
  addOciRegistry: vi.fn(),
  updateOciRegistry: vi.fn(),
  removeOciRegistry: vi.fn(),
  testOciRegistry: vi.fn(),
  runOciAutoUpdate: vi.fn(),
  fetchOciCollectionRecipes: vi.fn(),
  fetchOciRegistryVersions: vi.fn(),
  installOciRecipe: vi.fn(),
  updateOciRecipe: vi.fn(),
  uninstallOciRecipe: vi.fn(),
}));

import {
  addOciRegistry,
  applyOciUpdates,
  checkOciUpdates,
  fetchOciAutoUpdateSettings,
  fetchOciCollectionRecipes,
  fetchOciCollections,
  fetchOciMeta,
  fetchOciRegistries,
  fetchOciRegistryVersions,
  installOciCollection,
  installOciRecipe,
  removeOciRegistry,
  runOciAutoUpdate,
  testOciRegistry,
  uninstallOciRecipe,
  updateOciAutoUpdateSettings,
  updateOciRecipe,
  updateOciRegistry,
} from "@/lib/api";

const REGISTRY: OciRegistry = {
  name: "ghcr",
  url: "ghcr.io/acme/recipes",
  enabled: true,
  default: false,
  auth_type: "none",
  connected: true,
};

const COLLECTION: OciCollection = {
  name: "spark-recipes",
  version: "1.2.0",
  display_version: "v1.2.0",
  description: "Recipes for the DGX Spark",
  vendor: "acme",
  license: "Apache-2.0",
  recipe_count: 4,
  digest: "sha256:aaaa",
  registry: "ghcr",
};

const RECIPE: OciCollectionRecipe = {
  name: "qwen3-8b",
  description: "Qwen3 at 8B",
  model: "Qwen/Qwen3-8B",
  container: "vllm-node",
  recipe_version: "2",
  solo_only: true,
  cluster_only: false,
};

const META: OciRecipeMeta = {
  name: "qwen3-8b",
  source: "ghcr",
  collection: "spark-recipes",
  version: "1.1.0",
  digest: "sha256:bbbb",
  installed_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  local_changes: false,
};

const UPDATE: OciUpdateCheck = {
  collection: "spark-recipes",
  current_version: "1.1.0",
  latest_version: "1.2.0",
  current_digest: "sha256:bbbb",
  latest_digest: "sha256:aaaa",
  local_changes: false,
  added_recipes: ["qwen3-32b"],
  modified_recipes: ["qwen3-8b"],
};

const AUTO: OciAutoUpdateSettings = {
  enabled: false,
  schedule: "0 3 * * *",
  overwrite_local: false,
};

const openTab = (name: string) =>
  userEvent.click(screen.getByRole("button", { name: new RegExp(`^${name}`) }));

describe("OciRegistryPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchOciRegistries).mockResolvedValue([REGISTRY]);
    vi.mocked(fetchOciCollections).mockResolvedValue([COLLECTION]);
    vi.mocked(fetchOciMeta).mockResolvedValue([META]);
    vi.mocked(fetchOciAutoUpdateSettings).mockResolvedValue(AUTO);
    vi.mocked(checkOciUpdates).mockResolvedValue([UPDATE]);
    vi.mocked(fetchOciRegistryVersions).mockResolvedValue({ versions: ["1.2.0", "1.1.0"] } as never);
    vi.mocked(fetchOciCollectionRecipes).mockResolvedValue([RECIPE]);
    vi.mocked(installOciCollection).mockResolvedValue({} as never);
    vi.mocked(installOciRecipe).mockResolvedValue({} as never);
    vi.mocked(updateOciRecipe).mockResolvedValue({} as never);
    vi.mocked(uninstallOciRecipe).mockResolvedValue({} as never);
    vi.mocked(updateOciRegistry).mockResolvedValue(REGISTRY);
    vi.mocked(removeOciRegistry).mockResolvedValue({} as never);
    vi.mocked(addOciRegistry).mockResolvedValue(REGISTRY);
    vi.mocked(testOciRegistry).mockResolvedValue({ ok: true } as never);
    vi.mocked(runOciAutoUpdate).mockResolvedValue({ success: true, updated: 2 } as never);
    vi.mocked(updateOciAutoUpdateSettings).mockResolvedValue(AUTO);
    vi.mocked(applyOciUpdates).mockResolvedValue([
      { collection: "spark-recipes", success: true, installed: ["qwen3-8b"] },
    ]);
  });

  describe("browse", () => {
    it("lists the collections a registry offers, with what is in them", async () => {
      render(<OciRegistryPage />);

      expect(await screen.findByText("spark-recipes")).toBeInTheDocument();
      expect(screen.getByText("Recipes for the DGX Spark")).toBeInTheDocument();
      expect(screen.getByText("4 recipes")).toBeInTheDocument();
      expect(screen.getByText("Apache-2.0")).toBeInTheDocument();
    });

    it("points at Settings when no registry has produced a collection", async () => {
      vi.mocked(fetchOciCollections).mockResolvedValue([]);
      render(<OciRegistryPage />);

      expect(await screen.findByText("No collections found")).toBeInTheDocument();
      expect(
        screen.getByText("Configure registries in Settings to browse collections"),
      ).toBeInTheDocument();
    });

    it("opens a collection and lists the recipes it carries", async () => {
      render(<OciRegistryPage />);
      await userEvent.click(await screen.findByText("spark-recipes"));

      await waitFor(() =>
        expect(fetchOciCollectionRecipes).toHaveBeenCalledWith("spark-recipes", "1.2.0", "ghcr"),
      );
      expect(await screen.findByText("qwen3-8b")).toBeInTheDocument();
      expect(screen.getByText("Qwen3 at 8B")).toBeInTheDocument();
    });

    it("says a collection is empty rather than showing a blank drawer", async () => {
      vi.mocked(fetchOciCollectionRecipes).mockResolvedValue([]);
      render(<OciRegistryPage />);
      await userEvent.click(await screen.findByText("spark-recipes"));

      expect(
        await screen.findByText("No recipes found for this collection"),
      ).toBeInTheDocument();
    });

    it("offers Update and Uninstall for a recipe already installed, Install for one that is not", async () => {
      render(<OciRegistryPage />);
      await userEvent.click(await screen.findByText("spark-recipes"));
      await screen.findByText("qwen3-8b");

      expect(screen.getByRole("button", { name: /Update/ })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /Uninstall/ })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /^Install$/ })).not.toBeInTheDocument();
    });

    it("installs a single recipe from the version the collection is pinned to", async () => {
      vi.mocked(fetchOciMeta).mockResolvedValue([]);
      render(<OciRegistryPage />);
      await userEvent.click(await screen.findByText("spark-recipes"));
      await screen.findByText("qwen3-8b");

      await userEvent.click(screen.getByRole("button", { name: "Install" }));

      await waitFor(() =>
        expect(installOciRecipe).toHaveBeenCalledWith({
          collection: "spark-recipes",
          recipe: "qwen3-8b",
          version: "1.2.0",
          registry: "ghcr",
        }),
      );
    });

    it("names the recipe that failed to install rather than a bare error", async () => {
      vi.mocked(fetchOciMeta).mockResolvedValue([]);
      vi.mocked(installOciRecipe).mockRejectedValue(new Error("manifest unknown"));
      render(<OciRegistryPage />);
      await userEvent.click(await screen.findByText("spark-recipes"));
      await screen.findByText("qwen3-8b");

      await userEvent.click(screen.getByRole("button", { name: "Install" }));

      expect(await screen.findByText("Install Failed")).toBeInTheDocument();
      expect(screen.getByText("manifest unknown")).toBeInTheDocument();
    });

    it("updates one installed recipe", async () => {
      render(<OciRegistryPage />);
      await userEvent.click(await screen.findByText("spark-recipes"));
      await screen.findByText("qwen3-8b");

      await userEvent.click(screen.getByRole("button", { name: /Update/ }));

      await waitFor(() =>
        expect(updateOciRecipe).toHaveBeenCalledWith("qwen3-8b", {
          collection: "spark-recipes",
          version: "1.2.0",
          registry: "ghcr",
        }),
      );
    });

    it("reports an update the registry refused", async () => {
      vi.mocked(updateOciRecipe).mockRejectedValue(new Error("digest mismatch"));
      render(<OciRegistryPage />);
      await userEvent.click(await screen.findByText("spark-recipes"));
      await screen.findByText("qwen3-8b");

      await userEvent.click(screen.getByRole("button", { name: /Update/ }));

      expect(await screen.findByText("Update Failed")).toBeInTheDocument();
      expect(screen.getByText("digest mismatch")).toBeInTheDocument();
    });

    it("installs a whole collection and says which version landed", async () => {
      render(<OciRegistryPage />);
      await userEvent.click(await screen.findByText("spark-recipes"));

      await userEvent.click(await screen.findByRole("button", { name: /Install All Recipes/ }));

      await waitFor(() =>
        expect(installOciCollection).toHaveBeenCalledWith("spark-recipes", "1.2.0", "ghcr"),
      );
      expect(await screen.findByText("Installed spark-recipes:1.2.0")).toBeInTheDocument();
    });

    it("reports a collection install that failed", async () => {
      vi.mocked(installOciCollection).mockRejectedValue(new Error("registry unreachable"));
      render(<OciRegistryPage />);
      await userEvent.click(await screen.findByText("spark-recipes"));
      await userEvent.click(await screen.findByRole("button", { name: /Install All Recipes/ }));

      expect(await screen.findByText("registry unreachable")).toBeInTheDocument();
    });
  });

  describe("installed", () => {
    it("lists what is installed, from which collection and at what version", async () => {
      render(<OciRegistryPage />);
      await openTab("Installed");

      expect(await screen.findByText("qwen3-8b")).toBeInTheDocument();
      expect(screen.getByText("spark-recipes@1.1.0")).toBeInTheDocument();
      expect(screen.getByText("(ghcr)")).toBeInTheDocument();
    });

    it("says nothing is installed rather than showing an empty list", async () => {
      vi.mocked(fetchOciMeta).mockResolvedValue([]);
      vi.mocked(checkOciUpdates).mockResolvedValue([]);
      render(<OciRegistryPage />);
      await openTab("Installed");

      expect(await screen.findByText("No OCI recipes installed")).toBeInTheDocument();
    });

    it("marks a recipe the operator edited locally", async () => {
      vi.mocked(fetchOciMeta).mockResolvedValue([{ ...META, local_changes: true }]);
      render(<OciRegistryPage />);
      await openTab("Installed");

      expect(await screen.findByText("Modified")).toBeInTheDocument();
    });

    it("shows what an update would change before applying it", async () => {
      render(<OciRegistryPage />);
      await openTab("Installed");

      expect(await screen.findByText("Available Updates")).toBeInTheDocument();
      expect(screen.getByText("1.1.0 → 1.2.0")).toBeInTheDocument();
      expect(screen.getByText("+1")).toBeInTheDocument();
      expect(screen.getByText("~1")).toBeInTheDocument();
    });

    /** Applying an update overwrites the file; a collection the operator has
     *  edited locally would lose that edit, so Apply All is held back. */
    it("holds back Apply All while any collection carries local changes", async () => {
      vi.mocked(checkOciUpdates).mockResolvedValue([{ ...UPDATE, local_changes: true }]);
      render(<OciRegistryPage />);
      await openTab("Installed");

      await screen.findByText("Available Updates");
      expect(screen.getByRole("button", { name: "Apply All" })).toBeDisabled();
      expect(screen.getByText("Local changes")).toBeInTheDocument();
    });

    it("applies the pending updates and reports how many landed", async () => {
      vi.mocked(applyOciUpdates).mockResolvedValue([
        { collection: "spark-recipes", success: true, installed: ["qwen3-8b"] },
        { collection: "other", success: false, installed: [], error: "boom" },
      ]);
      render(<OciRegistryPage />);
      await openTab("Installed");
      await screen.findByText("Available Updates");

      await userEvent.click(screen.getByRole("button", { name: "Apply All" }));

      await waitFor(() =>
        expect(applyOciUpdates).toHaveBeenCalledWith([
          { collection: "spark-recipes", target_version: "1.2.0", registry: "" },
        ]),
      );
      expect(await screen.findByText("1 succeeded, 1 failed")).toBeInTheDocument();
    });

    it("reports an apply that never reached the registry", async () => {
      vi.mocked(applyOciUpdates).mockRejectedValue(new Error("registry unreachable"));
      render(<OciRegistryPage />);
      await openTab("Installed");
      await screen.findByText("Available Updates");

      await userEvent.click(screen.getByRole("button", { name: "Apply All" }));

      expect(await screen.findByText("registry unreachable")).toBeInTheDocument();
    });

    it("re-asks the registry when the operator checks for updates", async () => {
      render(<OciRegistryPage />);
      await openTab("Installed");
      await screen.findByText("Available Updates");
      const before = vi.mocked(checkOciUpdates).mock.calls.length;

      await userEvent.click(screen.getByRole("button", { name: "Check" }));

      await waitFor(() =>
        expect(vi.mocked(checkOciUpdates).mock.calls.length).toBeGreaterThan(before),
      );
    });

    it("uninstalls a recipe and says so", async () => {
      render(<OciRegistryPage />);
      await openTab("Installed");
      await screen.findByText("qwen3-8b");

      await userEvent.click(screen.getByRole("button", { name: "Uninstall this recipe" }));

      await waitFor(() => expect(uninstallOciRecipe).toHaveBeenCalledWith("qwen3-8b"));
      expect(await screen.findByText("Uninstalled qwen3-8b")).toBeInTheDocument();
    });

    it("reports an uninstall that failed", async () => {
      vi.mocked(uninstallOciRecipe).mockRejectedValue(new Error("recipe is deployed"));
      render(<OciRegistryPage />);
      await openTab("Installed");
      await screen.findByText("qwen3-8b");

      await userEvent.click(screen.getByRole("button", { name: "Uninstall this recipe" }));

      expect(await screen.findByText("recipe is deployed")).toBeInTheDocument();
    });
  });

  describe("settings", () => {
    it("lists the registries with their URL and the versions they carry", async () => {
      render(<OciRegistryPage />);
      await openTab("Settings");

      expect(await screen.findByText("ghcr")).toBeInTheDocument();
      expect(screen.getByText("ghcr.io/acme/recipes")).toBeInTheDocument();
      expect(screen.getByText("2 versions")).toBeInTheDocument();
    });

    it("says there are no registries rather than showing an empty box", async () => {
      vi.mocked(fetchOciRegistries).mockResolvedValue([]);
      render(<OciRegistryPage />);
      await openTab("Settings");

      expect(await screen.findByText("No registries configured")).toBeInTheDocument();
    });

    it("adds a registry, enabled, and clears the form", async () => {
      render(<OciRegistryPage />);
      await openTab("Settings");

      await userEvent.click(await screen.findByRole("button", { name: /Add Registry/ }));
      await userEvent.type(screen.getByPlaceholderText("my-registry"), "internal");
      await userEvent.type(screen.getByPlaceholderText("ghcr.io/owner/recipe-repo"), "reg.acme/x");
      await userEvent.click(screen.getByRole("button", { name: "Add" }));

      await waitFor(() =>
        expect(addOciRegistry).toHaveBeenCalledWith({
          name: "internal",
          url: "reg.acme/x",
          enabled: true,
          default: false,
          auth_type: "none",
        }),
      );
      expect(screen.queryByPlaceholderText("my-registry")).not.toBeInTheDocument();
    });

    it("will not add a registry missing a name or a URL", async () => {
      render(<OciRegistryPage />);
      await openTab("Settings");

      await userEvent.click(await screen.findByRole("button", { name: /Add Registry/ }));
      await userEvent.type(screen.getByPlaceholderText("my-registry"), "internal");

      expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    });

    it("reports a registry the backend would not accept", async () => {
      vi.mocked(addOciRegistry).mockRejectedValue(new Error("that name is taken"));
      render(<OciRegistryPage />);
      await openTab("Settings");

      await userEvent.click(await screen.findByRole("button", { name: /Add Registry/ }));
      await userEvent.type(screen.getByPlaceholderText("my-registry"), "internal");
      await userEvent.type(screen.getByPlaceholderText("ghcr.io/owner/recipe-repo"), "reg.acme/x");
      await userEvent.click(screen.getByRole("button", { name: "Add" }));

      expect(await screen.findByText("that name is taken")).toBeInTheDocument();
    });

    it("abandons the add form without writing anything", async () => {
      render(<OciRegistryPage />);
      await openTab("Settings");

      await userEvent.click(await screen.findByRole("button", { name: /Add Registry/ }));
      await userEvent.type(screen.getByPlaceholderText("my-registry"), "internal");
      await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

      expect(screen.queryByPlaceholderText("my-registry")).not.toBeInTheDocument();
      expect(addOciRegistry).not.toHaveBeenCalled();
    });

    it("toggles a registry off without deleting it", async () => {
      render(<OciRegistryPage />);
      await openTab("Settings");

      await userEvent.click(await screen.findByRole("button", { name: "Disable" }));

      await waitFor(() =>
        expect(updateOciRegistry).toHaveBeenCalledWith("ghcr", { enabled: false }),
      );
    });

    it("reports a toggle the backend rejected", async () => {
      vi.mocked(updateOciRegistry).mockRejectedValue(new Error("registries.yaml is read-only"));
      render(<OciRegistryPage />);
      await openTab("Settings");

      await userEvent.click(await screen.findByRole("button", { name: "Disable" }));

      expect(await screen.findByText("registries.yaml is read-only")).toBeInTheDocument();
    });

    it("removes a registry the operator no longer wants", async () => {
      render(<OciRegistryPage />);
      await openTab("Settings");

      await userEvent.click(await screen.findByRole("button", { name: "Remove registry" }));

      await waitFor(() => expect(removeOciRegistry).toHaveBeenCalledWith("ghcr"));
    });

    it("reports a removal the backend refused", async () => {
      vi.mocked(removeOciRegistry).mockRejectedValue(new Error("cannot remove the default"));
      render(<OciRegistryPage />);
      await openTab("Settings");

      await userEvent.click(await screen.findByRole("button", { name: "Remove registry" }));

      expect(await screen.findByText("cannot remove the default")).toBeInTheDocument();
    });

    /** An unreachable registry is why collections are missing, so a failed
     *  test says so instead of leaving the operator to guess. */
    it("names a registry that failed its connection test", async () => {
      vi.mocked(fetchOciRegistries).mockResolvedValue([{ ...REGISTRY, connected: false }]);
      vi.mocked(testOciRegistry).mockResolvedValue({ ok: false } as never);
      render(<OciRegistryPage />);
      await openTab("Settings");

      await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));

      expect(await screen.findByText("Registry ghcr is not reachable")).toBeInTheDocument();
    });

    it("reports a connection test that threw", async () => {
      vi.mocked(fetchOciRegistries).mockResolvedValue([{ ...REGISTRY, connected: false }]);
      vi.mocked(testOciRegistry).mockRejectedValue(new Error("DNS failure"));
      render(<OciRegistryPage />);
      await openTab("Settings");

      await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));

      expect(await screen.findByText("DNS failure")).toBeInTheDocument();
    });

    it("turns auto-update on", async () => {
      render(<OciRegistryPage />);
      await openTab("Settings");
      await screen.findByText("Enable Auto-Update");

      const toggle = screen
        .getByText("Enable Auto-Update")
        .closest("div")!.parentElement!.querySelector("button")!;
      await userEvent.click(toggle);

      await waitFor(() =>
        expect(updateOciAutoUpdateSettings).toHaveBeenCalledWith({ enabled: true }),
      );
    });

    it("runs auto-update on demand and reports what it changed", async () => {
      render(<OciRegistryPage />);
      await openTab("Settings");

      await userEvent.click(await screen.findByRole("button", { name: "Run Now" }));

      await waitFor(() => expect(runOciAutoUpdate).toHaveBeenCalled());
      expect(await screen.findByText("2 recipe(s) updated")).toBeInTheDocument();
    });

    it("explains an auto-update that decided to do nothing", async () => {
      vi.mocked(runOciAutoUpdate).mockResolvedValue({
        skipped: true,
        reason: "not due until 03:00",
      } as never);
      render(<OciRegistryPage />);
      await openTab("Settings");

      await userEvent.click(await screen.findByRole("button", { name: "Run Now" }));

      expect(await screen.findByText("not due until 03:00")).toBeInTheDocument();
    });

    it("reports an auto-update that failed", async () => {
      vi.mocked(runOciAutoUpdate).mockResolvedValue({
        success: false,
        error: "no registry is enabled",
      } as never);
      render(<OciRegistryPage />);
      await openTab("Settings");

      await userEvent.click(await screen.findByRole("button", { name: "Run Now" }));

      expect(await screen.findByText("no registry is enabled")).toBeInTheDocument();
    });

    it("reports an auto-update that threw", async () => {
      vi.mocked(runOciAutoUpdate).mockRejectedValue(new Error("scheduler is down"));
      render(<OciRegistryPage />);
      await openTab("Settings");

      await userEvent.click(await screen.findByRole("button", { name: "Run Now" }));

      expect(await screen.findByText("scheduler is down")).toBeInTheDocument();
    });

    it("carries on when a registry will not report its versions", async () => {
      vi.mocked(fetchOciRegistryVersions).mockRejectedValue(new Error("no tags"));
      render(<OciRegistryPage />);
      await openTab("Settings");

      expect(await screen.findByText("ghcr")).toBeInTheDocument();
      expect(screen.queryByText(/versions/)).not.toBeInTheDocument();
    });

    it("lets the operator dismiss whatever the page reported", async () => {
      render(<OciRegistryPage />);
      await openTab("Settings");
      await userEvent.click(await screen.findByRole("button", { name: "Run Now" }));
      const alert = await screen.findByText("2 recipe(s) updated");

      await userEvent.click(
        within(alert.closest('[role="dialog"]')!).getByRole("button", { name: "Close" }),
      );

      expect(screen.queryByText("2 recipe(s) updated")).not.toBeInTheDocument();
    });
  });
});
