/** Settings after the global NCCL fields went away.
 *
 * Interface pinning is a property of a node, not of the installation, so the
 * two free-text NCCL boxes and the button that wrote them globally are gone
 * along with the endpoint behind them. What is left is the detection, read
 * only, and a line saying where the value actually belongs. Nothing else on
 * the page was meant to change, so this file also checks it still renders.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SettingsPage from "@/pages/SettingsPage";
import type { DiscoveryResponse } from "@/lib/api";
import type { Settings } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchSettings: vi.fn(),
  updateSettings: vi.fn(),
  fetchSecrets: vi.fn(),
  saveSecrets: vi.fn(),
  deleteSecret: vi.fn(),
  runDiscovery: vi.fn(),
  fetchEngines: vi.fn(),
  refreshEngines: vi.fn(),
}));

import { fetchEngines, fetchSecrets, fetchSettings, runDiscovery } from "@/lib/api";

const SETTINGS: Settings = {
  spark_vllm_path: "/opt/spark-vllm-docker",
  default_container: "vllm-node",
  default_gpu_mem_util: 0.8,
  default_port_range_start: 9000,
  default_port_range_end: 9100,
  webui_port: 8100,
  cluster_enabled: false,
  cluster_experimental: true,
  job_retention_days: 7,
  benchmarking_enabled: false,
  env_managed: [],
};

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
  await user.click(screen.getByRole("button", { name: /discover/i }));
}

describe("SettingsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchSettings).mockResolvedValue(SETTINGS);
    vi.mocked(fetchSecrets).mockResolvedValue({ hf_token: "" });
    vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "vllm", engines: [] });
    vi.mocked(runDiscovery).mockResolvedValue(DISCOVERY);
  });

  it("renders the settings it still owns", async () => {
    render(<SettingsPage />);

    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Docker" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Network Discovery" })).toBeInTheDocument();
    // The form is seeded from the fetched settings a tick after they land.
    expect(await screen.findByDisplayValue("/opt/spark-vllm-docker")).toBeInTheDocument();
  });

  it("offers no global NCCL fields to edit", async () => {
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Settings" });

    // The two removed inputs, by the text that was only ever theirs.
    expect(screen.queryByPlaceholderText("auto-detect")).toBeNull();
    expect(screen.queryByText(/Leave empty to auto-detect/i)).toBeNull();
    expect(screen.queryByText(/InfiniBand HCA selector/i)).toBeNull();
  });

  it("shows what was detected without offering to apply it installation-wide", async () => {
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Settings" });
    await discover();

    // Detection survives: it is the useful half.
    expect(await screen.findByText("NCCL socket")).toBeInTheDocument();
    expect(screen.getByText("mlx5_0", { selector: "code" })).toBeInTheDocument();
    // The button that wrote it globally does not, and neither does its endpoint.
    expect(screen.queryByRole("button", { name: /apply detected nccl/i })).toBeNull();
  });

  it("points at the node registry, which is what a deploy actually reads", async () => {
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Settings" });
    await discover();

    const pointer = await screen.findByText(/Interface pinning is per node, not global/i);
    expect(pointer).toHaveTextContent(/registry/i);
    expect(pointer).toHaveTextContent(/Cluster page/i);
  });

  it("surfaces a failed discovery instead of pretending nothing was detected", async () => {
    vi.mocked(runDiscovery).mockRejectedValue(new Error("API 500: no interfaces"));
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Settings" });
    await discover();

    expect(await screen.findByText(/no interfaces/)).toBeInTheDocument();
  });
});
