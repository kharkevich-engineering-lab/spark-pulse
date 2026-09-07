/** Network discovery, on the page about the machines.
 *
 * These tests came from the Settings page, where this card used to live under
 * a "Cluster" tab whose only other control was a toggle nothing read. What
 * they check has not changed and should not: the panel reports what it found
 * and never offers to apply it installation-wide, because interface pinning is
 * per node and the node registry is what a deploy actually reads.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import NetworkDiscovery from "@/components/NetworkDiscovery";
import type { DiscoveryResponse } from "@/lib/api";

vi.mock("@/lib/api", () => ({ runDiscovery: vi.fn() }));

import { runDiscovery } from "@/lib/api";

const DISCOVERY: DiscoveryResponse = {
  detected: {
    local_ip: "10.0.0.10",
    ethernet_if: "enp1s0",
    infiniband_present: true,
    infiniband_devices: [{ hca: "mlx5_0", ports: [1], net_devices: ["ib0"], state: "ACTIVE" }],
    interfaces: [],
    nccl_defaults: { socket_ifname: "enp1s0", ib_hca: "mlx5_0", ib_disable: false },
    validation_errors: [],
  },
  validation: { healthy: true, warnings: [], errors: [] },
};

async function discover() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /discover/i }));
}

describe("NetworkDiscovery", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runDiscovery).mockResolvedValue(DISCOVERY);
  });

  it("offers no global NCCL fields to edit", async () => {
    render(<NetworkDiscovery />);
    await screen.findByRole("heading", { name: "Network Discovery" });

    expect(screen.queryByPlaceholderText("auto-detect")).toBeNull();
    expect(screen.queryByText(/Leave empty to auto-detect/i)).toBeNull();
  });

  it("shows what was detected without offering to apply it installation-wide", async () => {
    render(<NetworkDiscovery />);
    await discover();

    expect(await screen.findByText("NCCL socket")).toBeInTheDocument();
    expect(screen.getByText("mlx5_0", { selector: "code" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /apply detected nccl/i })).toBeNull();
  });

  it("points at the node registry, which is what a deploy actually reads", async () => {
    render(<NetworkDiscovery />);
    await discover();

    const pointer = await screen.findByText(/Interface pinning is per node, not global/i);
    expect(pointer).toHaveTextContent(/registry/i);
    expect(pointer).toHaveTextContent(/Cluster page/i);
  });

  it("surfaces a failed discovery instead of pretending nothing was detected", async () => {
    vi.mocked(runDiscovery).mockRejectedValue(new Error("API 500: no interfaces"));
    render(<NetworkDiscovery />);
    await discover();

    expect(await screen.findByText(/no interfaces/)).toBeInTheDocument();
  });

  it("says nothing has been discovered yet rather than showing an empty panel", async () => {
    render(<NetworkDiscovery />);

    expect(await screen.findByText(/Reports only — nothing is saved/)).toBeInTheDocument();
  });

  it("names the interfaces it could not find rather than reporting a clean bill", async () => {
    vi.mocked(runDiscovery).mockResolvedValue({
      detected: {
        ...DISCOVERY.detected,
        local_ip: "",
        ethernet_if: "",
        infiniband_present: false,
        infiniband_devices: [],
        nccl_defaults: { socket_ifname: "lo", ib_hca: "", ib_disable: true },
      },
      validation: {
        healthy: false,
        warnings: ["no InfiniBand HCA in ACTIVE state"],
        errors: ["no routable ethernet interface"],
      },
    });
    render(<NetworkDiscovery />);
    await discover();

    expect(await screen.findByText("Network: Issues found")).toBeInTheDocument();
    expect(screen.getByText(/no routable ethernet interface/)).toBeInTheDocument();
    expect(screen.getByText(/no InfiniBand HCA in ACTIVE state/)).toBeInTheDocument();
    expect(screen.getAllByText("not detected").length).toBe(2);
    expect(screen.getByText("not present")).toBeInTheDocument();
  });
});
