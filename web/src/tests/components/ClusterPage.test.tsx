/** The Cluster page: the experimental banner says what is unproven, and a
 * deployment that spans machines is marked as one on its own row. */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import ClusterPage from "@/pages/ClusterPage";
import { MULTI_NODE_BADGE_TITLE, MULTI_NODE_UNPROVEN } from "@/lib/experimental";
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

  it("warns that multi-node is unverified and lists what is unproven", async () => {
    render(<ClusterPage />);

    const note = await screen.findByRole("note");
    expect(note).toHaveTextContent(/implemented but unverified/i);
    for (const item of MULTI_NODE_UNPROVEN) {
      expect(note).toHaveTextContent(item);
    }
    expect(within(note).getAllByRole("listitem")).toHaveLength(MULTI_NODE_UNPROVEN.length);
  });

  it("does not repeat the stale claim that more than one node is refused", async () => {
    render(<ClusterPage />);

    const note = await screen.findByRole("note");
    // The old copy said multi-node was refused outright. It is not: it is
    // implemented and unverified, which is a different thing to tell an
    // operator, and the difference is the whole point of the banner.
    expect(note).not.toHaveTextContent(/asking for more than one node/i);
    expect(note).not.toHaveTextContent(/refused by name/i);
    expect(note).not.toHaveTextContent(/until the start loop covers every rank/i);
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
