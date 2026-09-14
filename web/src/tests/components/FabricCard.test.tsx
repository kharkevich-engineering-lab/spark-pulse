import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import FabricCard from "@/components/FabricCard";
import type { FabricApplyReport, FabricNode, FabricNodePlan, FabricResponse } from "@/lib/types";

vi.mock("@/lib/api", () => ({ fetchFabric: vi.fn(), applyFabric: vi.fn() }));

import { applyFabric, fetchFabric } from "@/lib/api";

function node(over: Partial<FabricNode> = {}): FabricNode {
  return {
    node_id: "a",
    name: "gx10-ced2",
    is_control_plane: true,
    reported: true,
    mode: "direct",
    ports: [
      { hca: "rocep1s0f0", netdev: "enp1s0f0np0", is_up: false, cidr: "", mtu: 1500 },
      { hca: "rocep1s0f1", netdev: "enp1s0f1np1", is_up: true, cidr: "", mtu: 1500 },
      { hca: "roceP2p1s0f1", netdev: "enP2p1s0f1np1", is_up: true, cidr: "", mtu: 1500 },
    ],
    ib_hca: "rocep1s0f1,roceP2p1s0f1",
    errors: ["enp1s0f1np1 is up but has no IP address assigned."],
    warnings: [],
    ...over,
  };
}

function plan(over: Partial<FabricNodePlan> = {}): FabricNodePlan {
  return {
    node_id: "a",
    name: "gx10-ced2",
    status: "proposed",
    is_control_plane: true,
    assignments: [
      { netdev: "enp1s0f1np1", hca: "rocep1s0f1", cidr: "192.168.177.11/24", mtu: 9000, current_cidr: "", current_mtu: 1500, changes: true, peers: [{ name: "gx10-b90f", address: "192.168.177.12" }] },
      { netdev: "enP2p1s0f1np1", hca: "roceP2p1s0f1", cidr: "192.168.178.11/24", mtu: 9000, current_cidr: "", current_mtu: 1500, changes: true, peers: [{ name: "gx10-b90f", address: "192.168.178.12" }] },
    ],
    reasons: ["no address on a cabled port"],
    netplan: "network:\n  version: 2\n  ethernets:\n    enp1s0f1np1:\n      addresses: [192.168.177.11/24]\n",
    netplan_path: "/etc/netplan/40-cx7.yaml",
    ...over,
  };
}

const PAIR: FabricResponse = {
  transport: true,
  nodes: [node(), node({ node_id: "b", name: "gx10-b90f", is_control_plane: false })],
  plan: {
    mode: "direct",
    nodes: [plan(), plan({ node_id: "b", name: "gx10-b90f", is_control_plane: false })],
    problems: [],
    proposed: ["a", "b"],
  },
};

function report(over: Partial<FabricApplyReport> = {}): FabricApplyReport {
  return {
    node_id: "a",
    name: "gx10-ced2",
    applied: true,
    verified: true,
    steps: ["wrote /etc/netplan/40-cx7.yaml", "netplan apply ran"],
    errors: [],
    readback: {},
    pings: [
      { netdev: "enp1s0f1np1", peer: "gx10-b90f", address: "192.168.177.12", reachable: true },
      { netdev: "enP2p1s0f1np1", peer: "gx10-b90f", address: "192.168.178.12", reachable: true },
    ],
    privileged_calls: [],
    ...over,
  };
}

describe("FabricCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchFabric).mockResolvedValue(PAIR);
  });

  it("shows each node's cabled ports, addresses, MTU and the plan for it", async () => {
    render(<FabricCard />);
    const row = await screen.findByRole("row", { name: /gx10-ced2/ });
    expect(row).toHaveTextContent("enp1s0f1np1");
    expect(row).toHaveTextContent("enP2p1s0f1np1");
    expect(row).not.toHaveTextContent("enp1s0f0np0");
    expect(row).toHaveTextContent("none");
    expect(row).toHaveTextContent("→ 192.168.177.11/24");
    expect(row).toHaveTextContent("1500");
    expect(within(row).getByText("Proposed")).toHaveAttribute("title", "no address on a cabled port");
    expect(screen.getByText(/One cable per node/)).toBeInTheDocument();
  });

  it("says so when the transport is down and offers nothing", async () => {
    vi.mocked(fetchFabric).mockResolvedValue({ transport: false, nodes: [], plan: { mode: "", nodes: [], problems: [], proposed: [] } });
    render(<FabricCard />);
    expect(await screen.findByText(/transport is not running/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Configure fabric" })).toBeDisabled();
  });

  it("says which nodes no agent has reported, and lists the plan's problems", async () => {
    vi.mocked(fetchFabric).mockResolvedValue({
      transport: true,
      nodes: [node(), node({ node_id: "b", name: "gx10-b90f", reported: false, ports: [] })],
      plan: {
        mode: "",
        nodes: [plan({ status: "refused", reasons: [] }), plan({ node_id: "b", name: "gx10-b90f", status: "unknown", assignments: [], netplan: "", reasons: ["no agent has reported this node's ports"] })],
        problems: ["the nodes are not cabled alike, so no one shape fits them all"],
        proposed: [],
      },
    });
    render(<FabricCard />);
    const row = await screen.findByRole("row", { name: /gx10-b90f/ });
    expect(row).toHaveTextContent("No agent has reported this node");
    expect(within(row).getByText("Unknown")).toBeInTheDocument();
    expect(screen.getByTestId("fabric-problems")).toHaveTextContent("not cabled alike");
    expect(screen.getByText("No shape fits these nodes.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Configure fabric" })).toBeDisabled();
  });

  it("shows a node with no cable as such", async () => {
    vi.mocked(fetchFabric).mockResolvedValue({
      ...PAIR,
      nodes: [node({ ports: PAIR.nodes[0].ports.map((p) => ({ ...p, is_up: false })) })],
    });
    render(<FabricCard />);
    expect(await screen.findByText("No cable")).toBeInTheDocument();
  });

  it("surfaces a failure to read the fabric", async () => {
    vi.mocked(fetchFabric).mockRejectedValue(new Error("API 500: boom"));
    render(<FabricCard />);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });

  it("refreshes on request", async () => {
    const user = userEvent.setup();
    render(<FabricCard />);
    await screen.findByRole("row", { name: /gx10-ced2/ });
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(fetchFabric).toHaveBeenCalledTimes(2));
  });

  it("shows each proposed file, applies with the sudo password, and reports the pings", async () => {
    const user = userEvent.setup();
    vi.mocked(applyFabric).mockResolvedValue({
      mode: "direct",
      reports: [report(), report({ node_id: "b", name: "gx10-b90f" })],
    });
    render(<FabricCard />);
    await screen.findByRole("row", { name: /gx10-ced2/ });
    await user.click(screen.getByRole("button", { name: "Configure fabric" }));
    const dialog = screen.getByRole("dialog", { name: "Configure fabric" });
    expect(within(dialog).getByText("Planned addresses for gx10-ced2")).toBeInTheDocument();
    expect(within(dialog).getByText("Planned addresses for gx10-b90f")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("192.168.177.11/24");
    // No node is already configured, so there is nothing to override.
    expect(within(dialog).queryByLabelText(/already configured/)).toBeNull();

    await user.type(within(dialog).getByLabelText("sudo password"), "s3cret");
    await user.click(within(dialog).getByRole("button", { name: "Configure 2 node(s)" }));

    await waitFor(() => expect(applyFabric).toHaveBeenCalledWith({ override: false, sudo_password: "s3cret" }));
    const outcome = await within(dialog).findByTestId("fabric-apply-report");
    expect(outcome).toHaveTextContent("gx10-ced2: applied and verified");
    expect(outcome).toHaveTextContent("gx10-b90f: applied and verified");
    expect(outcome).toHaveTextContent("enp1s0f1np1 → gx10-b90f (192.168.177.12): answers");
    // The list is re-read so the rows say what the ports now carry.
    await waitFor(() => expect(fetchFabric).toHaveBeenCalledTimes(2));
    await user.click(within(dialog).getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("names the link a peer did not answer on, and a node that was not applied", async () => {
    const user = userEvent.setup();
    vi.mocked(applyFabric).mockResolvedValue({
      mode: "direct",
      reports: [
        report({
          verified: false,
          errors: ["gx10-b90f (192.168.178.12) does not answer over enP2p1s0f1np1; either that node is not applied yet, or the cable does not go where the plan assumed"],
          pings: [
            { netdev: "enp1s0f1np1", peer: "gx10-b90f", address: "192.168.177.12", reachable: true },
            { netdev: "enP2p1s0f1np1", peer: "gx10-b90f", address: "192.168.178.12", reachable: false },
          ],
        }),
        report({ node_id: "b", name: "gx10-b90f", applied: false, verified: false, steps: [], pings: [], errors: ["needs root and none was available"] }),
      ],
    });
    render(<FabricCard />);
    await screen.findByRole("row", { name: /gx10-ced2/ });
    await user.click(screen.getByRole("button", { name: "Configure fabric" }));
    const dialog = screen.getByRole("dialog", { name: "Configure fabric" });
    await user.click(within(dialog).getByRole("button", { name: "Configure 2 node(s)" }));
    const outcome = await within(dialog).findByTestId("fabric-apply-report");
    expect(outcome).toHaveTextContent("gx10-ced2: applied, but not verified");
    expect(outcome).toHaveTextContent("cable does not go where the plan assumed");
    expect(outcome).toHaveTextContent("enP2p1s0f1np1 → gx10-b90f (192.168.178.12): no answer");
    expect(outcome).toHaveTextContent("gx10-b90f: not applied");
    expect(outcome).toHaveTextContent("needs root");
    expect(applyFabric).toHaveBeenCalledWith({ override: false });
  });

  it("offers to re-address configured nodes, and counts them only when asked", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchFabric).mockResolvedValue({
      ...PAIR,
      plan: {
        ...PAIR.plan,
        nodes: [plan({ status: "configured", reasons: ["left as it is"] }), PAIR.plan.nodes[1]],
        proposed: ["b"],
      },
    });
    vi.mocked(applyFabric).mockResolvedValue({ mode: "direct", reports: [] });
    render(<FabricCard />);
    const row = await screen.findByRole("row", { name: /gx10-ced2/ });
    expect(within(row).getByText("Configured")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Configure fabric" }));
    const dialog = screen.getByRole("dialog", { name: "Configure fabric" });
    expect(within(dialog).getByRole("button", { name: "Configure 1 node(s)" })).toBeEnabled();
    expect(within(dialog).queryByText("Planned addresses for gx10-ced2")).toBeNull();
    await user.click(within(dialog).getByLabelText(/already configured/));
    expect(within(dialog).getByText("Planned addresses for gx10-ced2")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Configure 2 node(s)" }));
    await waitFor(() => expect(applyFabric).toHaveBeenCalledWith({ override: true }));
  });

  it("keeps the dialog and says why when the apply itself fails", async () => {
    const user = userEvent.setup();
    vi.mocked(applyFabric).mockRejectedValue(new Error("API 503: the agent transport is not running"));
    render(<FabricCard />);
    await screen.findByRole("row", { name: /gx10-ced2/ });
    await user.click(screen.getByRole("button", { name: "Configure fabric" }));
    const dialog = screen.getByRole("dialog", { name: "Configure fabric" });
    await user.click(within(dialog).getByRole("button", { name: "Configure 2 node(s)" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("transport is not running");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("names the dual-cable pair, shows the plan's advice, and whether each node is pinned", async () => {
    vi.mocked(fetchFabric).mockResolvedValue({
      transport: true,
      nodes: [
        node({ mode: "dual", pinned: { ethernet_interface: "enp1s0f0np0", infiniband_interfaces: ["rocep1s0f0"], fabric_mode: "dual" } }),
        node({ node_id: "b", name: "gx10-b90f", is_control_plane: false, mode: "dual", pinned: { ethernet_interface: "", infiniband_interfaces: [], fabric_mode: "" } }),
      ],
      plan: {
        ...PAIR.plan,
        mode: "dual",
        advice: ["One cable already carries both RoCE twins of its port (200G). Plug the second in or not — either shape is planned."],
      },
    });
    render(<FabricCard />);
    expect(await screen.findByText(/Both cables between two nodes/)).toBeInTheDocument();
    expect(screen.getByTestId("fabric-advice")).toHaveTextContent("either shape is planned");
    expect(screen.getByRole("row", { name: /gx10-ced2/ })).toHaveTextContent("Pinned for deploys");
    expect(screen.getByRole("row", { name: /gx10-b90f/ })).toHaveTextContent("Not pinned for deploys");
  });

  it("flags a mesh node whose 10G port has no link", async () => {
    vi.mocked(fetchFabric).mockResolvedValue({
      transport: true,
      nodes: [node({ mode: "mesh", wired_management_up: false })],
      plan: { ...PAIR.plan, mode: "mesh", nodes: [plan()], proposed: ["a"] },
    });
    render(<FabricCard />);
    const row = await screen.findByRole("row", { name: /gx10-ced2/ });
    expect(row).toHaveTextContent("10G port down");
    expect(screen.getByText(/switchless three-node mesh/)).toBeInTheDocument();
  });
});
