/** The deploy drawer: what is offered, what is refused, and what is confirmed.
 *
 * The drawer is where a recipe becomes a deployment, so the properties that
 * matter are the ones that stop an operator doing the wrong thing: a recipe
 * already running cannot be deployed again, a cluster-only recipe on a solo
 * install says why rather than failing at the API, and resetting a
 * customization asks first because it is not undoable.
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import RecipeDrawer from "@/components/RecipeDrawer";
import type { RecipeDetail } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchEngines: vi.fn().mockResolvedValue({ engines: [] }),
  fetchModels: vi.fn().mockResolvedValue([]),
  fetchNodes: vi.fn().mockResolvedValue([]),
  planDeployment: vi.fn(),
  runPreflight: vi.fn(),
}));

vi.mock("@/components/LazyCodeEditor", () => ({
  default: ({ value }: { value: string }) => <textarea readOnly value={value} />,
}));

const RECIPE = {
  id: "bundled/qwen3-8b",
  name: "Qwen3 8B",
  model: "Qwen/Qwen3-8B",
  container: "vllm-node",
  command: "",
  description: "",
  mods: [],
  defaults: {},
  params: {},
  env: {},
  build_args: [],
  solo_only: false,
  cluster_only: false,
  is_customized: false,
  recipe_version: "2",
  engine: "vllm",
  engines: ["vllm"],
  source: "bundled",
  engine_support: [],
  min_nodes: null,
  engine_specs: {},
} as unknown as RecipeDetail;

function renderDrawer(props: Partial<React.ComponentProps<typeof RecipeDrawer>> = {}) {
  const onClose = vi.fn();
  const onError = vi.fn();
  const onDeploy = vi.fn().mockResolvedValue(undefined);
  const onSaveCustomization = vi.fn();
  const onReset = vi.fn().mockResolvedValue(undefined);
  const result = render(
    <RecipeDrawer
      recipe={RECIPE}
      customization={{}}
      isRunning={false}
      clusterEnabled={false}
      onClose={onClose}
      onError={onError}
      onDeploy={onDeploy}
      onSaveCustomization={onSaveCustomization}
      onReset={onReset}
      {...props}
    />,
  );
  return { ...result, onClose, onError, onDeploy, onSaveCustomization, onReset };
}

describe("RecipeDrawer", () => {
  it("names the recipe and its model in the header", () => {
    renderDrawer();
    expect(screen.getByRole("heading", { name: "Qwen3 8B" })).toBeInTheDocument();
    expect(screen.getByText("Qwen/Qwen3-8B")).toBeInTheDocument();
  });

  it("deploys under the name the form holds and closes itself", async () => {
    const { onDeploy, onClose } = renderDrawer();

    await userEvent.click(screen.getByRole("button", { name: "Deploy" }));

    await waitFor(() => expect(onDeploy).toHaveBeenCalled());
    expect(onDeploy.mock.calls[0][0]).toBe("Qwen3 8B");
    expect(onClose).toHaveBeenCalled();
  });

  /** The drawer used to send `params: {}` no matter what the deploy options
   *  showed, so the parallelism an operator set in the form never reached the
   *  create — and a two-node deploy was refused with no control that could
   *  fix it. */
  it("carries the parallelism the deploy options are showing", async () => {
    const recipe = {
      ...RECIPE,
      defaults: { tensor_parallel: 2, pipeline_parallel: 1 },
      params: { tensor_parallel: 2, pipeline_parallel: 1 },
    } as unknown as RecipeDetail;
    const { onDeploy } = renderDrawer({ recipe });

    await userEvent.click(screen.getByRole("button", { name: "Deploy" }));

    await waitFor(() => expect(onDeploy).toHaveBeenCalled());
    expect(onDeploy.mock.calls[0][1]).toEqual({ tensor_parallel: 2, pipeline_parallel: 1 });
  });

  it("carries a parallelism the operator raised by hand", async () => {
    const { onDeploy } = renderDrawer();

    await userEvent.click(screen.getByRole("button", { name: /deploy options/i }));
    const tp = await screen.findByLabelText("Tensor parallel");
    await userEvent.clear(tp);
    await userEvent.type(tp, "2");
    await userEvent.click(screen.getByRole("button", { name: "Deploy" }));

    await waitFor(() => expect(onDeploy).toHaveBeenCalled());
    expect(onDeploy.mock.calls[0][1]).toMatchObject({ tensor_parallel: 2 });
  });

  /** A failed deploy has to reach the page's alert; a drawer that swallowed
   *  it would close on a deployment that never started. */
  it("hands a failed deploy to the page rather than closing quietly", async () => {
    const onDeploy = vi.fn().mockRejectedValue(new Error("port 9000 is taken"));
    const { onError, onClose } = renderDrawer({ onDeploy });

    await userEvent.click(screen.getByRole("button", { name: "Deploy" }));

    await waitFor(() => expect(onError).toHaveBeenCalledWith("port 9000 is taken"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("refuses to deploy a recipe that is already running", () => {
    renderDrawer({ isRunning: true });
    expect(screen.getByRole("button", { name: "Deploy" })).toBeDisabled();
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  /** A cluster-only recipe on a solo install cannot start, and saying so in
   *  the drawer is cheaper than a 400 after the operator has clicked. */
  it("explains a cluster-only recipe on a solo install instead of letting it fail", () => {
    const clusterOnly = { ...RECIPE, cluster_only: true } as RecipeDetail;
    renderDrawer({ recipe: clusterOnly, clusterEnabled: false });

    expect(screen.getByText("This recipe requires cluster mode.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deploy" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Customize" })).toBeDisabled();
  });

  it("allows a cluster-only recipe once the cluster is enabled", () => {
    const clusterOnly = { ...RECIPE, cluster_only: true } as RecipeDetail;
    renderDrawer({ recipe: clusterOnly, clusterEnabled: true });

    expect(screen.queryByText("This recipe requires cluster mode.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deploy" })).toBeEnabled();
  });

  it("offers Customize for a stock recipe and Edit Custom for a customized one", () => {
    const { unmount } = renderDrawer();
    expect(screen.getByRole("button", { name: "Customize" })).toBeInTheDocument();
    unmount();

    renderDrawer({ customization: { model: "Qwen/Qwen3-32B" } });
    expect(screen.getByRole("button", { name: "Edit Custom" })).toBeInTheDocument();
  });

  it("swaps Deploy for Save once editing starts, and hides the deploy options", async () => {
    renderDrawer();
    expect(screen.getByRole("button", { name: "Deploy options" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Customize" }));

    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Deploy" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Deploy options" })).not.toBeInTheDocument();
  });

  it("leaves edit mode once a save succeeds", async () => {
    renderDrawer({ customization: { model: "m" } });

    await userEvent.click(screen.getByRole("button", { name: "Edit Custom" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Deploy" })).toBeInTheDocument(),
    );
  });

  it("stays in edit mode and reports a save that failed", async () => {
    const onSaveCustomization = vi.fn().mockRejectedValue(new Error("disk full"));
    const { onError } = renderDrawer({ onSaveCustomization, customization: { model: "m" } });

    await userEvent.click(screen.getByRole("button", { name: "Edit Custom" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onError).toHaveBeenCalledWith("disk full"));
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  /** Reset deletes a customization the operator may have spent an afternoon
   *  on, so it asks — and names the recipe it is about to revert. */
  it("asks before discarding a customization, and names it", async () => {
    const { onReset } = renderDrawer({ customization: { model: "m" } });

    await userEvent.click(screen.getByRole("button", { name: "Edit Custom" }));
    await userEvent.click(screen.getByRole("button", { name: "Reset" }));

    expect(screen.getByText(/Reset "Qwen3 8B" to its original recipe\?/)).toBeInTheDocument();
    expect(onReset).not.toHaveBeenCalled();

    await userEvent.click(screen.getAllByRole("button", { name: "Reset" }).at(-1)!);
    await waitFor(() => expect(onReset).toHaveBeenCalled());
  });

  it("keeps the customization when the reset prompt is dismissed", async () => {
    const { onReset } = renderDrawer({ customization: { model: "m" } });

    await userEvent.click(screen.getByRole("button", { name: "Edit Custom" }));
    await userEvent.click(screen.getByRole("button", { name: "Reset" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByText(/to its original recipe/)).not.toBeInTheDocument();
    expect(onReset).not.toHaveBeenCalled();
  });

  it("offers no Reset for a recipe that was never customized", async () => {
    renderDrawer({ customization: {} });
    await userEvent.click(screen.getByRole("button", { name: "Customize" }));
    expect(screen.queryByRole("button", { name: "Reset" })).not.toBeInTheDocument();
  });

  it("closes when the operator dismisses it", async () => {
    const { onClose } = renderDrawer();
    // The header's ✕ is the only button with no accessible name.
    const dismiss = screen
      .getAllByRole("button")
      .find((b) => b.textContent === "" && b.querySelector("svg.lucide-x"))!;
    await userEvent.click(dismiss);
    expect(onClose).toHaveBeenCalled();
  });

  it("shows no deploy affordance at all when the page offers no deploy", () => {
    renderDrawer({ onDeploy: undefined });
    expect(screen.queryByRole("button", { name: "Deploy" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Deploy options" })).not.toBeInTheDocument();
  });
});
