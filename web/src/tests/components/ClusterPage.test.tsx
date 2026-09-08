/** The Cluster page: the experimental banner says what is unproven, and a
 * deployment that spans machines is marked as one on its own row. */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import ClusterPage from "@/pages/ClusterPage";
import { MULTI_NODE_BADGE_TITLE } from "@/lib/experimental";
import type { Deployment } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchDeployments: vi.fn(),
  // <NodeRegistry /> and <LaunchScriptAnalyzer /> live on this page and fetch
  // on mount; they are stubbed out so the assertions are about this page.
  fetchNodes: vi.fn(),
  fetchNodeDiagnostics: vi.fn(),
  addNode: vi.fn(),
  removeNode: vi.fn(),
  discoverNodes: vi.fn(),
  resolveLaunchScript: vi.fn(),
  analyzeLaunchScript: vi.fn(),
  validateLaunchScript: vi.fn(),
}));

import { fetchDeployments, fetchNodeDiagnostics, fetchNodes } from "@/lib/api";

function deployment(over: Partial<Deployment> = {}): Deployment {
  return {
    id: "dep-1",
    recipe_id: "qwen3-8b",
    name: "solo job",
    params: {},
    nodes: null,
    status: "running",
    pid: null,
    port: 9000,
    created_at: "2026-01-01T00:00:00+00:00",
    started_at: null,
    stopped_at: null,
    error_message: null,
    ...over,
  };
}

const SOLO = deployment();
const GANG = deployment({
  id: "dep-2",
  name: "gang job",
  node_count: 2,
  nodes: ["10.0.0.10", "10.0.0.11"],
});

describe("ClusterPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchNodes).mockResolvedValue([]);
    vi.mocked(fetchNodeDiagnostics).mockResolvedValue({ findings: [] });
    vi.mocked(fetchDeployments).mockResolvedValue([SOLO, GANG]);
  });

  it("says multi-node is experimental in one line, not a wall of text", async () => {
    // The full banner — six named unproven things and why — belongs where an
    // operator is about to deploy across machines. This page is read, and on
    // it the banner was the loudest thing on the screen.
    render(<ClusterPage />);

    const note = await screen.findByRole("note");
    expect(note).toHaveTextContent("Multi-node is still experimental.");
    expect(within(note).queryAllByRole("listitem")).toHaveLength(0);
  });

  it("marks the row of a deployment that spans machines", async () => {
    render(<ClusterPage />);

    const row = await screen.findByRole("row", { name: /gang job/ });
    expect(row).toHaveTextContent("2");
    expect(within(row).getByTitle(MULTI_NODE_BADGE_TITLE)).toBeInTheDocument();
  });

  it("leaves a single-node deployment unmarked", async () => {
    render(<ClusterPage />);

    const row = await screen.findByRole("row", { name: /solo job/ });
    expect(row).toHaveTextContent("this node");
    expect(within(row).queryByTitle(MULTI_NODE_BADGE_TITLE)).toBeNull();
  });

  it("counts an older record with no node_count as one machine", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([
      deployment({ id: "old", name: "legacy job", nodes: null }),
    ]);
    render(<ClusterPage />);

    const row = await screen.findByRole("row", { name: /legacy job/ });
    expect(within(row).queryByTitle(MULTI_NODE_BADGE_TITLE)).toBeNull();
  });

  it("names the machines a multi-node deployment sits on", async () => {
    render(<ClusterPage />);

    const row = await screen.findByRole("row", { name: /gang job/ });
    expect(row).toHaveTextContent("10.0.0.10, 10.0.0.11");
  });
});
