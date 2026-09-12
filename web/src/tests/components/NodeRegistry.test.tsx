import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import NodeRegistry from "@/components/NodeRegistry";
import type {
  ClusterNode,
  DiscoverNodesResult,
  InstallReport,
  NodeFinding,
} from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchNodes: vi.fn(),
  addNode: vi.fn(),
  removeNode: vi.fn(),
  discoverNodes: vi.fn(),
  fetchNodeDiagnostics: vi.fn(),
  fetchNodeHostKey: vi.fn(),
  installNodeAgent: vi.fn(),
}));

import {
  addNode,
  discoverNodes,
  fetchNodeDiagnostics,
  fetchNodeHostKey,
  fetchNodes,
  installNodeAgent,
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
    vi.mocked(addNode).mockResolvedValue(
      node({ id: "new", name: "10.0.0.12", address: "10.0.0.12", ssh_user: "spark" }),
    );
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
      }),
    );
    // The list is reloaded, so what is shown is what the server holds.
    await waitFor(() => expect(fetchNodes).toHaveBeenCalledTimes(2));
    // Registering is half of it: the install follows, for the node just added.
    const install = await screen.findByRole("dialog", { name: /Install the agent on/ });
    expect(install).toHaveTextContent("10.0.0.12");
    expect(within(install).getByLabelText("SSH user *")).toHaveValue("spark");
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

// ── Installing the agent ────────────────────────────────────────────────────

const HOST_KEY = {
  host: "10.0.0.11",
  port: 22,
  algorithm: "ssh-ed25519",
  fingerprint: "SHA256:iQekJXsjX5A9WL86d1LHASi+Gzujg1PU2AGBUK14Hkw",
};

function report(overrides: Partial<InstallReport> = {}): InstallReport {
  return {
    host: "10.0.0.11",
    username: "spark",
    name: "spark-02",
    node_id: "peer",
    scope: "user",
    scope_reason: "docker socket, lingering user manager",
    converged: true,
    connected: true,
    host_key_fingerprint: HOST_KEY.fingerprint,
    public_key_fingerprint: "SHA256:cp",
    key_generated: true,
    used_password: true,
    capabilities: {},
    privileged_calls: [],
    concessions: [],
    steps: ["reached 10.0.0.11:22", "installed the control plane's public key", "peer is connected"],
    bundle: {},
    unit_path: "~/.config/systemd/user/spark-pulse-agent.service",
    identity_dir: "~/.config/spark-pulse/agent",
    ...overrides,
  };
}

async function openInstall(user: ReturnType<typeof userEvent.setup>) {
  render(<NodeRegistry />);
  await screen.findByRole("row", { name: /spark-02/ });
  await user.click(screen.getByRole("button", { name: "Install agent on spark-02" }));
  return screen.getByRole("dialog", { name: "Install the agent on spark-02" });
}

describe("InstallAgentDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApi();
    vi.mocked(fetchNodeHostKey).mockResolvedValue(HOST_KEY);
    vi.mocked(installNodeAgent).mockResolvedValue(report());
  });

  it("offers the install on a peer and not on the control plane", async () => {
    render(<NodeRegistry />);
    await screen.findByRole("row", { name: /spark-02/ });
    expect(screen.getByRole("button", { name: "Install agent on spark-02" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Install agent on spark-01" })).toBeNull();
  });

  it("says a node has no agent when the transport says so", async () => {
    mockApi({
      nodes: [CONTROL, { ...PEER, agent: { enrolled: false, connected: false } }],
    });
    render(<NodeRegistry />);
    const row = await screen.findByRole("row", { name: /spark-02/ });
    expect(row).toHaveTextContent("No agent");
    expect(screen.getByRole("button", { name: "Install agent on spark-02" })).toHaveAttribute(
      "title",
      "Install agent",
    );
  });

  it("calls it a reinstall on a node that already has an agent", async () => {
    mockApi({
      nodes: [CONTROL, { ...PEER, agent: { enrolled: true, connected: true } }],
    });
    render(<NodeRegistry />);
    const row = await screen.findByRole("row", { name: /spark-02/ });
    expect(row).not.toHaveTextContent("No agent");
    expect(screen.getByRole("button", { name: "Install agent on spark-02" })).toHaveAttribute(
      "title",
      "Reinstall agent",
    );
  });

  it("cannot install until the host key has been checked, and sends nothing to check it", async () => {
    const user = userEvent.setup();
    const dialog = await openInstall(user);
    await user.type(within(dialog).getByLabelText("SSH user *"), "spark");
    await user.type(within(dialog).getByLabelText("Password *"), "hunter2");
    const run = within(dialog).getByRole("button", { name: "Install agent" });
    expect(run).toBeDisabled();
    expect(run).toHaveAttribute("title", "Enabled once the host key has been checked.");

    await user.click(within(dialog).getByRole("button", { name: "Check host key" }));
    expect(await within(dialog).findByTestId("host-key-fingerprint")).toHaveTextContent(
      "ssh-ed25519 SHA256:iQekJXsjX5A9WL86d1LHASi+Gzujg1PU2AGBUK14Hkw",
    );
    expect(fetchNodeHostKey).toHaveBeenCalledWith("peer", 22);
    expect(installNodeAgent).not.toHaveBeenCalled();
    expect(run).toBeEnabled();
  });

  it("installs with a password and shows the report", async () => {
    const user = userEvent.setup();
    const dialog = await openInstall(user);
    await user.type(within(dialog).getByLabelText("SSH user *"), "spark");
    await user.type(within(dialog).getByLabelText("Password *"), "hunter2");
    await user.type(within(dialog).getByLabelText("sudo password"), "sudo-secret");
    await user.click(within(dialog).getByRole("button", { name: "Check host key" }));
    await within(dialog).findByTestId("host-key-fingerprint");
    await user.click(within(dialog).getByRole("button", { name: "Install agent" }));

    await waitFor(() =>
      expect(installNodeAgent).toHaveBeenCalledWith("peer", {
        username: "spark",
        auth: "password",
        host_key_fingerprint: HOST_KEY.fingerprint,
        port: 22,
        password: "hunter2",
        sudo_password: "sudo-secret",
      }),
    );
    const outcome = await within(dialog).findByTestId("install-report");
    expect(outcome).toHaveTextContent("Installed and connected.");
    expect(outcome).toHaveTextContent("Installed as a user unit: docker socket, lingering user manager");
    expect(outcome).toHaveTextContent("installed the control plane's public key");
    expect(outcome).toHaveTextContent("Privileged calls: 0");
    // The list is reloaded so the row reads what the hub now says.
    await waitFor(() => expect(fetchNodes).toHaveBeenCalledTimes(2));
    await user.click(within(dialog).getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("takes a private key from a file, with its passphrase", async () => {
    const user = userEvent.setup();
    const dialog = await openInstall(user);
    await user.type(within(dialog).getByLabelText("SSH user *"), "spark");
    await user.click(within(dialog).getByLabelText("Private key"));

    const file = new File(["-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"], "id_ed25519", {
      type: "text/plain",
    });
    await user.upload(within(dialog).getByLabelText("Upload a key file"), file);
    await waitFor(() =>
      expect(within(dialog).getByLabelText("Private key *")).toHaveValue(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n",
      ),
    );
    await user.type(within(dialog).getByLabelText("Passphrase"), "open sesame");

    await user.click(within(dialog).getByRole("button", { name: "Check host key" }));
    await within(dialog).findByTestId("host-key-fingerprint");
    await user.click(within(dialog).getByRole("button", { name: "Install agent" }));

    await waitFor(() =>
      expect(installNodeAgent).toHaveBeenCalledWith("peer", {
        username: "spark",
        auth: "key",
        host_key_fingerprint: HOST_KEY.fingerprint,
        port: 22,
        private_key: "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n",
        passphrase: "open sesame",
      }),
    );
  });

  it("takes a pasted key without a passphrase, and no stray password", async () => {
    const user = userEvent.setup();
    const dialog = await openInstall(user);
    await user.type(within(dialog).getByLabelText("SSH user *"), "spark");
    await user.type(within(dialog).getByLabelText("Password *"), "typed-then-switched");
    await user.click(within(dialog).getByLabelText("Private key"));
    await user.type(within(dialog).getByLabelText("Private key *"), "KEY");
    await user.click(within(dialog).getByRole("button", { name: "Check host key" }));
    await within(dialog).findByTestId("host-key-fingerprint");
    await user.click(within(dialog).getByRole("button", { name: "Install agent" }));

    await waitFor(() =>
      expect(installNodeAgent).toHaveBeenCalledWith("peer", {
        username: "spark",
        auth: "key",
        host_key_fingerprint: HOST_KEY.fingerprint,
        port: 22,
        private_key: "KEY",
      }),
    );
  });

  it("uses the control plane's own key with no secret at all, on another port", async () => {
    const user = userEvent.setup();
    const dialog = await openInstall(user);
    await user.type(within(dialog).getByLabelText("SSH user *"), "spark");
    await user.click(within(dialog).getByLabelText("The control plane's own key"));
    expect(dialog).toHaveTextContent(/already holds this control plane's public key/);
    const port = within(dialog).getByLabelText("SSH port");
    await user.clear(port);
    await user.type(port, "2222");
    await user.click(within(dialog).getByRole("button", { name: "Check host key" }));
    await within(dialog).findByTestId("host-key-fingerprint");
    expect(fetchNodeHostKey).toHaveBeenCalledWith("peer", 2222);
    await user.click(within(dialog).getByRole("button", { name: "Install agent" }));

    await waitFor(() =>
      expect(installNodeAgent).toHaveBeenCalledWith("peer", {
        username: "spark",
        auth: "control_plane_key",
        host_key_fingerprint: HOST_KEY.fingerprint,
        port: 2222,
      }),
    );
  });

  it("forgets a checked host key when the port changes", async () => {
    const user = userEvent.setup();
    const dialog = await openInstall(user);
    await user.click(within(dialog).getByRole("button", { name: "Check host key" }));
    await within(dialog).findByTestId("host-key-fingerprint");
    await user.type(within(dialog).getByLabelText("SSH port"), "2");
    // The fingerprint was for port 22; on 222 it has to be fetched again.
    expect(within(dialog).queryByTestId("host-key-fingerprint")).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Check host key" })).toBeEnabled();
    // And a port no sshd can listen on cannot even be asked.
    await user.clear(within(dialog).getByLabelText("SSH port"));
    await user.type(within(dialog).getByLabelText("SSH port"), "0");
    expect(within(dialog).getByRole("button", { name: "Check host key" })).toBeDisabled();
  });

  it("reports a host key it could not read", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchNodeHostKey).mockRejectedValue(
      new Error("API 502: cannot reach 10.0.0.11:22: no route to host"),
    );
    const dialog = await openInstall(user);
    await user.click(within(dialog).getByRole("button", { name: "Check host key" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("no route to host");
    expect(within(dialog).queryByTestId("host-key-fingerprint")).toBeNull();
  });

  it("drops the fingerprint when the node offered a different host key", async () => {
    const user = userEvent.setup();
    vi.mocked(installNodeAgent).mockRejectedValue(
      new Error("API 409: 10.0.0.11:22 now offers host key SHA256:other, not the one confirmed"),
    );
    const dialog = await openInstall(user);
    await user.type(within(dialog).getByLabelText("SSH user *"), "spark");
    await user.type(within(dialog).getByLabelText("Password *"), "hunter2");
    await user.click(within(dialog).getByRole("button", { name: "Check host key" }));
    await within(dialog).findByTestId("host-key-fingerprint");
    await user.click(within(dialog).getByRole("button", { name: "Install agent" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("now offers host key");
    expect(within(dialog).queryByTestId("host-key-fingerprint")).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Install agent" })).toBeDisabled();
  });

  it("keeps the form and says why when the credentials are refused", async () => {
    const user = userEvent.setup();
    vi.mocked(installNodeAgent).mockRejectedValue(
      new Error("API 401: spark@10.0.0.11 refused the credentials offered"),
    );
    const dialog = await openInstall(user);
    await user.type(within(dialog).getByLabelText("SSH user *"), "spark");
    await user.type(within(dialog).getByLabelText("Password *"), "wrong");
    await user.click(within(dialog).getByRole("button", { name: "Check host key" }));
    await within(dialog).findByTestId("host-key-fingerprint");
    await user.click(within(dialog).getByRole("button", { name: "Install agent" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("refused the credentials");
    // The host key is still the one shown: nothing about it changed.
    expect(within(dialog).getByTestId("host-key-fingerprint")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Install agent" })).toBeEnabled();
    expect(fetchNodes).toHaveBeenCalledTimes(1);
  });

  it("says so when the agent was installed but has not dialled home, with what it went without", async () => {
    const user = userEvent.setup();
    vi.mocked(installNodeAgent).mockResolvedValue(
      report({
        connected: false,
        scope: "system",
        scope_reason: "no user manager",
        privileged_calls: [{ why: "write the system unit", command: "sudo -n tee" }],
        concessions: [
          {
            capability: "dial-home",
            detail: "the agent was installed and started but peer has not appeared in the hub",
            cost: "check that the node can reach 10.0.0.10:8110",
          },
        ],
      }),
    );
    const dialog = await openInstall(user);
    await user.type(within(dialog).getByLabelText("SSH user *"), "spark");
    await user.type(within(dialog).getByLabelText("Password *"), "hunter2");
    await user.click(within(dialog).getByRole("button", { name: "Check host key" }));
    await within(dialog).findByTestId("host-key-fingerprint");
    await user.click(within(dialog).getByRole("button", { name: "Install agent" }));

    const outcome = await within(dialog).findByTestId("install-report");
    expect(outcome).toHaveTextContent("Installed, but the agent has not dialled home yet.");
    expect(outcome).toHaveTextContent("Installed as a system unit: no user manager");
    expect(within(outcome).getByRole("note")).toHaveTextContent("check that the node can reach");
    expect(outcome).toHaveTextContent("Privileged calls: 1");
  });

  it("can be put off for later", async () => {
    const user = userEvent.setup();
    const dialog = await openInstall(user);
    await user.click(within(dialog).getByRole("button", { name: "Later" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(installNodeAgent).not.toHaveBeenCalled();
  });
});

