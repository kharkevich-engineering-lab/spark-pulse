/** A node's own page: what it is, how full it is, and what is wrong with it.
 *
 * It replaced a panel beside the node list that scrolled on its own inside a
 * scrolling page, then five tabs. What is pinned: it is one page, every
 * reading comes from the node's own agent, the doctor is asked only when
 * somebody runs it, a node that could not be asked says why, and forgetting
 * the node leaves its page for the list.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import NodeDetailPage from "@/pages/NodeDetailPage";
import type { ClusterNode, NodeStats } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchNodes: vi.fn(),
  fetchNodeDiagnostics: vi.fn(),
  fetchFabric: vi.fn(),
  fetchMemory: vi.fn(),
  connectMetricsStream: vi.fn(),
  killGpuProcess: vi.fn(),
  fetchNodeDoctor: vi.fn(),
  treatNode: vi.fn(),
  removeNode: vi.fn(),
  updateNode: vi.fn(),
  updateNodeAgent: vi.fn(),
  installNodeAgent: vi.fn(),
  fetchNodeHostKey: vi.fn(),
  addNode: vi.fn(),
  discoverNodes: vi.fn(),
  fetchDeployments: vi.fn(() => Promise.resolve([])),
  connectEventStream: vi.fn(() => () => {}),
}));

import {
  connectMetricsStream,
  fetchFabric,
  fetchMemory,
  fetchNodeDiagnostics,
  fetchNodeDoctor,
  fetchNodes,
  killGpuProcess,
  removeNode,
  updateNodeAgent,
} from "@/lib/api";

function node(over: Partial<ClusterNode> = {}): ClusterNode {
  return {
    id: "peer",
    name: "spark-02",
    address: "10.0.0.11",
    is_control_plane: false,
    ssh_user: "spark",
    ssh_key_path: "",
    ethernet_interface: "enp1s0",
    infiniband_interfaces: ["ib0", "ib1"],
    state: "healthy",
    last_seen: null,
    machine_id: "",
    ...over,
  };
}

const CONTROL = node({ id: "control", name: "spark-01", is_control_plane: true, address: "10.0.0.10", infiniband_interfaces: [] });
const PEER = node({
  agent: {
    enrolled: true,
    connected: true,
    version: "1.2.3",
    current: false,
    runtime: { name: "podman", version: "4.9.3", rootless: true, cdi_devices: [] },
  },
});

function stats(over: Partial<NodeStats> = {}): NodeStats {
  return {
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
    ...over,
  } as NodeStats;
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/cluster/:id" element={<NodeDetailPage />} />
        <Route path="/cluster" element={<p>the node list</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("NodeDetailPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchNodes).mockResolvedValue([CONTROL, PEER]);
    vi.mocked(fetchNodeDiagnostics).mockResolvedValue({ findings: [] });
    vi.mocked(fetchMemory).mockResolvedValue({ nodes: [stats()] } as never);
    vi.mocked(connectMetricsStream).mockReturnValue(() => {});
    vi.mocked(fetchFabric).mockResolvedValue({
      transport: true,
      nodes: [
        {
          node_id: "peer", name: "spark-02", is_control_plane: false, reported: true, mode: "direct",
          ports: [{ hca: "rocep1s0f1", netdev: "enp1s0f1np1", is_up: true, cidr: "192.168.177.12/24", mtu: 9000 }],
          ib_hca: "rocep1s0f1", errors: [], warnings: [],
        },
      ],
      plan: { mode: "direct", nodes: [], problems: [], proposed: [] },
    });
    vi.mocked(fetchNodeDoctor).mockResolvedValue({
      node_id: "peer", host: "10.0.0.11", healthy: false, channels: ["agent"],
      findings: [
        { check: "docker-socket", status: "warn", detail: "spark is not in the docker group", channel: "agent", verdict: "fixable-here", remedy: "usermod -aG docker spark" },
      ],
      repairs: [],
    });
  });

  it("names the node, where it is, and the way back", async () => {
    renderAt("/cluster/peer");

    const page = await screen.findByTestId("node-detail-peer");
    expect(within(page).getByRole("heading", { level: 1 })).toHaveTextContent("spark-02");
    expect(page).toHaveTextContent("10.0.0.11 · Peer · agent 1.2.3");
    expect(screen.getByRole("link", { name: "Nodes" })).toHaveAttribute("href", "/cluster");
    // Derived, not guessed: the management link and both fabric links.
    expect(page).toHaveTextContent("enp1s0, ib0, ib1");
    expect(screen.getByTestId("node-runtime-peer")).toHaveTextContent("podman 4.9.3 · rootless");
    // The stale agent's action is an update, as a labelled button here.
    expect(screen.getByRole("button", { name: "Update" })).toBeInTheDocument();
  });

  it("shows the fabric at once, and asks the doctor only when checks are run", async () => {
    const user = userEvent.setup();
    renderAt("/cluster/peer");
    expect(await screen.findByText("192.168.177.12/24")).toBeInTheDocument();
    expect(screen.getByText("enp1s0f1np1")).toBeInTheDocument();
    expect(screen.queryByRole("tab")).toBeNull();
    expect(fetchNodeDoctor).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Run checks" }));
    expect(await screen.findByTestId("doctor-findings")).toHaveTextContent("docker-socket");
    expect(fetchNodeDoctor).toHaveBeenCalledWith("peer");

    await user.click(screen.getByRole("button", { name: "Run again" }));
    await waitFor(() => expect(fetchNodeDoctor).toHaveBeenCalledTimes(2));
  });

  it("says why when the fabric could not be read, and keeps the page when diagnostics fail", async () => {
    vi.mocked(fetchFabric).mockRejectedValue(new Error("fabric gone"));
    vi.mocked(fetchNodeDiagnostics).mockRejectedValue(new Error("no"));
    renderAt("/cluster/peer");
    expect(await screen.findByText("fabric gone")).toBeInTheDocument();
    expect(screen.getByText("No findings.")).toBeInTheDocument();
  });

  it("shows the memory, the GPU, the disks and what could not be read on one page", async () => {
    renderAt("/cluster/peer");
    expect(await screen.findByText("64%")).toBeInTheDocument();
    expect(screen.getByText("82°C")).toHaveClass("text-bad");
    expect(screen.getByText("90 / 140 W")).toBeInTheDocument();
    expect(screen.getByText(/disk/, { selector: "li" })).toBeInTheDocument();
    expect(screen.getByTestId("disk-peer-/")).toHaveTextContent("1024.0 GB free · 50% used");
    expect(screen.getByText("No findings.")).toBeInTheDocument();
  });

  it("lists the GPU processes, and kills one only after confirming", async () => {
    const user = userEvent.setup();
    vi.mocked(killGpuProcess).mockResolvedValue({ killed: false, error: "not ours" } as never);
    renderAt("/cluster/peer");

    const row = await screen.findByTestId("gpu-process-peer-42");
    expect(row).toHaveTextContent("python3");
    await user.click(within(row).getByRole("button", { name: /^Kill / }));
    const dialog = screen.getByRole("dialog");
    expect(killGpuProcess).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Kill process" }));
    await waitFor(() => expect(killGpuProcess).toHaveBeenCalledWith(42, "10.0.0.11"));
    expect(await screen.findByText("not ours")).toBeInTheDocument();
  });

  it("says why when the node could not be asked, and leaves out what it could not give", async () => {
    vi.mocked(fetchMemory).mockResolvedValue({
      nodes: [stats({ reachable: false, error: "agent unreachable", gpu: [], disk: [], processes: [], unavailable: [] })],
    } as never);
    renderAt("/cluster/peer");
    expect(await screen.findByTestId("node-live-unreachable-peer")).toHaveTextContent("agent unreachable");
    expect(screen.queryByText("Nothing is holding the GPU.")).toBeNull();
    expect(screen.queryByText("Host memory and disk")).toBeNull();
  });

  it("says nothing was reported for a node the stats do not name", async () => {
    vi.mocked(fetchMemory).mockResolvedValue({ nodes: [] } as never);
    renderAt("/cluster/control");
    await screen.findByTestId("node-detail-control");
    expect(await screen.findByText("Could not be asked")).toBeInTheDocument();
  });

  it("puts a finding about this node on its page, with the remedy behind a tip", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchNodeDiagnostics).mockResolvedValue({
      findings: [
        { code: "agent_outdated", severity: "warning", summary: "The agent on spark-02 is behind.", remedy: "Update it.", node_ids: ["peer"] },
        { code: "dup", severity: "warning", summary: "Two nodes share an id.", remedy: "Re-image one.", node_ids: ["peer", "control"] },
      ],
    });
    renderAt("/cluster/peer");
    const finding = await screen.findByTestId("node-finding-peer");
    expect(finding).toHaveTextContent("The agent on spark-02 is behind.");
    // A finding about the fleet stays on the list.
    expect(finding).not.toHaveTextContent("Two nodes share an id.");
    await user.click(within(finding).getByRole("button", { name: "How to fix" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("Update it.");
  });

  it("goes back to the list once the node is forgotten", async () => {
    const user = userEvent.setup();
    vi.mocked(removeNode).mockResolvedValue(undefined as never);
    renderAt("/cluster/peer");
    await screen.findByTestId("node-detail-peer");
    await user.click(screen.getByRole("button", { name: "Forget" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Forget" }));
    expect(await screen.findByText("the node list")).toBeInTheDocument();
    expect(removeNode).toHaveBeenCalledWith("peer");
  });

  it("updates the agent from the header", async () => {
    const user = userEvent.setup();
    vi.mocked(updateNodeAgent).mockResolvedValue({ updated: false, needs_reinstall: false, detail: "agent refused" } as never);
    renderAt("/cluster/peer");
    await screen.findByTestId("node-detail-peer");
    await user.click(screen.getByRole("button", { name: "Update" }));
    expect(await screen.findByText("spark-02: agent refused")).toBeInTheDocument();
  });

  it("says so, with the way back, for a node it does not know", async () => {
    renderAt("/cluster/nope");
    expect(await screen.findByText("No node with id nope.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to nodes" })).toHaveAttribute("href", "/cluster");
  });

  it("offers no forget and no install on the control plane", async () => {
    vi.mocked(fetchMemory).mockResolvedValue({ nodes: [] } as never);
    renderAt("/cluster/control");
    await screen.findByTestId("node-detail-control");
    expect(screen.queryByRole("button", { name: "Forget" })).toBeNull();
    expect(screen.getByRole("button", { name: "Edit node" })).toBeInTheDocument();
    expect(screen.getByTestId("node-detail-control")).toHaveTextContent("Control plane");
  });
});
