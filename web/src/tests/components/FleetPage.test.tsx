/** Nodes: one table over the machines, and the table that is not here.
 *
 * `/cluster` and `/monitoring` are the same page — "which machines do I have"
 * and "what are they doing" are two readings of the same hardware — so what is
 * worth pinning is that the live readings are on each row, that a node's name
 * opens its own page, and that the deployments table is gone rather than
 * duplicated: what is running is answered on Runs.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
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
  runDiscovery: vi.fn(),
  fetchMemory: vi.fn(),
  connectMetricsStream: vi.fn(),
  fetchDeployments: vi.fn(() => Promise.resolve([])),
  connectEventStream: vi.fn(() => () => {}),
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

  it("opens /cluster on the registry, with the fabric and discovery under it", async () => {
    renderAt("/cluster");

    expect(await screen.findByTestId("node-registry")).toBeInTheDocument();
    expect(screen.getByTestId("fabric-card")).toBeInTheDocument();
    expect(screen.getByTestId("network-discovery")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Nodes.");
    expect(screen.queryByRole("tab")).toBeNull();
  });

  /** Monitoring was a second page about the same machines; its address opens
   *  this one, the live readings on each row and the rest on the node's page. */
  it("answers /monitoring with the same page, the live readings on each row", async () => {
    vi.mocked(fetchMemory).mockResolvedValue({
      nodes: [
        {
          id: "peer",
          name: "spark-02",
          address: "10.0.0.11",
          is_control_plane: false,
          reachable: true,
          error: null,
          unavailable: ["disk"],
          gpu: [
            {
              index: 0, gpu: "0", uuid: "g", name: "GB10", memory_total: 1000, memory_used: 250, memory_free: 750,
              memory_supported: true, temperature: 82, utilization: 64, power_draw: 90.4, power_limit: 140,
            },
          ],
          cpu: { total: 121 * 1024, used: 60 * 1024, free: 61 * 1024, available: 61 * 1024, usage_percent: 50 },
          disk: [{ mount: "/", total: 2 * 1024 ** 4, used: 1024 ** 4, free: 1024 ** 4, usage_percent: 50 }],
          processes: [{ gpu_uuid: "g", pid: 42, process_name: "python3", used_memory: 2048, is_tracked: false }],
        },
        { id: "control", name: "spark-01", address: "10.0.0.10", is_control_plane: true, reachable: false, error: "agent unreachable", unavailable: [], gpu: [], cpu: { total: 0, used: 0, free: 0, available: 0, usage_percent: 0 }, disk: [], processes: [] },
      ],
    } as never);
    renderAt("/monitoring");

    const row = await screen.findByTestId("node-row-peer");
    expect(await within(row).findByText("64%")).toBeInTheDocument();
    expect(within(row).getByText("82°")).toBeInTheDocument();
    expect(within(screen.getByTestId("node-row-control")).getByText("Could not be asked")).toBeInTheDocument();
    // No panel beside the table: the name is a link to the node's page.
    expect(within(row).getByRole("link", { name: "spark-02" })).toHaveAttribute("href", "/cluster/peer");
    expect(screen.queryByRole("complementary")).toBeNull();
  });

  it("counts the machines, and points at Runs for what is on them", async () => {
    renderAt("/cluster");

    expect(await screen.findByText(/2 nodes/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Runs" })).toHaveAttribute("href", "/jobs");
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
    // The one table here is the nodes'.
    expect(screen.getAllByRole("table")).toHaveLength(1);
  });
});
