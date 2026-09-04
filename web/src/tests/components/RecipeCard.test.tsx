/** RecipeCard: what an operator can tell about a recipe without opening it.
 *
 * The badges are a routing decision, not decoration. "Solo"/"Cluster" says
 * whether this install can run it at all, the engine list says what it will
 * run on, and "Custom" says the recipe on disk is no longer the one that
 * shipped. Getting the engine list from `engine_support` rather than from
 * `engines` is the part that matters: the backend already knows which engines
 * are both able and switched on, and the card must not offer one that is not.
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import RecipeCard from "@/components/RecipeCard";
import type { RecipeSummary } from "@/lib/types";

const recipe = (over: Partial<RecipeSummary> = {}): RecipeSummary => ({
  id: "bundled/qwen3-8b",
  name: "Qwen3-8B",
  model: "Qwen/Qwen3-8B",
  container: "vllm-node",
  description: "A small instruct model.",
  solo_only: false,
  cluster_only: false,
  mods: [],
  defaults: {},
  is_customized: false,
  recipe_version: "2",
  engine: "vllm",
  engines: ["vllm", "sglang"],
  params: {},
  source: "bundled",
  engine_support: [],
  ...over,
});

describe("RecipeCard", () => {
  it("names the recipe, its description and its container", () => {
    render(
      <RecipeCard r={recipe()} isRunning={false} clusterBlocked={false} onSelect={vi.fn()} />,
    );

    expect(screen.getByText("Qwen3-8B")).toBeInTheDocument();
    expect(screen.getByText("A small instruct model.")).toBeInTheDocument();
    expect(screen.getByText("vllm-node")).toBeInTheDocument();
  });

  it("falls back to the model when a recipe carries no description", () => {
    render(
      <RecipeCard
        r={recipe({ description: "" })}
        isRunning={false}
        clusterBlocked={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("Qwen/Qwen3-8B")).toBeInTheDocument();
  });

  it("opens the recipe when clicked", () => {
    const onSelect = vi.fn();
    render(
      <RecipeCard r={recipe()} isRunning={false} clusterBlocked={false} onSelect={onSelect} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Qwen3-8B/ }));
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("marks a recipe that is already running", () => {
    render(<RecipeCard r={recipe()} isRunning clusterBlocked={false} onSelect={vi.fn()} />);
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  /** A cluster-only recipe on a single node: shown, explained, and not
   *  openable — the card says why rather than failing at deploy time. */
  it("says 'Cluster only' and refuses to open when this install cannot run it", () => {
    const onSelect = vi.fn();
    render(
      <RecipeCard
        r={recipe({ cluster_only: true })}
        isRunning={false}
        clusterBlocked
        onSelect={onSelect}
      />,
    );

    expect(screen.getByText("Cluster only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Qwen3-8B/ })).not.toBeInTheDocument();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("shows a recipe with no restriction as running both solo and clustered", () => {
    render(
      <RecipeCard r={recipe()} isRunning={false} clusterBlocked={false} onSelect={vi.fn()} />,
    );
    expect(screen.getByText("Solo")).toBeInTheDocument();
    expect(screen.getByText("Cluster")).toBeInTheDocument();
  });

  it("shows only Solo for a solo-only recipe", () => {
    render(
      <RecipeCard
        r={recipe({ solo_only: true })}
        isRunning={false}
        clusterBlocked={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("Solo")).toBeInTheDocument();
    expect(screen.queryByText("Cluster")).not.toBeInTheDocument();
  });

  /** `engine_support` is the backend's verdict. An engine that cannot run the
   *  recipe, or that is switched off, must not appear on the card. */
  it("lists only the engines the backend says can run it", () => {
    render(
      <RecipeCard
        r={recipe({
          engine_support: [
            { engine: "vllm", supported: true, reason: "", enabled: true },
            { engine: "sglang", supported: false, reason: "recipe pins vllm", enabled: true },
            { engine: "trtllm", supported: true, reason: "", enabled: false },
          ],
        })}
        isRunning={false}
        clusterBlocked={false}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.queryByText(/sglang/)).not.toBeInTheDocument();
    expect(screen.queryByText(/trtllm/)).not.toBeInTheDocument();
    // One usable engine is not a choice, so no engine badge is drawn at all.
    expect(screen.queryByText(/vllm ·/)).not.toBeInTheDocument();
  });

  it("names the alternatives when more than one engine can run it", () => {
    render(
      <RecipeCard
        r={recipe({
          engine_support: [
            { engine: "vllm", supported: true, reason: "", enabled: true },
            { engine: "sglang", supported: true, reason: "", enabled: true },
          ],
        })}
        isRunning={false}
        clusterBlocked={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("vllm · sglang")).toBeInTheDocument();
  });

  /** Older payloads have no `engine_support`; the declared engine list is the
   *  only thing available and must still be shown. */
  it("falls back to the declared engines when the backend reports no verdict", () => {
    render(
      <RecipeCard r={recipe()} isRunning={false} clusterBlocked={false} onSelect={vi.fn()} />,
    );
    expect(screen.getByText("vllm · sglang")).toBeInTheDocument();
  });

  it("names a non-upstream source, and stays quiet about upstream", () => {
    const { unmount } = render(
      <RecipeCard
        r={recipe({ source: "oci" })}
        isRunning={false}
        clusterBlocked={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("oci")).toBeInTheDocument();
    unmount();

    render(
      <RecipeCard
        r={recipe({ source: "upstream" })}
        isRunning={false}
        clusterBlocked={false}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.queryByText("upstream")).not.toBeInTheDocument();
  });

  /** Resetting a customised recipe throws away local edits, so the button
   *  must not also open the drawer on its way. */
  it("resets a customised recipe without opening it", () => {
    const onSelect = vi.fn();
    const onReset = vi.fn();
    render(
      <RecipeCard
        r={recipe({ is_customized: true })}
        isRunning={false}
        clusterBlocked={false}
        onSelect={onSelect}
        onReset={onReset}
      />,
    );

    expect(screen.getByText("Custom")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("Reset to original"));
    expect(onReset).toHaveBeenCalledTimes(1);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("offers no reset for a recipe that was never customised", () => {
    render(
      <RecipeCard
        r={recipe()}
        isRunning={false}
        clusterBlocked={false}
        onSelect={vi.fn()}
        onReset={vi.fn()}
      />,
    );
    expect(screen.queryByTitle("Reset to original")).not.toBeInTheDocument();
  });
});
