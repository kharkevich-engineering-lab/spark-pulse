import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import NodeRegistry from "@/components/NodeRegistry";
import type { ClusterNode, DiscoverNodesResult, NodeFinding } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchNodes: vi.fn(),
  addNode: vi.fn(),
  removeNode: vi.fn(),
  discoverNodes: vi.fn(),
  fetchNodeDiagnostics: vi.fn(),
}));

import {
  addNode,
  discoverNodes,
  fetchNodeDiagnostics,
  fetchNodes,
  removeNode,
} from "@/lib/api";

function node(overrides: Partial<ClusterNode> = {}): ClusterNode {
  return {
    id: "n1",
    name: "spark-01",
    address: "10.0.0.10",
    is_control_plane: false,
    ssh_user: "",
    ssh_key_path: "",
    ethernet_interface: "enp1s0",
    infiniband_interfaces: [],
    state: "unknown",
    last_seen: null,
    machine_id: "",
    ...overrides,
  };
}

const CONTROL = node({
  id: "control",
  name: "spark-01",
  is_control_plane: true,
  state: "healthy",
});
const PEER = node({
  id: "peer",
  name: "spark-02",
  address: "10.0.0.11",
  infiniband_interfaces: ["ib0", "ib1"],
  state: "unknown",
});

function mockApi({
  nodes = [CONTROL, PEER],
  findings = [] as NodeFinding[],
} = {}) {
  vi.mocked(fetchNodes).mockResolvedValue(nodes);
  vi.mocked(fetchNodeDiagnostics).mockResolvedValue({ findings });
}

describe("NodeRegistry", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi();
  });

  it("renders one row per node with its address and interfaces", async () => {
    render(<NodeRegistry />);

    const peerRow = await screen.findByRole("row", { name: /spark-02/ });
    expect(peerRow).toHaveTextContent("10.0.0.11");
    // Derived, not guessed: the management link and both fabric links.
    expect(peerRow).toHaveTextContent("enp1s0, ib0, ib1");
  });

  it("marks the control plane and calls a peer a peer", async () => {
    render(<NodeRegistry />);

    expect(await screen.findByRole("row", { name: /spark-01/ })).toHaveTextContent(
      "Control plane",
    );
    expect(screen.getByRole("row", { name: /spark-02/ })).toHaveTextContent("Peer");
  });

  it("shows the three states as three distinct labels", async () => {
    mockApi({
      nodes: [
        node({ id: "a", name: "alpha", address: "10.0.0.1", state: "healthy" }),
        node({ id: "b", name: "bravo", address: "10.0.0.2", state: "unknown" }),
        node({ id: "c", name: "charlie", address: "10.0.0.3", state: "dead" }),
      ],
    });
    render(<NodeRegistry />);

    expect(await screen.findByRole("row", { name: /alpha/ })).toHaveTextContent("Healthy");
    expect(screen.getByRole("row", { name: /bravo/ })).toHaveTextContent("Unknown");
    expect(screen.getByRole("row", { name: /charlie/ })).toHaveTextContent("Dead");
  });

  it("says unknown means unverified rather than failed", async () => {
    render(<NodeRegistry />);
    const row = await screen.findByRole("row", { name: /spark-02/ });
    expect(within(row).getByTitle(/status unverified/i)).toBeInTheDocument();
  });

  it("offers no forget button for the control plane", async () => {
    render(<NodeRegistry />);
    await screen.findByRole("row", { name: /spark-01/ });

    expect(screen.getByRole("button", { name: "Forget spark-02" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Forget spark-01" })).toBeNull();
  });

  it("adds a node by address without any discovery at all", async () => {
    const user = userEvent.setup();
    vi.mocked(addNode).mockResolvedValue(node({ id: "new", address: "10.0.0.12" }));
    render(<NodeRegistry />);
    await screen.findByRole("row", { name: /spark-02/ });

    await user.click(screen.getByRole("button", { name: "Add node" }));
    const dialog = screen.getByRole("dialog", { name: "Add node" });
    await user.type(within(dialog).getByLabelText("Address *"), "10.0.0.12");
    await user.type(within(dialog).getByLabelText("SSH user"), "spark");
    await user.click(within(dialog).getByRole("button", { name: "Add node" }));

    await waitFor(() =>
      expect(addNode).toHaveBeenCalledWith({
        name: undefined,
        address: "10.0.0.12",
        ssh_user: "spark",
        ssh_key_path: undefined,
      }),
    );
    // The list is reloaded, so what is shown is what the server holds.
    await waitFor(() => expect(fetchNodes).toHaveBeenCalledTimes(2));
  });

  it("cannot submit without an address", async () => {
    const user = userEvent.setup();
    render(<NodeRegistry />);
    await screen.findByRole("row", { name: /spark-02/ });

    await user.click(screen.getByRole("button", { name: "Add node" }));
    const dialog = screen.getByRole("dialog", { name: "Add node" });
    expect(within(dialog).getByRole("button", { name: "Add node" })).toBeDisabled();
  });

  it("reports a rejected add instead of closing silently", async () => {
    const user = userEvent.setup();
    vi.mocked(addNode).mockRejectedValue(
      new Error("API 400: a node with address 10.0.0.11 is already registered"),
    );
    render(<NodeRegistry />);
    await screen.findByRole("row", { name: /spark-02/ });

    await user.click(screen.getByRole("button", { name: "Add node" }));
    const dialog = screen.getByRole("dialog", { name: "Add node" });
    await user.type(within(dialog).getByLabelText("Address *"), "10.0.0.11");
    await user.click(within(dialog).getByRole("button", { name: "Add node" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "already registered",
    );
    expect(dialog).toBeInTheDocument();
  });

  it("fills the address from a discovered peer", async () => {
    const user = userEvent.setup();
    const result: DiscoverNodesResult = {
      mdns_available: true,
      peers: [
        {
          address: "10.0.0.20",
          port: 8100,
          service: "_spark-pulse._tcp.local.",
          hostname: "spark-09.local",
          instance: "spark-09",
          node_id: "abc",
          version: "1.2.3",
          is_spark_pulse: true,
          registered: false,
        },
      ],
    };
    vi.mocked(discoverNodes).mockResolvedValue(result);
    render(<NodeRegistry />);
    await screen.findByRole("row", { name: /spark-02/ });

    await user.click(screen.getByRole("button", { name: "Add node" }));
    const dialog = screen.getByRole("dialog", { name: "Add node" });
    await user.click(within(dialog).getByRole("button", { name: "Scan" }));

    await user.click(await within(dialog).findByRole("button", { name: /10\.0\.0\.20/ }));
    expect(within(dialog).getByLabelText("Address *")).toHaveValue("10.0.0.20");
    expect(within(dialog).getByLabelText("Name")).toHaveValue("spark-09");
  });

  it("says so when mDNS is unavailable and keeps manual entry working", async () => {
    const user = userEvent.setup();
    vi.mocked(discoverNodes).mockResolvedValue({ mdns_available: false, peers: [] });
    render(<NodeRegistry />);
    await screen.findByRole("row", { name: /spark-02/ });

    await user.click(screen.getByRole("button", { name: "Add node" }));
    const dialog = screen.getByRole("dialog", { name: "Add node" });
    await user.click(within(dialog).getByRole("button", { name: "Scan" }));

    expect(await within(dialog).findByText(/mDNS is unavailable/i)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Address *"), "10.0.0.30");
    expect(within(dialog).getByRole("button", { name: "Add node" })).toBeEnabled();
  });

  it("treats a failed scan as no peers, not as an error", async () => {
    const user = userEvent.setup();
    vi.mocked(discoverNodes).mockRejectedValue(new Error("API 500: boom"));
    render(<NodeRegistry />);
    await screen.findByRole("row", { name: /spark-02/ });

    await user.click(screen.getByRole("button", { name: "Add node" }));
    const dialog = screen.getByRole("dialog", { name: "Add node" });
    await user.click(within(dialog).getByRole("button", { name: "Scan" }));

    expect(await within(dialog).findByText(/mDNS is unavailable/i)).toBeInTheDocument();
    expect(within(dialog).queryByRole("alert")).toBeNull();
  });

  it("forgets a peer after confirming, and says what forgetting does not do", async () => {
    const user = userEvent.setup();
    vi.mocked(removeNode).mockResolvedValue({ removed: true, node: PEER });
    render(<NodeRegistry />);
    await screen.findByRole("row", { name: /spark-02/ });

    await user.click(screen.getByRole("button", { name: "Forget spark-02" }));
    expect(screen.getByText(/does not touch the machine itself/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Forget" }));

    await waitFor(() => expect(removeNode).toHaveBeenCalledWith("peer"));
  });

  it("renders each diagnostic finding with its remedy", async () => {
    mockApi({
      findings: [
        {
          code: "duplicate_machine_id",
          severity: "warning",
          summary: "2 nodes report the same machine-id 0f5c9e1a…",
          remedy: "Regenerate it on all but one with systemd-machine-id-setup.",
          node_ids: ["control", "peer"],
        },
        {
          code: "mdns_unavailable",
          severity: "info",
          summary: "mDNS is not available, so peer discovery returns an empty list.",
          remedy: "Adding a node by address always works.",
          node_ids: [],
        },
      ],
    });
    render(<NodeRegistry />);

    const panel = await screen.findByTestId("node-diagnostics");
    expect(panel).toHaveTextContent("2 nodes report the same machine-id");
    expect(panel).toHaveTextContent("systemd-machine-id-setup");
    expect(panel).toHaveTextContent("Adding a node by address always works");
    // Findings are notes, not errors.
    expect(within(panel).getAllByRole("note")).toHaveLength(2);
    expect(within(panel).queryByRole("alert")).toBeNull();
  });

  it("shows nothing at all when there is nothing to report", async () => {
    render(<NodeRegistry />);
    await screen.findByRole("row", { name: /spark-02/ });
    expect(screen.queryByTestId("node-diagnostics")).toBeNull();
  });

  it("surfaces a failure to list nodes rather than showing an empty cluster", async () => {
    vi.mocked(fetchNodes).mockRejectedValue(new Error("API 500: unreadable state file"));
    render(<NodeRegistry />);
    expect(await screen.findByRole("alert")).toHaveTextContent("unreadable state file");
  });

  /** A forget that failed silently is the worst outcome: the row disappears
   *  from nothing, the operator believes the node is gone, and the next
   *  deploy still tries to reach it. The row has to stay and say why. */
  it("keeps the node and says why when forgetting it fails", async () => {
    mockApi();
    vi.mocked(removeNode).mockRejectedValue(
      new Error("API 400: the control plane cannot be removed from the registry"),
    );
    render(<NodeRegistry />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Forget spark-02" }));
    await user.click(screen.getByRole("button", { name: "Forget" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "cannot be removed from the registry",
    );
    expect(screen.getByRole("row", { name: /spark-02/ })).toBeInTheDocument();
  });
});
