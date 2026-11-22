/** Recipes as rows: the facts a deploy is decided on, one column each, and
 *  one state per recipe — running, queued, blocked, not here, tight, or ready. */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import RecipesTable from "@/components/RecipesTable";
import type { RecipeSummary } from "@/lib/types";

function recipe(over: Partial<RecipeSummary> = {}): RecipeSummary {
  return {
    id: "bundled/qwen",
    name: "Qwen3.8-27B",
    description: "Dense 27B",
    model: "Qwen/Qwen3.8-27B",
    engine: "vllm",
    engines: ["vllm", "sglang"],
    source: "bundled",
    cluster_only: false,
    solo_only: false,
    ...over,
  } as RecipeSummary;
}

describe("RecipesTable", () => {
  it("puts the engines, the node count, what it serves and only the unusual state in columns", () => {
    const onSelect = vi.fn();
    const onReset = vi.fn();
    render(
      <RecipesTable
        recipes={[
          recipe(),
          recipe({
            id: "a",
            name: "Solo",
            description: "",
            solo_only: true,
            engines: [],
            engine_support: [
              { engine: "llama-cpp", supported: true, enabled: true },
              { engine: "vllm", supported: false, enabled: true },
            ],
            source: "unknown",
            is_customized: true,
          } as unknown as Partial<RecipeSummary>),
          recipe({ id: "b", name: "Big", cluster_only: true, engines: [] }),
          recipe({ id: "c", name: "Queued" }),
          recipe({ id: "d", name: "Far" }),
          recipe({ id: "e", name: "Tight" }),
        ]}
        stateOf={(r) =>
          ({
            "bundled/qwen": { running: true },
            b: { clusterBlocked: true },
            c: { queued: true },
            d: { notHere: "no arm64 image" },
            e: { tight: "92% of memory" },
          })[r.id] ?? {}
        }
        onSelect={onSelect}
        onReset={onReset}
      />,
    );

    const qwen = screen.getByTestId("recipe-row-bundled/qwen");
    expect(qwen).toHaveTextContent("vllm · sglang");
    expect(qwen).toHaveTextContent("One or more");
    expect(qwen).toHaveTextContent("Qwen/Qwen3.8-27B");
    expect(qwen).toHaveTextContent("Running");

    const solo = screen.getByTestId("recipe-row-a");
    expect(solo).toHaveTextContent("llama-cpp");
    expect(solo).not.toHaveTextContent("vllm");
    expect(solo).toHaveTextContent("Custom");
    // Plain "runs here" is the ordinary case and says nothing.
    expect(solo).not.toHaveTextContent("Runs here");
    fireEvent.click(within(solo).getByRole("button", { name: "Reset to original" }));
    expect(onReset).toHaveBeenCalledWith(expect.objectContaining({ id: "a" }));
    expect(onSelect).not.toHaveBeenCalled();

    const big = screen.getByTestId("recipe-row-b");
    expect(big).toHaveTextContent("—");
    expect(within(big).getByRole("button", { name: "Big" })).toBeDisabled();
    fireEvent.click(big);
    expect(onSelect).not.toHaveBeenCalled();

    expect(screen.getByTestId("recipe-row-c")).toHaveTextContent("Queued");
    expect(screen.getByTestId("recipe-row-d")).toHaveTextContent("no arm64 image");
    expect(screen.getByTestId("recipe-row-e")).toHaveTextContent(/tight/i);

    fireEvent.click(screen.getByTestId("recipe-row-d"));
    expect(onSelect).toHaveBeenLastCalledWith(expect.objectContaining({ id: "d" }));
    fireEvent.click(screen.getByRole("button", { name: "Qwen3.8-27B" }));
    expect(onSelect).toHaveBeenLastCalledWith(expect.objectContaining({ id: "bundled/qwen" }));
    fireEvent.click(screen.getByRole("button", { name: "Deploy Queued" }));
    expect(onSelect).toHaveBeenLastCalledWith(expect.objectContaining({ id: "c" }));
    expect(onSelect).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("button", { name: "Deploy Big" })).toBeDisabled();
  });

  it("names what it serves when that is not chat, and says when nothing matches", () => {
    const { rerender } = render(
      <RecipesTable
        recipes={[recipe({ id: "emb", name: "Embed", serves: "embedding" } as Partial<RecipeSummary>)]}
        stateOf={() => ({})}
        onSelect={vi.fn()}
        onReset={vi.fn()}
      />,
    );
    expect(screen.getByTestId("recipe-row-emb")).toHaveTextContent(/embedding/i);
    rerender(<RecipesTable recipes={[]} stateOf={() => ({})} onSelect={vi.fn()} onReset={vi.fn()} empty="Nothing matches." />);
    expect(screen.getByText("Nothing matches.")).toBeInTheDocument();
  });
});
