/** The Inference list: a deployment that spans machines says so on its row
 * and repeats what is unproven when it is opened; a solo one says nothing. */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import InferencePage from "@/pages/InferencePage";
import { MULTI_NODE_BADGE_TITLE, MULTI_NODE_UNPROVEN } from "@/lib/experimental";
import type { Deployment } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchDeployments: vi.fn(),
  stopDeployment: vi.fn(),
  connectLogStream: vi.fn(() => () => {}),
  runBenchmark: vi.fn(),
}));

import { fetchDeployments } from "@/lib/api";

function deployment(over: Partial<Deployment> = {}): Deployment {
  return {
    id: "solo",
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
    node_count: 1,
    ...over,
  };
}

const SOLO = deployment();
const GANG = deployment({
  id: "gang",
  name: "gang job",
  node_count: 2,
  nodes: ["10.0.0.10", "10.0.0.11"],
});
/** A record written before the field existed. */
const LEGACY = deployment({ id: "legacy", name: "legacy job", node_count: undefined });

async function expand(name: string) {
  const user = userEvent.setup();
  await user.click(await screen.findByText(name));
}

describe("InferencePage multi-node marking", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchDeployments).mockResolvedValue([SOLO, GANG, LEGACY]);
  });

  it("says how many machines a multi-node deployment holds, and that it is unverified", async () => {
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-gang");
    expect(row).toHaveTextContent("2 nodes");
    expect(within(row).getByTitle(MULTI_NODE_BADGE_TITLE)).toBeInTheDocument();
  });

  it("says nothing multi-node about a deployment on one machine", async () => {
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-solo");
    expect(row).not.toHaveTextContent(/nodes/);
    expect(within(row).queryByTitle(MULTI_NODE_BADGE_TITLE)).toBeNull();
  });

  it("leaves a record with no node_count alone rather than failing on it", async () => {
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-legacy");
    expect(row).toHaveTextContent("legacy job");
    expect(within(row).queryByTitle(MULTI_NODE_BADGE_TITLE)).toBeNull();
  });

  it("carries the unproven list into the expanded view of a multi-node deployment", async () => {
    render(<InferencePage />);
    await expand("gang job");

    const note = within(screen.getByTestId("deployment-gang")).getByRole("note");
    expect(note).toHaveTextContent(/implemented but unverified/i);
    for (const item of MULTI_NODE_UNPROVEN) {
      expect(note).toHaveTextContent(item);
    }
  });

  it("does not warn about multi-node when a solo deployment is opened", async () => {
    render(<InferencePage />);
    await expand("solo job");

    const panel = screen.getByTestId("deployment-solo");
    expect(within(panel).queryByRole("note")).toBeNull();
    // Opened, not merely unchanged: the log pane is there.
    expect(within(panel).getByText("No logs yet...")).toBeInTheDocument();
  });

  it("opens an older record with no node_count without warning about machines it has none of", async () => {
    render(<InferencePage />);
    await expand("legacy job");

    const panel = screen.getByTestId("deployment-legacy");
    expect(within(panel).queryByRole("note")).toBeNull();
    expect(within(panel).getByText("No logs yet...")).toBeInTheDocument();
  });
});
