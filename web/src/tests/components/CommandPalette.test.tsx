/** ⌘K: a way into a recipe's drawer, a run, a node or a page.
 *
 * It must never deploy by itself — a deploy item navigates to the drawer,
 * where the options and the pre-flight are — and each of its three sources
 * may fail without taking the others with it.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import CommandPalette, { buildItems, matches, openCommandPalette } from "@/components/CommandPalette";
import type { ClusterNode, Deployment, RecipeSummary } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchRecipes: vi.fn(),
  fetchDeployments: vi.fn(),
  fetchNodes: vi.fn(),
}));

import { fetchDeployments, fetchNodes, fetchRecipes } from "@/lib/api";

const RECIPES = [
  { id: "bundled/qwen", name: "Qwen3.8-27B", engine: "vllm", engines: ["vllm"], model: "Qwen/Qwen3.8-27B" },
  { id: "oci-bonsai", name: "Bonsai-2-27B", engine: null, engines: ["llama-cpp"], model: "prism/bonsai" },
  { id: "bundled/huge", name: "Huge", engine: "vllm", engines: [], model: "x", fit: { verdict: "wont_run", reason: "", engines: {} } },
] as unknown as RecipeSummary[];

const RUNS = [
  { id: "old", name: "old run", status: "stopped", engine: "vllm" },
  { id: "live", name: "live run", status: "running", phase: "running", engine: "vllm" },
] as unknown as Deployment[];

const NODES = [{ id: "n1", name: "spark-02", address: "10.0.0.2" }] as unknown as ClusterNode[];

function Where() {
  const location = useLocation();
  return <p data-testid="where">{location.pathname + location.search}</p>;
}

function show() {
  return render(
    <MemoryRouter>
      <CommandPalette />
      <Where />
    </MemoryRouter>,
  );
}

const t = (key: string) => key;

describe("buildItems and matches", () => {
  it("lists runnable recipes, live runs first, nodes and pages", () => {
    const items = buildItems(t, RECIPES, RUNS, NODES);
    const deploy = items.filter((i) => i.group === "deploy");
    expect(deploy.map((i) => i.label)).toEqual(["Qwen3.8-27B", "Bonsai-2-27B"]);
    expect(deploy[0].href).toBe("/recipes?recipe=bundled%2Fqwen");
    expect(deploy[1].hint).toBe("llama-cpp · prism/bonsai");
    expect(items.filter((i) => i.group === "runs").map((i) => i.label)).toEqual(["live run", "old run"]);
    expect(items.find((i) => i.group === "nodes")?.hint).toBe("10.0.0.2");
    expect(items.some((i) => i.id === "page:/settings")).toBe(true);
  });

  it("matches every word, anywhere in the label or the hint", () => {
    const [qwen] = buildItems(t, RECIPES, [], []);
    expect(matches(qwen, "qwen vllm")).toBe(true);
    expect(matches(qwen, "  QWEN  ")).toBe(true);
    expect(matches(qwen, "qwen sglang")).toBe(false);
    expect(matches({ id: "x", group: "pages", label: "Runs", href: "/jobs" }, "run")).toBe(true);
  });
});

describe("CommandPalette", () => {
  beforeEach(() => {
    vi.mocked(fetchRecipes).mockResolvedValue(RECIPES);
    vi.mocked(fetchDeployments).mockResolvedValue(RUNS);
    vi.mocked(fetchNodes).mockResolvedValue(NODES);
  });

  it("is closed until asked, and opens on ⌘K", async () => {
    show();
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(await screen.findByText("Qwen3.8-27B")).toBeInTheDocument();
    // Ctrl+K toggles it shut again.
    fireEvent.keyDown(window, { key: "K", ctrlKey: true });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens from a button, filters, and Enter goes to the recipe's drawer", async () => {
    show();
    act(() => openCommandPalette());
    const input = await screen.findByRole("combobox");
    await screen.findByText("Bonsai-2-27B");
    fireEvent.change(input, { target: { value: "bonsai" } });
    expect(screen.queryByText("Qwen3.8-27B")).toBeNull();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("where")).toHaveTextContent("/recipes?recipe=oci-bonsai");
  });

  it("moves the selection with the arrows and opens a run with a click", async () => {
    show();
    act(() => openCommandPalette());
    const input = await screen.findByRole("combobox");
    fireEvent.change(input, { target: { value: "run" } });
    const options = await screen.findAllByRole("option");
    expect(options[0]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(screen.getAllByRole("option")[1]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowUp" });
    fireEvent.keyDown(input, { key: "ArrowUp" });
    fireEvent.keyDown(input, { key: "ArrowUp" });
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(screen.getAllByRole("option")[0]).toHaveAttribute("aria-selected", "true");
    fireEvent.mouseEnter(screen.getByText("live run"));
    fireEvent.click(screen.getByText("live run"));
    expect(screen.getByTestId("where")).toHaveTextContent("/jobs/live");
  });

  it("says so when nothing matches, and Enter then does nothing", async () => {
    show();
    act(() => openCommandPalette());
    const input = await screen.findByRole("combobox");
    fireEvent.change(input, { target: { value: "zzzz" } });
    expect(screen.getByText("Nothing matches.")).toBeInTheDocument();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("closes on Escape and on a click outside", async () => {
    show();
    act(() => openCommandPalette());
    const input = await screen.findByRole("combobox");
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();

    act(() => openCommandPalette());
    const dialog = await screen.findByRole("dialog");
    fireEvent.mouseDown(dialog);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.mouseDown(dialog.parentElement!);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("still offers the pages when every source fails", async () => {
    vi.mocked(fetchRecipes).mockRejectedValue(new Error("down"));
    vi.mocked(fetchDeployments).mockRejectedValue(new Error("down"));
    vi.mocked(fetchNodes).mockRejectedValue(new Error("down"));
    show();
    act(() => openCommandPalette());
    expect(await screen.findByText("Settings")).toBeInTheDocument();
    expect(screen.queryByText("Qwen3.8-27B")).toBeNull();
  });

  it("caps each group at six until the query narrows it", async () => {
    const many = Array.from({ length: 9 }, (_, i) => ({
      id: `r${i}`,
      name: `Recipe ${i}`,
      engine: "vllm",
      engines: ["vllm"],
      model: "m",
    })) as unknown as RecipeSummary[];
    vi.mocked(fetchRecipes).mockResolvedValue(many);
    show();
    act(() => openCommandPalette());
    await screen.findByText("Recipe 0");
    expect(screen.queryByText("Recipe 6")).toBeNull();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "recipe" } });
    expect(screen.getByText("Recipe 8")).toBeInTheDocument();
  });
});
