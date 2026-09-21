/** Fleet: two tabs over one set of machines, and the table that is not here.
 *
 * `/cluster` and `/monitoring` are the same page now — "which machines do I
 * have" and "what are they doing" are two readings of the same hardware — so
 * what is worth pinning is that each route opens on its own tab, that
 * switching tabs is a navigation somebody can bookmark, and that the
 * deployments table is gone rather than duplicated: what is running is
 * answered on Runs, where the controls to act on it are.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import FleetPage from "@/pages/FleetPage";
import type { ClusterNode } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  // The registry, the fabric section and the monitoring tab all fetch on
  // mount; they are stubbed so the assertions are about this page.
  fetchNodes: vi.fn(),
  fetchNodeDiagnostics: vi.fn(),
  fetchFabric: vi.fn(),
  applyFabric: vi.fn(),
  addNode: vi.fn(),
  updateNode: vi.fn(),
  updateNodeAgent: vi.fn(),
  removeNode: vi.fn(),
  discoverNodes: vi.fn(),
  fetchNodeHostKey: vi.fn(),
  installNodeAgent: vi.fn(),
  fetchNodeDoctor: vi.fn(),
  treatNode: vi.fn(),
  runDiscovery: vi.fn(),
  fetchMemory: vi.fn(),
  connectMetricsStream: vi.fn(),
  killGpuProcess: vi.fn(),
}));

import {
  connectMetricsStream,
  fetchFabric,
  fetchMemory,
  fetchNodeDiagnostics,
  fetchNodes,
} from "@/lib/api";

function node(over: Partial<ClusterNode> = {}): ClusterNode {
  return {
    id: "control",
    name: "spark-01",
    address: "10.0.0.10",
    is_control_plane: true,
    ssh_user: "",
    ssh_key_path: "",
    ethernet_interface: "enp1s0",
    infiniband_interfaces: [],
    state: "healthy",
    last_seen: null,
    machine_id: "",
    ...over,
  };
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <FleetPage />
    </MemoryRouter>,
  );
}

describe("FleetPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchNodes).mockResolvedValue([node(), node({ id: "peer", name: "spark-02", is_control_plane: false })]);
    vi.mocked(fetchNodeDiagnostics).mockResolvedValue({ findings: [] });
    vi.mocked(fetchFabric).mockResolvedValue({
      transport: false,
      nodes: [],
      plan: { mode: "", nodes: [], problems: [], proposed: [] },
    });
    vi.mocked(fetchMemory).mockResolvedValue({ nodes: [] });
    vi.mocked(connectMetricsStream).mockReturnValue(() => {});
  });

  /** The page carried a line saying multi-node was experimental. Two nodes
   *  have run and been measured, so there is nothing to disclaim. */
  it("carries no experimental note above the registry", async () => {
    renderAt("/cluster");

    expect(await screen.findByTestId("node-registry")).toBeInTheDocument();
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });

  it("opens /cluster on the nodes tab, with the fabric and discovery under it", async () => {
    renderAt("/cluster");

    expect(await screen.findByTestId("node-registry")).toBeInTheDocument();
    expect(screen.getByTestId("fabric-card")).toBeInTheDocument();
    expect(screen.getByTestId("network-discovery")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Nodes/ })).toHaveAttribute("aria-selected", "true");
  });

  it("opens /monitoring on the monitoring tab", async () => {
    renderAt("/monitoring");

    expect(await screen.findByRole("tab", { name: "Monitoring" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.queryByTestId("node-registry")).toBeNull();
    expect(fetchMemory).toHaveBeenCalled();
  });

  it("switches to monitoring, and back, from the tab bar", async () => {
    const user = userEvent.setup();
    renderAt("/cluster");
    await screen.findByTestId("node-registry");

    await user.click(screen.getByRole("tab", { name: "Monitoring" }));
    expect(screen.queryByTestId("node-registry")).toBeNull();

    await user.click(screen.getByRole("tab", { name: /Nodes/ }));
    expect(await screen.findByTestId("node-registry")).toBeInTheDocument();
  });

  it("counts the machines, and points at Runs for what is on them", async () => {
    renderAt("/cluster");

    expect(await screen.findByText(/2 nodes/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Runs" })).toHaveAttribute("href", "/jobs");
  });

  /** The registry is only mounted on the Nodes tab, so a header that took its
   *  count from it said "0 nodes" the moment you opened Monitoring — a lie
   *  about the one thing the page is named for. */
  it("still counts the machines on the monitoring tab", async () => {
    renderAt("/monitoring");

    expect(await screen.findByText(/2 nodes/)).toBeInTheDocument();
    expect(screen.queryByTestId("node-registry")).toBeNull();
  });

  it("opens the add dialog from the page's own action", async () => {
    const user = userEvent.setup();
    renderAt("/cluster");
    await screen.findByTestId("node-registry");

    // One Add button on the page: the registry does not render a second one
    // when the page above it owns the action.
    const add = screen.getByRole("button", { name: "Add node" });
    await user.click(add);
    expect(screen.getByRole("dialog", { name: "Add node" })).toBeInTheDocument();
  });

  it("no longer carries a deployments table", async () => {
    renderAt("/cluster");
    await screen.findByTestId("node-registry");

    expect(screen.queryByTestId("cluster-deployments")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
  });
});
