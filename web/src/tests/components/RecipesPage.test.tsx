/** The recipes page: the deploy an operator starts, and the gate that stops it.
 *
 * This is the only page a deployment is started from, so the properties held
 * here are the ones that decide whether a deploy happens at all:
 *
 * * **A blocked pre-flight is a report, not an alert.** A 409 carries the
 *   same checks the preview showed plus a stop sign; reducing it to a
 *   one-line "Error" would take away the only thing that tells the operator
 *   which node to go and fix. Every other failure *is* a one-line alert.
 * * **"Deploy anyway" re-issues the same deploy.** Same name, same nodes,
 *   same engine — only `skip_preflight` differs. An override that quietly
 *   changed the deployment would be worse than no override.
 * * **A cluster-only recipe on a solo install is hidden, not broken.** It is
 *   collapsed behind a count rather than offered and then refused.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import RecipesPage from "@/pages/RecipesPage";
import type {
  CustomModInfo,
  CustomRecipeInfo,
  Deployment,
  ModSummary,
  PreflightReport,
  RecipeDetail,
  RecipeSummary,
  Settings,
} from "@/lib/types";

vi.mock("@/lib/api", async () => {
  // `RecipesPage` narrows a failed create with `e instanceof ApiError`, so the
  // class has to be the real one — a stub would make every failure look like
  // a generic error and the pre-flight gate would never render.
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ApiError: actual.ApiError,
    fetchRecipes: vi.fn(),
    fetchRecipe: vi.fn(),
    fetchDeployments: vi.fn(),
    createDeployment: vi.fn(),
    fetchSettings: vi.fn(),
    fetchRecipeCustomization: vi.fn(),
    saveRecipeCustomization: vi.fn(),
    deleteRecipeCustomization: vi.fn(),
    fetchMods: vi.fn(),
    fetchMod: vi.fn(),
    listCustomRecipes: vi.fn(),
    saveCustomRecipe: vi.fn(),
    deleteCustomRecipe: vi.fn(),
    listCustomMods: vi.fn(),
    getCustomModFiles: vi.fn(),
    saveCustomModFiles: vi.fn(),
    deleteCustomMod: vi.fn(),
    getCustomRecipeContent: vi.fn(),
    importRecipes: vi.fn(),
    fetchRecipeImportStatus: vi.fn(),
    fetchEngines: vi.fn(),
    fetchModels: vi.fn(),
    fetchNodes: vi.fn(),
    planDeployment: vi.fn(),
    runPreflight: vi.fn(),
  };
});

vi.mock("@/components/LazyCodeEditor", () => ({
  default: ({
    value,
    onChange,
  }: {
    value: string;
    onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
  }) => <textarea aria-label="Recipe YAML" value={value} onChange={onChange} />,
}));

import {
  ApiError,
  createDeployment,
  deleteCustomMod,
  deleteCustomRecipe,
  deleteRecipeCustomization,
  fetchDeployments,
  fetchEngines,
  fetchMod,
  fetchModels,
  fetchMods,
  fetchNodes,
  fetchRecipe,
  fetchRecipeCustomization,
  fetchRecipeImportStatus,
  fetchRecipes,
  fetchSettings,
  getCustomModFiles,
  getCustomRecipeContent,
  listCustomMods,
  listCustomRecipes,
  saveCustomModFiles,
  saveCustomRecipe,
  saveRecipeCustomization,
} from "@/lib/api";

const summary = (over: Partial<RecipeSummary> = {}): RecipeSummary => ({
  id: "bundled/qwen3-8b",
  name: "Qwen3 8B",
  model: "Qwen/Qwen3-8B",
  container: "vllm-node",
  description: "Qwen3 at 8B, one GPU",
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

const detail = (over: Partial<RecipeDetail> = {}): RecipeDetail =>
  ({
    ...summary(),
    command: "",
    env: {},
    build_args: [],
    min_nodes: null,
    engine_specs: {},
    ...over,
  }) as RecipeDetail;

const SOLO = summary();
const CLUSTER_ONLY = summary({
  id: "bundled/big-mesh",
  name: "Big Mesh",
  solo_only: false,
  cluster_only: true,
});

const MOD: ModSummary = {
  id: "flash-attn",
  description: "Swap in the fused attention kernel",
  files: [{ name: "run.sh", kind: "script" }],
  has_patches: true,
};

const SETTINGS = { cluster_enabled: false } as Settings;

const CUSTOM_RECIPE: CustomRecipeInfo = {
  id: "custom/mine",
  name: "Mine",
  filename: "mine.yaml",
  filepath: "/cfg/mine.yaml",
  created_at: 0,
};

const CUSTOM_MOD: CustomModInfo = {
  id: "custom/my-mod",
  name: "My Mod",
  description: "does a thing",
  filepath: "/cfg/mods/my-mod",
  has_run_sh: true,
};

const blockedReport = (): PreflightReport => ({
  verdict: "blocked",
  summary: "blocked: 1 check failed on spark-02",
  can_proceed: false,
  delays: false,
  estimated_transfer_bytes: 0,
  counts: { pass: 8, warn: 0, fail: 1 },
  nodes: [
    { id: "peer-1", label: "spark-02", address: "10.0.0.11", is_control_plane: false, ranks: [1] },
  ],
  checks: [],
  blocking: [
    {
      id: "docker",
      title: "Docker",
      node: "spark-02",
      node_id: "peer-1",
      status: "fail",
      observed: "docker is not installed on spark-02",
      remedy: "Install Docker on spark-02 and add the spark user to the docker group.",
      delay_bytes: 0,
      costs_time: false,
      detail: {},
    },
  ],
  delaying: [],
  advisories: [],
  plan: {
    recipe_id: "bundled/qwen3-8b",
    engine: "vllm",
    variant: "default",
    image_ref: "ghcr.io/acme/vllm:0.1.0",
    model: "Qwen/Qwen3-8B",
    port: 9000,
    rendezvous_port: 29501,
    node_count: 2,
  },
  checked_at: "2026-01-01T00:00:00Z",
});

/** The 409 the create endpoint returns when the pre-flight gate stops it. */
const gateError = (report: PreflightReport) =>
  new ApiError(409, "API 409: docker is missing on spark-02", {
    detail: { message: "docker is missing on spark-02", preflight: report },
  });

const openDeployDrawer = async (name = "Qwen3 8B") => {
  await userEvent.click(await screen.findByText(name));
  return screen.findByRole("heading", { name });
};

describe("RecipesPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(fetchRecipes).mockResolvedValue([SOLO]);
    vi.mocked(fetchDeployments).mockResolvedValue([]);
    vi.mocked(fetchSettings).mockResolvedValue(SETTINGS);
    vi.mocked(fetchMods).mockResolvedValue([MOD]);
    vi.mocked(fetchRecipe).mockResolvedValue(detail());
    vi.mocked(fetchRecipeCustomization).mockResolvedValue({});
    vi.mocked(fetchRecipeImportStatus).mockResolvedValue({
      imported: false,
      imported_at: null,
    } as never);
    vi.mocked(listCustomRecipes).mockResolvedValue([CUSTOM_RECIPE]);
    vi.mocked(listCustomMods).mockResolvedValue([CUSTOM_MOD]);
    vi.mocked(getCustomModFiles).mockResolvedValue({
      files: { "run.sh": "#!/bin/bash\n" },
      id: CUSTOM_MOD.id,
    });
    vi.mocked(getCustomRecipeContent).mockResolvedValue({
      content: "name: Mine\n",
      id: CUSTOM_RECIPE.id,
    });
    vi.mocked(createDeployment).mockResolvedValue({} as Deployment);
    vi.mocked(saveRecipeCustomization).mockResolvedValue({});
    vi.mocked(deleteRecipeCustomization).mockResolvedValue({ deleted: true });
    vi.mocked(saveCustomRecipe).mockResolvedValue({ saved: true });
    vi.mocked(deleteCustomRecipe).mockResolvedValue({ deleted: true });
    vi.mocked(saveCustomModFiles).mockResolvedValue({ saved: true });
    vi.mocked(deleteCustomMod).mockResolvedValue({ deleted: true });
    vi.mocked(fetchEngines).mockResolvedValue({ engines: [] } as never);
    vi.mocked(fetchModels).mockResolvedValue([]);
    vi.mocked(fetchNodes).mockResolvedValue([]);
    vi.mocked(fetchMod).mockResolvedValue({
      ...MOD,
      script: "#!/bin/bash\necho patched\n",
    });
  });

  describe("listing", () => {
    it("lists each recipe with what it runs and where", async () => {
      render(<RecipesPage />);

      expect(await screen.findByText("Qwen3 8B")).toBeInTheDocument();
      expect(screen.getByText("Qwen3 at 8B, one GPU")).toBeInTheDocument();
      expect(screen.getByText("vllm-node")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /Recipes \(1\)/ })).toBeInTheDocument();
    });

    it("marks a recipe that is already running", async () => {
      vi.mocked(fetchDeployments).mockResolvedValue([
        { id: "d1", recipe_id: SOLO.id, status: "running" } as Deployment,
      ]);
      render(<RecipesPage />);

      expect(await screen.findByText("Running")).toBeInTheDocument();
    });

    /** A cluster-only recipe cannot start on a solo install, so it is folded
     *  away behind a count rather than offered and then refused. */
    it("folds cluster-only recipes away on a solo install", async () => {
      vi.mocked(fetchRecipes).mockResolvedValue([SOLO, CLUSTER_ONLY]);
      render(<RecipesPage />);

      await screen.findByText("Qwen3 8B");
      expect(screen.queryByText("Big Mesh")).not.toBeInTheDocument();

      await userEvent.click(
        screen.getByRole("button", { name: /Show 1 unavailable recipe \(cluster only\)/ }),
      );
      expect(screen.getByText("Big Mesh")).toBeInTheDocument();
      expect(screen.getByText("Cluster only")).toBeInTheDocument();

      await userEvent.click(screen.getByRole("button", { name: /Hide 1 unavailable/ }));
      expect(screen.queryByText("Big Mesh")).not.toBeInTheDocument();
    });

    it("offers every recipe once the cluster is enabled", async () => {
      vi.mocked(fetchRecipes).mockResolvedValue([SOLO, CLUSTER_ONLY]);
      vi.mocked(fetchSettings).mockResolvedValue({ cluster_enabled: true } as Settings);
      render(<RecipesPage />);

      expect(await screen.findByText("Big Mesh")).toBeInTheDocument();
      expect(screen.queryByText(/unavailable recipe/)).not.toBeInTheDocument();
    });

    it("points at Settings when there are no recipes at all", async () => {
      vi.mocked(fetchRecipes).mockResolvedValue([]);
      render(<RecipesPage />);

      expect(await screen.findByText("No recipes found.")).toBeInTheDocument();
      expect(screen.getByText(/Check spark-vllm-docker path in Settings/)).toBeInTheDocument();
    });

    it("surfaces a recipe list the backend could not produce", async () => {
      vi.mocked(fetchRecipes).mockRejectedValue(new Error("recipes dir unreadable"));
      render(<RecipesPage />);

      expect(await screen.findByText("recipes dir unreadable")).toBeInTheDocument();
    });
  });

  describe("mods tab", () => {
    it("lists the mods with their assets", async () => {
      render(<RecipesPage />);
      await userEvent.click(await screen.findByRole("button", { name: /Mods \(1\)/ }));

      expect(screen.getByText("flash-attn")).toBeInTheDocument();
      expect(screen.getByText("Swap in the fused attention kernel")).toBeInTheDocument();
      expect(screen.getByText("patches")).toBeInTheDocument();
      expect(screen.getByText("run.sh")).toBeInTheDocument();
    });

    it("says there are no mods rather than showing an empty grid", async () => {
      vi.mocked(fetchMods).mockResolvedValue([]);
      render(<RecipesPage />);
      await userEvent.click(await screen.findByRole("button", { name: /Mods \(0\)/ }));

      expect(screen.getByText("No mods found")).toBeInTheDocument();
    });

    /** A mod is a shell script that runs inside the container as root; the
     *  drawer exists so an operator can read it before applying it. */
    it("opens a mod and shows the script it would run", async () => {
      render(<RecipesPage />);
      await userEvent.click(await screen.findByRole("button", { name: /Mods \(1\)/ }));
      await userEvent.click(screen.getByText("flash-attn"));

      await waitFor(() => expect(fetchMod).toHaveBeenCalledWith("flash-attn"));
      expect(await screen.findByText(/echo patched/)).toBeInTheDocument();
    });

    it("reports a mod whose script could not be read", async () => {
      vi.mocked(fetchMod).mockRejectedValue(new Error("mod dir vanished"));
      render(<RecipesPage />);
      await userEvent.click(await screen.findByRole("button", { name: /Mods \(1\)/ }));
      await userEvent.click(screen.getByText("flash-attn"));

      expect(await screen.findByText(/mod dir vanished/)).toBeInTheDocument();
    });

    /** The script is the thing an operator wants in a terminal next, so the
     *  drawer copies it rather than making them select it out of a <pre>. */
    it("copies the mod's script to the clipboard", async () => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
      render(<RecipesPage />);
      await userEvent.click(await screen.findByRole("button", { name: /Mods \(1\)/ }));
      await userEvent.click(screen.getByText("flash-attn"));
      await screen.findByText(/echo patched/);

      await userEvent.click(screen.getByRole("button", { name: /Copy/ }));

      await waitFor(() => expect(writeText).toHaveBeenCalledWith("#!/bin/bash\necho patched\n"));
      expect(await screen.findByText("Copied")).toBeInTheDocument();
      vi.unstubAllGlobals();
    });
  });

  describe("deploying", () => {
    it("creates the deployment the drawer describes and closes it", async () => {
      render(<RecipesPage />);
      await openDeployDrawer();

      await userEvent.click(screen.getByRole("button", { name: "Deploy" }));

      await waitFor(() => expect(createDeployment).toHaveBeenCalled());
      expect(vi.mocked(createDeployment).mock.calls[0][0]).toMatchObject({
        recipe_id: "bundled/qwen3-8b",
        name: "Qwen3 8B",
      });
      await waitFor(() =>
        expect(screen.queryByRole("button", { name: "Deploy" })).not.toBeInTheDocument(),
      );
    });

    it("reduces an ordinary failure to one line the operator can read", async () => {
      vi.mocked(createDeployment).mockRejectedValue(
        new ApiError(400, "API 400: port 9000 is already bound", { detail: "port 9000" }),
      );
      render(<RecipesPage />);
      await openDeployDrawer();

      await userEvent.click(screen.getByRole("button", { name: "Deploy" }));

      expect(await screen.findByText("API 400: port 9000 is already bound")).toBeInTheDocument();
      expect(screen.queryByTestId("preflight-block-modal")).not.toBeInTheDocument();
    });

    it("shows the pre-flight report, not an alert, when the gate refuses", async () => {
      vi.mocked(createDeployment).mockRejectedValue(gateError(blockedReport()));
      render(<RecipesPage />);
      await openDeployDrawer();

      await userEvent.click(screen.getByRole("button", { name: "Deploy" }));

      const modal = await screen.findByTestId("preflight-block-modal");
      expect(
        within(modal).getByRole("heading", { name: "Pre-flight blocked this deploy" }),
      ).toBeInTheDocument();
      // The report names the node and what to do about it — the whole point
      // of showing the report rather than the message.
      expect(within(modal).getByText("spark-02")).toBeInTheDocument();
      expect(within(modal).getByText(/Install Docker on spark-02/)).toBeInTheDocument();
      expect(within(modal).getByTestId("preflight-verdict")).toHaveTextContent("Blocked");
    });

    /** The override has to survive the drawer closing under it. The drawer
     *  calls `onClose()` the moment `onDeploy` resolves, and a gated create
     *  *resolves* — the page handles the 409 itself rather than rethrowing —
     *  so by the time the report is on screen the drawer is gone. The blocked
     *  deploy therefore has to carry its own recipe; reading it back off the
     *  page's selection left this button doing nothing at all. */
    it("re-issues the identical deploy with the gate skipped", async () => {
      vi.mocked(createDeployment)
        .mockRejectedValueOnce(gateError(blockedReport()))
        .mockResolvedValueOnce({ id: "d-1" } as unknown as Deployment);
      render(<RecipesPage />);
      await openDeployDrawer();
      await userEvent.click(screen.getByRole("button", { name: "Deploy" }));
      await screen.findByTestId("preflight-block-modal");

      // The drawer has already closed itself; the override stands alone.
      expect(screen.queryByRole("button", { name: "Deploy options" })).not.toBeInTheDocument();

      await userEvent.click(screen.getByRole("button", { name: "Deploy anyway" }));

      await waitFor(() => expect(createDeployment).toHaveBeenCalledTimes(2));
      const [first, second] = vi.mocked(createDeployment).mock.calls.map(([body]) => body);
      // Same deploy, one flag different. An override that quietly changed the
      // recipe, the name or the nodes would be worse than no override.
      expect(second).toEqual({ ...first, skip_preflight: true });
      await waitFor(() =>
        expect(screen.queryByTestId("preflight-block-modal")).not.toBeInTheDocument(),
      );
    });

    it("falls back to a plain alert when even the override is refused", async () => {
      vi.mocked(createDeployment)
        .mockRejectedValueOnce(gateError(blockedReport()))
        .mockRejectedValueOnce(new Error("API 500: docker daemon is gone"));
      render(<RecipesPage />);
      await openDeployDrawer();
      await userEvent.click(screen.getByRole("button", { name: "Deploy" }));
      await screen.findByTestId("preflight-block-modal");

      await userEvent.click(screen.getByRole("button", { name: "Deploy anyway" }));

      expect(await screen.findByText(/docker daemon is gone/)).toBeInTheDocument();
      // The report goes: it is no longer what is standing in the way.
      expect(screen.queryByTestId("preflight-block-modal")).not.toBeInTheDocument();
    });

    it("lets the operator back out of a blocked deploy", async () => {
      vi.mocked(createDeployment).mockRejectedValue(gateError(blockedReport()));
      render(<RecipesPage />);
      await openDeployDrawer();
      await userEvent.click(screen.getByRole("button", { name: "Deploy" }));
      await screen.findByTestId("preflight-block-modal");

      await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

      expect(screen.queryByTestId("preflight-block-modal")).not.toBeInTheDocument();
      expect(createDeployment).toHaveBeenCalledTimes(1);
    });

    /** A 409 whose body is not a report is still just an error; treating it
     *  as a gate would render an empty modal with an override button. */
    it("treats a 409 without a report as an ordinary failure", async () => {
      vi.mocked(createDeployment).mockRejectedValue(
        new ApiError(409, "API 409: conflict", { detail: "conflict" }),
      );
      render(<RecipesPage />);
      await openDeployDrawer();

      await userEvent.click(screen.getByRole("button", { name: "Deploy" }));

      expect(await screen.findByText("API 409: conflict")).toBeInTheDocument();
      expect(screen.queryByTestId("preflight-block-modal")).not.toBeInTheDocument();
    });

    it("says so when the recipe itself could not be opened", async () => {
      vi.mocked(fetchRecipe).mockRejectedValue(new Error("recipe file is malformed"));
      render(<RecipesPage />);

      await userEvent.click(await screen.findByText("Qwen3 8B"));

      expect(await screen.findByText("recipe file is malformed")).toBeInTheDocument();
    });
  });

  describe("customization", () => {
    it("saves the edited fields and reopens the recipe as customized", async () => {
      render(<RecipesPage />);
      await openDeployDrawer();

      await userEvent.click(screen.getByRole("button", { name: "Customize" }));
      const model = screen.getByDisplayValue("Qwen/Qwen3-8B");
      await userEvent.clear(model);
      await userEvent.type(model, "Qwen/Qwen3-32B");
      await userEvent.click(screen.getByRole("button", { name: "Save" }));

      await waitFor(() =>
        expect(saveRecipeCustomization).toHaveBeenCalledWith("bundled/qwen3-8b", {
          model: "Qwen/Qwen3-32B",
        }),
      );
    });

    it("reports a customization the backend would not store", async () => {
      vi.mocked(saveRecipeCustomization).mockRejectedValue(new Error("config dir is read-only"));
      render(<RecipesPage />);
      await openDeployDrawer();

      await userEvent.click(screen.getByRole("button", { name: "Customize" }));
      await userEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(await screen.findByText("config dir is read-only")).toBeInTheDocument();
    });

    /** Resetting from inside the drawer leaves the drawer open, so the page
     *  re-reads the recipe: what the drawer names afterwards is the
     *  *original* recipe, and the edit session is over. */
    it("reopens the original recipe after a reset from inside the drawer", async () => {
      vi.mocked(fetchRecipe)
        .mockResolvedValueOnce(detail({ model: "Qwen/Qwen3-32B", is_customized: true }))
        .mockResolvedValue(detail());
      vi.mocked(fetchRecipeCustomization).mockResolvedValue({ model: "Qwen/Qwen3-32B" });
      render(<RecipesPage />);
      await openDeployDrawer();
      expect(screen.getByText("Qwen/Qwen3-32B")).toBeInTheDocument();

      await userEvent.click(screen.getByRole("button", { name: "Edit Custom" }));
      await userEvent.click(screen.getByRole("button", { name: "Reset" }));
      await userEvent.click(screen.getAllByRole("button", { name: "Reset" }).at(-1)!);

      await waitFor(() =>
        expect(deleteRecipeCustomization).toHaveBeenCalledWith("bundled/qwen3-8b"),
      );
      expect(await screen.findByText("Qwen/Qwen3-8B")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Deploy" })).toBeInTheDocument();
    });

    /** Reset deletes work the operator did by hand, so the card's reset
     *  affordance asks before it calls the API. */
    it("asks before resetting a customized recipe from its card", async () => {
      vi.mocked(fetchRecipes).mockResolvedValue([summary({ is_customized: true })]);
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getByRole("button", { name: "Reset to original" }));
      expect(
        screen.getByText(/Reset "Qwen3 8B" to its original recipe\?/),
      ).toBeInTheDocument();
      expect(deleteRecipeCustomization).not.toHaveBeenCalled();

      await userEvent.click(screen.getByRole("button", { name: "Reset" }));
      await waitFor(() =>
        expect(deleteRecipeCustomization).toHaveBeenCalledWith("bundled/qwen3-8b"),
      );
    });

    it("reports a reset the backend refused", async () => {
      vi.mocked(fetchRecipes).mockResolvedValue([summary({ is_customized: true })]);
      vi.mocked(deleteRecipeCustomization).mockRejectedValue(new Error("nothing to reset"));
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getByRole("button", { name: "Reset to original" }));
      await userEvent.click(screen.getByRole("button", { name: "Reset" }));

      expect(await screen.findByText("nothing to reset")).toBeInTheDocument();
    });
  });

  describe("custom mode", () => {
    const enterCustomMode = async () => {
      await userEvent.click(await screen.findByRole("button", { name: "Toggle custom mode" }));
      await waitFor(() => expect(listCustomRecipes).toHaveBeenCalled());
    };

    it("swaps the bundled recipes for the operator's own", async () => {
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");

      await enterCustomMode();

      expect(await screen.findByText("Mine")).toBeInTheDocument();
      expect(screen.getByText("mine.yaml")).toBeInTheDocument();
      expect(screen.queryByText("Qwen3 8B")).not.toBeInTheDocument();
      expect(screen.getByText("Browse your custom recipes and mods")).toBeInTheDocument();
    });

    it("says there are none rather than showing an empty grid", async () => {
      vi.mocked(listCustomRecipes).mockResolvedValue([]);
      vi.mocked(listCustomMods).mockResolvedValue([]);
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");

      await enterCustomMode();

      expect(await screen.findByText("No custom recipes")).toBeInTheDocument();
      await userEvent.click(screen.getByRole("button", { name: /Mods \(0\)/ }));
      expect(screen.getByText("No custom mods")).toBeInTheDocument();
    });

    it("surfaces a custom directory it could not read", async () => {
      vi.mocked(listCustomRecipes).mockRejectedValue(new Error("config dir missing"));
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getByRole("button", { name: "Toggle custom mode" }));

      expect(await screen.findByText("config dir missing")).toBeInTheDocument();
    });

    it("edits a custom recipe's YAML and writes it back", async () => {
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");
      await enterCustomMode();

      await userEvent.click(await screen.findByText("Mine"));
      const editor = await screen.findByLabelText("Recipe YAML");
      await waitFor(() => expect(editor).toHaveValue("name: Mine\n"));
      await userEvent.clear(editor);
      await userEvent.type(editor, "name: Renamed");
      await userEvent.click(screen.getByRole("button", { name: /Save/ }));

      await waitFor(() =>
        expect(saveCustomRecipe).toHaveBeenCalledWith("custom/mine", "name: Renamed"),
      );
    });

    it("deletes a custom recipe once the operator confirms", async () => {
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");
      await enterCustomMode();

      await userEvent.click(await screen.findByText("Mine"));
      await screen.findByLabelText("Recipe YAML");
      await userEvent.click(screen.getByRole("button", { name: /Delete/ }));
      await userEvent.click(screen.getAllByRole("button", { name: "Delete" }).at(-1)!);

      await waitFor(() => expect(deleteCustomRecipe).toHaveBeenCalledWith("custom/mine"));
    });

    it("opens a custom mod with the files it holds and saves them together", async () => {
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");
      await enterCustomMode();
      await userEvent.click(screen.getByRole("button", { name: /Mods \(1\)/ }));

      await userEvent.click(await screen.findByText("My Mod"));

      await waitFor(() => expect(getCustomModFiles).toHaveBeenCalledWith("custom/my-mod"));
      const editor = await screen.findByRole("textbox");
      await userEvent.clear(editor);
      await userEvent.type(editor, "#!/bin/sh");
      await userEvent.click(screen.getByRole("button", { name: /Save/ }));

      await waitFor(() =>
        expect(saveCustomModFiles).toHaveBeenCalledWith("custom/my-mod", {
          "run.sh": "#!/bin/sh",
        }),
      );
    });

    it("reports a custom mod whose files could not be read", async () => {
      vi.mocked(getCustomModFiles).mockRejectedValue(new Error("gone"));
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");
      await enterCustomMode();
      await userEvent.click(screen.getByRole("button", { name: /Mods \(1\)/ }));

      await userEvent.click(await screen.findByText("My Mod"));

      expect(await screen.findByText("Failed to load mod")).toBeInTheDocument();
    });

    it("deletes a custom mod once the operator confirms", async () => {
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");
      await enterCustomMode();
      await userEvent.click(screen.getByRole("button", { name: /Mods \(1\)/ }));
      await userEvent.click(await screen.findByText("My Mod"));
      await screen.findByRole("textbox");

      await userEvent.click(screen.getByRole("button", { name: /Delete/ }));
      await userEvent.click(screen.getAllByRole("button", { name: "Delete" }).at(-1)!);

      await waitFor(() => expect(deleteCustomMod).toHaveBeenCalledWith("custom/my-mod"));
    });

    it("reports a custom recipe the drawer could not read", async () => {
      vi.mocked(getCustomRecipeContent).mockRejectedValue(new Error("mine.yaml is gone"));
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");
      await enterCustomMode();

      await userEvent.click(await screen.findByText("Mine"));

      expect(await screen.findByText("mine.yaml is gone")).toBeInTheDocument();
    });

    it("closes a custom recipe drawer without writing anything", async () => {
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");
      await enterCustomMode();
      await userEvent.click(await screen.findByText("Mine"));
      await screen.findByLabelText("Recipe YAML");

      await userEvent.click(
        screen
          .getAllByRole("button")
          .find((b) => b.textContent === "" && b.querySelector("svg.lucide-x"))!,
      );

      expect(screen.queryByLabelText("Recipe YAML")).not.toBeInTheDocument();
      expect(saveCustomRecipe).not.toHaveBeenCalled();
    });

    /** The list is read from disk, so a newly written recipe only appears
     *  once the page re-reads it — the modal closing is not enough. */
    it("re-reads the custom directory after a new recipe is written", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) }),
      );
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");
      await enterCustomMode();
      const before = vi.mocked(listCustomRecipes).mock.calls.length;

      await userEvent.click(await screen.findByRole("button", { name: /New Recipe/ }));
      const zone = screen.getByText(/Drag and drop a YAML file here/).parentElement!;
      fireEvent.drop(zone, {
        dataTransfer: {
          files: [new File(["name: Fresh\n"], "fresh.yaml", { type: "text/yaml" })],
        },
      });
      await screen.findByRole("heading", { name: "Preview Recipe" });
      await userEvent.click(screen.getByRole("button", { name: /Save Recipe/ }));

      await waitFor(() =>
        expect(vi.mocked(listCustomRecipes).mock.calls.length).toBeGreaterThan(before),
      );
      expect(screen.queryByRole("heading", { name: "Preview Recipe" })).not.toBeInTheDocument();
      vi.unstubAllGlobals();
    });

    it("abandons the new-recipe form without writing anything", async () => {
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");
      await enterCustomMode();

      await userEvent.click(await screen.findByRole("button", { name: /New Recipe/ }));
      expect(screen.getByRole("heading", { name: "Upload Recipe" })).toBeInTheDocument();

      await userEvent.click(screen.getAllByRole("button", { name: "Cancel" })[0]);
      expect(screen.queryByRole("heading", { name: "Upload Recipe" })).not.toBeInTheDocument();
    });

    it("re-reads the custom directory after a new mod is written", async () => {
      vi.stubGlobal(
        "fetch",
        vi
          .fn()
          .mockResolvedValue({ ok: true, status: 200, json: async () => ({ id: "custom/m", name: "m" }) }),
      );
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");
      await enterCustomMode();
      await userEvent.click(screen.getByRole("button", { name: /Mods \(1\)/ }));
      const before = vi.mocked(listCustomMods).mock.calls.length;

      await userEvent.click(await screen.findByRole("button", { name: /New Mod/ }));
      await userEvent.type(screen.getByPlaceholderText("my-mod"), "fresh");
      await userEvent.click(screen.getByRole("button", { name: /Create Mod/ }));

      await waitFor(() =>
        expect(vi.mocked(listCustomMods).mock.calls.length).toBeGreaterThan(before),
      );
      expect(screen.queryByRole("heading", { name: "New Mod" })).not.toBeInTheDocument();
      vi.unstubAllGlobals();
    });

    it("abandons the new-mod form without writing anything", async () => {
      render(<RecipesPage />);
      await screen.findByText("Qwen3 8B");
      await enterCustomMode();
      await userEvent.click(screen.getByRole("button", { name: /Mods \(1\)/ }));

      await userEvent.click(await screen.findByRole("button", { name: /New Mod/ }));
      expect(screen.getByRole("heading", { name: "New Mod" })).toBeInTheDocument();

      await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
      expect(screen.queryByRole("heading", { name: "New Mod" })).not.toBeInTheDocument();
    });
  });
});
