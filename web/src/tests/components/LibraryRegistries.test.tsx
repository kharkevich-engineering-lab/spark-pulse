/** The OCI page: registries, their collections, and each recipe's state.
 *
 * Everything on this page mutates what the deploy path will later read, so
 * the properties worth holding are the refusals and the reports: a recipe the
 * operator edited is not overwritten without asking, Update all leaves it
 * alone and says so, a bulk action names each recipe that failed, and every
 * recipe offers at most one thing to do — the drawer used to offer Install on
 * recipes that were already installed.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import LibraryPage from "@/pages/LibraryPage";
import type {
  OciCollection,
  OciCollectionApplyResult,
  OciCollectionState,
  OciRecipeMeta,
  OciRecipeState,
  OciRegistry,
} from "@/lib/types";

vi.mock("@/lib/api", () => ({
  // The Library shell's own reads: Registries is a tab of it now.
  fetchModels: vi.fn(() => Promise.resolve([])),
  fetchImages: vi.fn(() => Promise.resolve([])),
  fetchCache: vi.fn(() => Promise.resolve({ entries: [] })),
  cleanCache: vi.fn(),
  fetchOciRegistries: vi.fn(),
  fetchOciCollections: vi.fn(),
  fetchOciMeta: vi.fn(),
  fetchOciCollectionState: vi.fn(),
  applyOciCollection: vi.fn(),
  addOciRegistry: vi.fn(),
  updateOciRegistry: vi.fn(),
  removeOciRegistry: vi.fn(),
  testOciRegistry: vi.fn(),
  fetchOciRegistryVersions: vi.fn(),
}));

import {
  addOciRegistry,
  applyOciCollection,
  fetchOciCollectionState,
  fetchOciCollections,
  fetchOciMeta,
  fetchOciRegistries,
  fetchOciRegistryVersions,
  removeOciRegistry,
  testOciRegistry,
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

const REGISTRY_WITH_AUTH: OciRegistry = {
  ...REGISTRY,
  auth_type: "username_password",
  auth: { type: "username_password", username: "alice" },
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

const META: OciRecipeMeta = {
  name: "qwen3-8b.yaml",
  source: "ghcr",
  collection: "spark-recipes",
  version: "1.1.0",
  digest: "sha256:bbbb",
  installed_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  local_changes: false,
};

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

/** One recipe in every state, as the simulated backend serves them. */
const RECIPES: OciRecipeState[] = [
  recipe("Qwen3.5-397B (PP=3)", "update"),
  recipe("MiniMax-M2.5", "installed"),
  recipe("Bonsai-2-27B (ternary)", "local_edits", { update_available: true }),
  recipe("Gemma-Edited", "local_edits"),
  recipe("Qwen3-Embedding-4B", "not_installed", { serves: "embedding" }),
  recipe("Llama-70B", "not_installed"),
  recipe("Old-Recipe", "removed"),
];

const STATE: OciCollectionState = {
  collection: "spark-recipes",
  registry: "ghcr",
  description: "Recipes for the DGX Spark",
  latest_version: "1.2.0",
  display_version: "v1.2.0",
  installed_version: "1.1.0",
  checked: true,
  recipes: RECIPES,
};

const applied = (
  ...results: OciCollectionApplyResult["results"]
): OciCollectionApplyResult => ({ collection: "spark-recipes", version: "1.2.0", results });

/** Destructive actions confirm. */
const confirmDialog = async (label: string) =>
  userEvent.click(await screen.findByRole("button", { name: label }));

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={["/oci"]}>
      <LibraryPage />
    </MemoryRouter>,
  );

const openCollection = async () => {
  renderPage();
  await userEvent.click(await screen.findByText("spark-recipes"));
  return screen.findByTestId("collection-view");
};

const row = (r: OciRecipeState) => screen.getByTestId(`collection-recipe-${r.recipe_id}`);

describe("Library — registries", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchOciRegistries).mockResolvedValue([REGISTRY]);
    vi.mocked(fetchOciCollections).mockResolvedValue([COLLECTION]);
    vi.mocked(fetchOciMeta).mockResolvedValue([META]);
    vi.mocked(fetchOciCollectionState).mockResolvedValue(STATE);
    vi.mocked(applyOciCollection).mockResolvedValue(applied());
    vi.mocked(fetchOciRegistryVersions).mockResolvedValue({ versions: ["1.2.0", "1.1.0"] } as never);
    vi.mocked(updateOciRegistry).mockResolvedValue(REGISTRY);
    vi.mocked(removeOciRegistry).mockResolvedValue({} as never);
    vi.mocked(addOciRegistry).mockResolvedValue(REGISTRY);
    vi.mocked(testOciRegistry).mockResolvedValue({ ok: true } as never);
  });

  describe("collections", () => {
    it("lists the collections a registry offers, with what is in them and what is installed", async () => {
      renderPage();

      expect(await screen.findByText("spark-recipes")).toBeInTheDocument();
      expect(screen.getByText("Recipes for the DGX Spark")).toBeInTheDocument();
      expect(screen.getByText("4 recipes")).toBeInTheDocument();
      expect(screen.getByText("Apache-2.0")).toBeInTheDocument();
      expect(await screen.findByText("1 installed")).toBeInTheDocument();
      // One view now: no Browse/Installed sub-tabs.
      expect(screen.queryByRole("tab", { name: /^Browse/ })).not.toBeInTheDocument();
    });

    it("points at Settings when no registry has produced a collection", async () => {
      vi.mocked(fetchOciCollections).mockResolvedValue([]);
      renderPage();

      expect(await screen.findByText("No collections found")).toBeInTheDocument();
      expect(
        screen.getByText("Configure registries in Settings to browse collections"),
      ).toBeInTheDocument();
    });
  });

  describe("the collection view", () => {
    it("asks the server for the state of the collection it opened", async () => {
      await openCollection();
      expect(fetchOciCollectionState).toHaveBeenCalledWith("spark-recipes", "ghcr", expect.anything());
      expect(screen.getByTestId("collection-versions")).toHaveTextContent("1.1.0 → 1.2.0 available");
    });

    it("shows only the newest version when nothing is behind it", async () => {
      vi.mocked(fetchOciCollectionState).mockResolvedValue({ ...STATE, installed_version: "" });
      await openCollection();
      expect(screen.getByTestId("collection-versions")).toHaveTextContent(/^1\.2\.0$/);
    });

    it("gives every recipe one state and at most one action", async () => {
      await openCollection();
      const expected: Record<string, [string, string | null]> = {
        "Qwen3.5-397B (PP=3)": ["Update available", "Update"],
        "MiniMax-M2.5": ["Installed", null],
        "Bonsai-2-27B (ternary)": ["Local edits", "Update"],
        "Gemma-Edited": ["Local edits", null],
        "Qwen3-Embedding-4B": ["Not installed", "Install"],
        "Llama-70B": ["Not installed", "Install"],
        "Old-Recipe": ["Removed upstream", null],
      };
      for (const r of RECIPES) {
        const [label, action] = expected[r.name];
        const el = row(r);
        expect(el).toHaveAttribute("data-state", r.state);
        expect(el).toHaveTextContent(label);
        const buttons = within(el).queryAllByRole("button");
        expect(buttons.map((b) => b.textContent)).toEqual(action ? [action] : []);
      }
      // Nothing on this page uninstalls: that is the Recipes page's job.
      expect(screen.queryByRole("button", { name: /Uninstall/ })).not.toBeInTheDocument();
      expect(within(row(RECIPES[6])).getByText(/Uninstall it from Recipes/)).toBeInTheDocument();
    });

    it("marks a recipe that serves something other than chat", async () => {
      await openCollection();
      expect(within(row(RECIPES[4])).getByTestId("serves-chip")).toHaveTextContent("Embeddings");
      expect(within(row(RECIPES[5])).queryByTestId("serves-chip")).not.toBeInTheDocument();
    });

    it("filters by state", async () => {
      await openCollection();
      const names = () =>
        screen
          .getAllByTestId(/^collection-recipe-/)
          .map((el) => el.getAttribute("data-state"));

      await userEvent.click(screen.getByRole("tab", { name: /^Installed/ }));
      expect(names()).toEqual(["update", "installed", "local_edits", "local_edits", "removed"]);

      await userEvent.click(screen.getByRole("tab", { name: /^Updates/ }));
      expect(names()).toEqual(["update", "local_edits"]);

      await userEvent.click(screen.getByRole("tab", { name: /^Not installed/ }));
      expect(names()).toEqual(["not_installed", "not_installed"]);

      await userEvent.click(screen.getByRole("tab", { name: /^All/ }));
      expect(names()).toHaveLength(RECIPES.length);
    });

    it("says a filter matched nothing rather than showing a blank list", async () => {
      vi.mocked(fetchOciCollectionState).mockResolvedValue({
        ...STATE,
        recipes: [recipe("MiniMax-M2.5", "installed")],
      });
      await openCollection();
      await userEvent.click(screen.getByRole("tab", { name: /^Updates/ }));
      expect(screen.getByText("No recipes match this filter.")).toBeInTheDocument();
    });

    it("says a collection is empty rather than showing a blank drawer", async () => {
      vi.mocked(fetchOciCollectionState).mockResolvedValue({ ...STATE, recipes: [] });
      await openCollection();
      expect(screen.getByText("No recipes found for this collection")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Install all/ })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Update all/ })).not.toBeInTheDocument();
    });

    it("reports a state the server could not read", async () => {
      vi.mocked(fetchOciCollectionState).mockRejectedValue(new Error("registry unreachable"));
      renderPage();
      await userEvent.click(await screen.findByText("spark-recipes"));
      expect(await screen.findByText("registry unreachable")).toBeInTheDocument();
    });

    it("says when the newest version could not be compared", async () => {
      vi.mocked(fetchOciCollectionState).mockResolvedValue({ ...STATE, checked: false });
      await openCollection();
      expect(screen.getByText(/Updates are not shown/)).toBeInTheDocument();
    });

    it("installs one recipe from the newest version, and refreshes", async () => {
      vi.mocked(applyOciCollection).mockResolvedValue(
        applied({ recipe: "Llama-70B", recipe_id: "oci-llama-70b", success: true, action: "installed" }),
      );
      await openCollection();
      await userEvent.click(within(row(RECIPES[5])).getByRole("button", { name: "Install" }));

      await waitFor(() =>
        expect(applyOciCollection).toHaveBeenCalledWith("spark-recipes", {
          recipes: ["Llama-70B"],
          version: "1.2.0",
          registry: "ghcr",
          overwrite_local: undefined,
        }),
      );
      await waitFor(() => expect(fetchOciCollectionState).toHaveBeenCalledTimes(2));
      // The card counts come from the sidecars, which just changed.
      expect(vi.mocked(fetchOciMeta).mock.calls.length).toBeGreaterThan(1);
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    it("updates a recipe with no local edits without asking", async () => {
      await openCollection();
      await userEvent.click(within(row(RECIPES[0])).getByRole("button", { name: "Update" }));
      await waitFor(() =>
        expect(applyOciCollection).toHaveBeenCalledWith(
          "spark-recipes",
          expect.objectContaining({ recipes: ["Qwen3.5-397B (PP=3)"], overwrite_local: undefined }),
        ),
      );
    });

    it("asks before an update overwrites local edits, and sends the overwrite only then", async () => {
      await openCollection();
      await userEvent.click(within(row(RECIPES[2])).getByRole("button", { name: "Update" }));

      const dialog = await screen.findByRole("dialog");
      expect(dialog).toHaveTextContent("Bonsai-2-27B (ternary) was edited here");
      expect(applyOciCollection).not.toHaveBeenCalled();

      await userEvent.click(within(dialog).getByRole("button", { name: "Update" }));
      await waitFor(() =>
        expect(applyOciCollection).toHaveBeenCalledWith(
          "spark-recipes",
          expect.objectContaining({ recipes: ["Bonsai-2-27B (ternary)"], overwrite_local: true }),
        ),
      );
    });

    it("leaves local edits alone when the overwrite is cancelled", async () => {
      await openCollection();
      await userEvent.click(within(row(RECIPES[2])).getByRole("button", { name: "Update" }));
      await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
      expect(applyOciCollection).not.toHaveBeenCalled();
    });

    it("names the recipe that failed rather than a bare error", async () => {
      vi.mocked(applyOciCollection).mockResolvedValue(
        applied({ recipe: "Llama-70B", recipe_id: "oci-llama-70b", success: false, error: "manifest unknown" }),
      );
      await openCollection();
      await userEvent.click(within(row(RECIPES[5])).getByRole("button", { name: "Install" }));

      expect(await screen.findByText("Could not apply")).toBeInTheDocument();
      expect(screen.getByText(/Llama-70B: manifest unknown/)).toBeInTheDocument();
    });

    it("reports a request that never reached the registry", async () => {
      vi.mocked(applyOciCollection).mockRejectedValue(new Error("registry unreachable"));
      await openCollection();
      await userEvent.click(within(row(RECIPES[0])).getByRole("button", { name: "Update" }));
      expect(await screen.findByText("registry unreachable")).toBeInTheDocument();
    });

    it("updates all, leaves local edits as they are, and says how many it skipped", async () => {
      vi.mocked(applyOciCollection).mockResolvedValue(
        applied(
          { recipe: "Qwen3.5-397B (PP=3)", recipe_id: "a", success: true, action: "updated" },
          { recipe: "Bonsai-2-27B (ternary)", recipe_id: "b", success: true, action: "skipped_local_edits" },
        ),
      );
      await openCollection();
      expect(screen.getByText("Update all skips local edits (1).")).toBeInTheDocument();

      await userEvent.click(screen.getByRole("button", { name: "Update all (1)" }));

      await waitFor(() =>
        expect(applyOciCollection).toHaveBeenCalledWith("spark-recipes", {
          recipes: ["Qwen3.5-397B (PP=3)", "Bonsai-2-27B (ternary)"],
          version: "1.2.0",
          registry: "ghcr",
          overwrite_local: undefined,
        }),
      );
      expect(await screen.findByText("Update finished")).toBeInTheDocument();
      expect(screen.getByText("1 done · 1 skipped for local edits · 0 failed")).toBeInTheDocument();
    });

    it("installs all the missing recipes only after asking, and names each failure", async () => {
      vi.mocked(applyOciCollection).mockResolvedValue(
        applied(
          { recipe: "Qwen3-Embedding-4B", recipe_id: "a", success: true, action: "installed" },
          { recipe: "Llama-70B", recipe_id: "b", success: false },
        ),
      );
      await openCollection();
      await userEvent.click(screen.getByRole("button", { name: "Install all (2)" }));

      const dialog = await screen.findByRole("dialog");
      expect(dialog).toHaveTextContent("Install 2 recipes from spark-recipes 1.2.0?");
      expect(applyOciCollection).not.toHaveBeenCalled();
      await userEvent.click(within(dialog).getByRole("button", { name: "Install" }));

      await waitFor(() =>
        expect(applyOciCollection).toHaveBeenCalledWith(
          "spark-recipes",
          expect.objectContaining({ recipes: ["Qwen3-Embedding-4B", "Llama-70B"] }),
        ),
      );
      expect(await screen.findByText("Install finished")).toBeInTheDocument();
      expect(screen.getByText(/1 done · 0 skipped for local edits · 1 failed/)).toBeInTheDocument();
      expect(screen.getByText(/Llama-70B: Unknown error/)).toBeInTheDocument();
    });

    it("reports a bulk request that failed outright", async () => {
      vi.mocked(applyOciCollection).mockRejectedValue(new Error("pull failed"));
      await openCollection();
      await userEvent.click(screen.getByRole("button", { name: "Update all (1)" }));
      expect(await screen.findByText("pull failed")).toBeInTheDocument();
    });

    it("offers no bulk action when there is nothing to do", async () => {
      vi.mocked(fetchOciCollectionState).mockResolvedValue({
        ...STATE,
        recipes: [recipe("MiniMax-M2.5", "installed"), recipe("Gemma-Edited", "local_edits")],
      });
      await openCollection();
      expect(screen.queryByRole("button", { name: /Update all/ })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Install all/ })).not.toBeInTheDocument();
    });

    it("closes", async () => {
      await openCollection();
      await userEvent.click(screen.getByRole("button", { name: "Close" }));
      expect(screen.queryByTestId("collection-view")).not.toBeInTheDocument();
    });
  });

  describe("the registries themselves", () => {
    it("lists the registries with their URL and the versions they carry", async () => {
      renderPage();

      expect(await screen.findByText("ghcr")).toBeInTheDocument();
      expect(screen.getByText("ghcr.io/acme/recipes")).toBeInTheDocument();
      // The versions are a second request, made once the registries land.
      expect(await screen.findByText("2 versions")).toBeInTheDocument();
    });

    it("says there are no registries rather than showing an empty box", async () => {
      vi.mocked(fetchOciRegistries).mockResolvedValue([]);
      renderPage();

      expect(await screen.findByText("No registries configured")).toBeInTheDocument();
    });

    it("adds a registry, enabled, and clears the form", async () => {
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: /Add registry/ }));
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
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: /Add registry/ }));
      await userEvent.type(screen.getByPlaceholderText("my-registry"), "internal");

      expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    });

    it("reports a registry the backend would not accept", async () => {
      vi.mocked(addOciRegistry).mockRejectedValue(new Error("that name is taken"));
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: /Add registry/ }));
      await userEvent.type(screen.getByPlaceholderText("my-registry"), "internal");
      await userEvent.type(screen.getByPlaceholderText("ghcr.io/owner/recipe-repo"), "reg.acme/x");
      await userEvent.click(screen.getByRole("button", { name: "Add" }));

      expect(await screen.findByText("that name is taken")).toBeInTheDocument();
    });

    it("abandons the add form without writing anything", async () => {
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: /Add registry/ }));
      await userEvent.type(screen.getByPlaceholderText("my-registry"), "internal");
      await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

      expect(screen.queryByPlaceholderText("my-registry")).not.toBeInTheDocument();
      expect(addOciRegistry).not.toHaveBeenCalled();
    });

    it("toggles a registry off without deleting it", async () => {
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Disable" }));

      await waitFor(() =>
        expect(updateOciRegistry).toHaveBeenCalledWith("ghcr", { enabled: false }),
      );
    });

    it("reports a toggle the backend rejected", async () => {
      vi.mocked(updateOciRegistry).mockRejectedValue(new Error("registries.yaml is read-only"));
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Disable" }));

      expect(await screen.findByText("registries.yaml is read-only")).toBeInTheDocument();
    });

    it("opens the edit dialog pre-filled, without ever showing a stored secret", async () => {
      vi.mocked(fetchOciRegistries).mockResolvedValue([REGISTRY_WITH_AUTH]);
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Edit registry" }));

      expect(screen.getByLabelText("URL")).toHaveValue("ghcr.io/acme/recipes");
      expect(screen.getByLabelText("Authentication")).toHaveValue("username_password");
      expect(screen.getByLabelText("Username")).toHaveValue("alice");
      // A username/password registry never echoes its stored password back;
      // the "new password" field always starts blank.
      expect(screen.getByLabelText(/New password/)).toHaveValue("");
    });

    it("saves only the fields the operator changed, plus a non-empty new secret", async () => {
      vi.mocked(fetchOciRegistries).mockResolvedValue([REGISTRY_WITH_AUTH]);
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Edit registry" }));
      const url = screen.getByLabelText("URL");
      await userEvent.clear(url);
      await userEvent.type(url, "ghcr.io/acme/recipes-v2");
      await userEvent.type(screen.getByLabelText(/New password/), "hunter2");
      await userEvent.click(screen.getByRole("button", { name: "Save" }));

      await waitFor(() =>
        expect(updateOciRegistry).toHaveBeenCalledWith("ghcr", {
          url: "ghcr.io/acme/recipes-v2",
          auth: { type: "username_password", username: "alice", password: "hunter2" },
        }),
      );
      // Test connection is re-run for a URL change, then the list refreshes.
      await waitFor(() => expect(testOciRegistry).toHaveBeenCalledWith("ghcr"));
      await waitFor(() => expect(screen.queryByLabelText("URL")).not.toBeInTheDocument());
    });

    it("leaves the stored secret alone when only the URL changes", async () => {
      vi.mocked(fetchOciRegistries).mockResolvedValue([REGISTRY_WITH_AUTH]);
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Edit registry" }));
      const url = screen.getByLabelText("URL");
      await userEvent.clear(url);
      await userEvent.type(url, "ghcr.io/acme/recipes-v2");
      await userEvent.click(screen.getByRole("button", { name: "Save" }));

      await waitFor(() =>
        expect(updateOciRegistry).toHaveBeenCalledWith("ghcr", {
          url: "ghcr.io/acme/recipes-v2",
        }),
      );
    });

    it("shows a backend error inline and keeps the dialog open to retry", async () => {
      vi.mocked(updateOciRegistry).mockRejectedValue(new Error("registries.yaml is read-only"));
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Edit registry" }));
      const url = screen.getByLabelText("URL");
      await userEvent.clear(url);
      await userEvent.type(url, "ghcr.io/acme/recipes-v2");
      await userEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(await screen.findByText("registries.yaml is read-only")).toBeInTheDocument();
      expect(screen.getByLabelText("URL")).toBeInTheDocument();
    });

    it("requires a username for username & password authentication", async () => {
      vi.mocked(fetchOciRegistries).mockResolvedValue([REGISTRY_WITH_AUTH]);
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Edit registry" }));
      const username = screen.getByLabelText("Username");
      await userEvent.clear(username);
      await userEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(
        await screen.findByText("Username is required for username & password authentication"),
      ).toBeInTheDocument();
      expect(updateOciRegistry).not.toHaveBeenCalled();
    });

    it("requires a URL", async () => {
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Edit registry" }));
      const url = screen.getByLabelText("URL");
      await userEvent.clear(url);
      await userEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(await screen.findByText("URL is required")).toBeInTheDocument();
      expect(updateOciRegistry).not.toHaveBeenCalled();
    });

    it("switches to token authentication with a new token", async () => {
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Edit registry" }));
      await userEvent.selectOptions(screen.getByLabelText("Authentication"), "token");
      await userEvent.type(screen.getByLabelText(/New token/), "abc123");
      await userEvent.click(screen.getByRole("button", { name: "Save" }));

      await waitFor(() =>
        expect(updateOciRegistry).toHaveBeenCalledWith("ghcr", {
          auth: { type: "token", token: "abc123" },
        }),
      );
    });

    it("requires a new secret before switching to a different authentication type", async () => {
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Edit registry" }));
      await userEvent.selectOptions(screen.getByLabelText("Authentication"), "token");
      await userEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(
        await screen.findByText("Enter a token or password before switching to this authentication type"),
      ).toBeInTheDocument();
      expect(updateOciRegistry).not.toHaveBeenCalled();
    });

    it("closes without saving when nothing changed", async () => {
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Edit registry" }));
      await userEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(updateOciRegistry).not.toHaveBeenCalled();
      await waitFor(() => expect(screen.queryByLabelText("URL")).not.toBeInTheDocument());
    });

    it("abandons the edit dialog on cancel", async () => {
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Edit registry" }));
      await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

      expect(screen.queryByLabelText("URL")).not.toBeInTheDocument();
      expect(updateOciRegistry).not.toHaveBeenCalled();
    });

    it("removes a registry the operator no longer wants", async () => {
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Remove registry" }));
      await confirmDialog("Delete");

      await waitFor(() => expect(removeOciRegistry).toHaveBeenCalledWith("ghcr"));
    });

    it("reports a removal the backend refused", async () => {
      vi.mocked(removeOciRegistry).mockRejectedValue(new Error("cannot remove the default"));
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Remove registry" }));
      await confirmDialog("Delete");

      expect(await screen.findByText("cannot remove the default")).toBeInTheDocument();
    });

    /** An unreachable registry is why collections are missing, so a failed
     *  test says so instead of leaving the operator to guess. */
    it("names a registry that failed its connection test", async () => {
      vi.mocked(fetchOciRegistries).mockResolvedValue([{ ...REGISTRY, connected: false }]);
      vi.mocked(testOciRegistry).mockResolvedValue({ ok: false } as never);
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));

      expect(await screen.findByText("ghcr is not reachable from here.")).toBeInTheDocument();
    });

    it("reports a connection test that threw", async () => {
      vi.mocked(fetchOciRegistries).mockResolvedValue([{ ...REGISTRY, connected: false }]);
      vi.mocked(testOciRegistry).mockRejectedValue(new Error("DNS failure"));
      renderPage();

      await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));

      expect(await screen.findByText("DNS failure")).toBeInTheDocument();
    });

    it("carries on when a registry will not report its versions", async () => {
      vi.mocked(fetchOciRegistryVersions).mockRejectedValue(new Error("no tags"));
      renderPage();

      expect(await screen.findByText("ghcr")).toBeInTheDocument();
      expect(screen.queryByText(/versions/)).not.toBeInTheDocument();
    });

    it("lets the operator dismiss whatever the page reported", async () => {
      vi.mocked(fetchOciRegistries).mockResolvedValue([{ ...REGISTRY, connected: false }]);
      vi.mocked(testOciRegistry).mockResolvedValue({ ok: false } as never);
      renderPage();
      await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));
      const alert = await screen.findByText("ghcr is not reachable from here.");

      await userEvent.click(
        within(alert.closest('[role="dialog"]')!).getByRole("button", { name: "Close" }),
      );

      expect(screen.queryByText("ghcr is not reachable from here.")).not.toBeInTheDocument();
    });
  });
});
