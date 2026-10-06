/** What a recipe or a run serves, and what that changes on the page.
 *
 * Chat is the default and shows nothing. Anything else gets a chip — on the
 * recipe card, in the drawer's header and on the run row — and a run that does
 * not serve chat is not offered a benchmark, because llama-benchy drives chat
 * completions and the backend would refuse it with a 409.
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import ServesChip from "@/components/ServesChip";
import RunRow from "@/components/RunRow";
import RecipeCard from "@/components/RecipeCard";
import BenchmarkLauncher from "@/components/BenchmarkLauncher";
import { servesChat } from "@/lib/utils";
import type { Deployment, RecipeSummary } from "@/lib/types";

vi.mock("@/lib/api", () => ({ runBenchmark: vi.fn() }));

const run = (over: Partial<Deployment> = {}): Deployment => ({
  id: "dep-1",
  recipe_id: "oci-qwen3-embedding-4b",
  name: "qwen3-embedding",
  params: {},
  nodes: null,
  status: "running",
  pid: null,
  port: 8001,
  created_at: "2026-10-06T10:00:00Z",
  started_at: "2026-10-06T10:00:00Z",
  stopped_at: null,
  error_message: null,
  ...over,
});

const recipe = (over: Partial<RecipeSummary> = {}): RecipeSummary => ({
  id: "oci-qwen3-embedding-4b",
  name: "Qwen3-Embedding-4B",
  model: "Qwen/Qwen3-Embedding-4B",
  container: "vllm-node",
  description: "Embeddings.",
  solo_only: true,
  cluster_only: false,
  mods: [],
  defaults: {},
  is_customized: false,
  recipe_version: "2",
  engine: "vllm",
  engines: ["vllm"],
  params: {},
  source: "oci",
  engine_support: [],
  ...over,
});

const row = (r: Deployment) =>
  render(
    <RunRow run={r} expanded={false} onToggle={vi.fn()} onTeardown={vi.fn()} onBenchmark={vi.fn()} />,
  );

describe("servesChat", () => {
  it("reads absence and chat as chat, and nothing else", () => {
    expect(servesChat(undefined)).toBe(true);
    expect(servesChat(null)).toBe(true);
    expect(servesChat("")).toBe(true);
    expect(servesChat("chat")).toBe(true);
    expect(servesChat("embedding")).toBe(false);
  });
});

describe("ServesChip", () => {
  it("says nothing for chat, the default", () => {
    const { container } = render(<ServesChip serves="chat" />);
    expect(container).toBeEmptyDOMElement();
    expect(render(<ServesChip />).container).toBeEmptyDOMElement();
  });

  it.each([
    ["embedding", "Embeddings"],
    ["image", "Image"],
    ["video", "Video"],
    ["speech", "Speech"],
  ])("names %s", (kind, label) => {
    render(<ServesChip serves={kind} />);
    expect(screen.getByTestId("serves-chip")).toHaveTextContent(label);
  });

  it("shows a kind this build does not know as it was stored", () => {
    render(<ServesChip serves="hologram" />);
    expect(screen.getByTestId("serves-chip")).toHaveTextContent("hologram");
  });
});

describe("RunRow", () => {
  it("offers a benchmark on a live chat run", () => {
    row(run({ serves: "chat" }));
    expect(screen.getByRole("button", { name: /Benchmark/ })).toBeInTheDocument();
    expect(screen.queryByTestId("serves-chip")).toBeNull();
  });

  it("offers a benchmark on a run recorded before the field existed", () => {
    row(run());
    expect(screen.getByRole("button", { name: /Benchmark/ })).toBeInTheDocument();
  });

  it("hides the benchmark and shows the chip on an embeddings run", () => {
    row(run({ serves: "embedding" }));
    expect(screen.queryByRole("button", { name: /Benchmark/ })).toBeNull();
    expect(screen.getByTestId("serves-chip")).toHaveTextContent("Embeddings");
    // Logs and Stop are still there, as halves rather than thirds.
    expect(screen.getByRole("button", { name: /Logs/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Stop/ })).toBeInTheDocument();
  });
});

describe("RecipeCard", () => {
  it("chips an embeddings recipe and not a chat one", () => {
    const { rerender } = render(
      <RecipeCard r={recipe({ serves: "embedding" })} isRunning={false} clusterBlocked={false} onSelect={vi.fn()} />,
    );
    expect(screen.getByTestId("serves-chip")).toHaveTextContent("Embeddings");
    rerender(
      <RecipeCard r={recipe({ serves: "chat" })} isRunning={false} clusterBlocked={false} onSelect={vi.fn()} />,
    );
    expect(screen.queryByTestId("serves-chip")).toBeNull();
  });
});

describe("BenchmarkLauncher", () => {
  it("says why and will not start against a run that does not serve chat", () => {
    render(<BenchmarkLauncher run={run({ serves: "embedding" })} onClose={vi.fn()} onStarted={vi.fn()} />);
    expect(screen.getByText(/does not serve chat/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Run$/ })).toBeDisabled();
  });
});
