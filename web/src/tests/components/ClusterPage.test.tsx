/** The Fleet page: the machines, and one line saying what is unproven.
 *
 * What ran on them used to be a second table here, on its own poll, disagreeing
 * with the Runs page about the same endpoint. It is gone, and what is left is
 * the pointer to the one list.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ClusterPage from "@/pages/ClusterPage";

vi.mock("@/lib/api", () => ({
  // <NodeRegistry /> and <FabricCard /> live on this page and fetch on mount;
  // they are stubbed out so the assertions are about this page.
  fetchNodes: vi.fn(),
  fetchNodeDiagnostics: vi.fn(),
  addNode: vi.fn(),
  removeNode: vi.fn(),
  discoverNodes: vi.fn(),
  fetchNodeDoctor: vi.fn(),
  treatNode: vi.fn(),
  fetchFabric: vi.fn(),
  applyFabric: vi.fn(),
  fetchDeployments: vi.fn(),
}));

import { fetchDeployments, fetchNodeDiagnostics, fetchNodes } from "@/lib/api";

function show() {
  return render(
    <MemoryRouter>
      <ClusterPage />
    </MemoryRouter>,
  );
}

describe("ClusterPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchNodes).mockResolvedValue([]);
    vi.mocked(fetchNodeDiagnostics).mockResolvedValue({ findings: [] });
  });

  it("says multi-node is experimental in one line, not a wall of text", async () => {
    // The full banner — six named unproven things and why — belongs where an
    // operator is about to deploy across machines. This page is read, and on
    // it the banner was the loudest thing on the screen.
    show();

    const note = await screen.findByRole("note");
    expect(note).toHaveTextContent("Multi-node is still experimental.");
    expect(within(note).queryAllByRole("listitem")).toHaveLength(0);
  });

  /** Two lists of the same endpoint is two lists that can disagree, and this
   *  one polled on a different interval from the page operators actually
   *  read. It points at that page instead. */
  it("sends the reader to Runs rather than listing the deployments again", async () => {
    show();

    const link = await screen.findByRole("link", { name: "See the runs." });
    expect(link).toHaveAttribute("href", "/jobs");
    expect(screen.queryByTestId("cluster-deployments")).toBeNull();
    expect(fetchDeployments).not.toHaveBeenCalled();
  });
});
