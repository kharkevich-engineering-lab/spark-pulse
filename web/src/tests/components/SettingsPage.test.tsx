/** Settings after the global NCCL fields went away.
 *
 * Interface pinning is a property of a node, not of the installation, so the
 * two free-text NCCL boxes and the button that wrote them globally are gone
 * along with the endpoint behind them. What is left is the detection, read
 * only, and a line saying where the value actually belongs. Nothing else on
 * the page was meant to change, so this file also checks it still renders.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SettingsPage from "@/pages/SettingsPage";
import type { DiscoveryResponse } from "@/lib/api";
import type { EngineSummary, Settings } from "@/lib/types";

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

import {
  deleteSecret,
  fetchEngines,
  fetchSecrets,
  fetchSettings,
  refreshEngines,
  runDiscovery,
  saveSecrets,
  updateSettings,
} from "@/lib/api";

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

  it("says nothing has been discovered yet rather than showing an empty panel", async () => {
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Settings" });

    expect(
      screen.getByText(/Click "Discover" to detect network interfaces/),
    ).toBeInTheDocument();
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
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Settings" });
    await discover();

    expect(await screen.findByText("Network: Issues found")).toBeInTheDocument();
    expect(screen.getByText(/no routable ethernet interface/)).toBeInTheDocument();
    expect(screen.getByText(/no InfiniBand HCA in ACTIVE state/)).toBeInTheDocument();
    expect(screen.getAllByText("not detected").length).toBe(2);
    expect(screen.getByText("not present")).toBeInTheDocument();
  });
});

/** Saving.
 *
 * The Save button writes the whole form to `~/.config/spark-pulse/settings.json`,
 * so the two things worth pinning are that it stays disabled until something
 * actually changed — an accidental click must not rewrite the file — and that
 * a rejected write says so rather than flashing "Saved!" on a save that did
 * not happen.
 */
describe("SettingsPage saving", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchSettings).mockResolvedValue(SETTINGS);
    vi.mocked(fetchSecrets).mockResolvedValue({ hf_token: "" });
    vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "vllm", engines: [] });
    vi.mocked(updateSettings).mockResolvedValue(SETTINGS);
  });

  it("will not save a form nobody has changed", async () => {
    render(<SettingsPage />);
    await screen.findByDisplayValue("/opt/spark-vllm-docker");

    expect(screen.getByRole("button", { name: /save settings/i })).toBeDisabled();
  });

  it("saves the edited fields and confirms where they landed", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    const path = await screen.findByDisplayValue("/opt/spark-vllm-docker");

    await user.clear(path);
    await user.type(path, "/srv/spark");
    const save = screen.getByRole("button", { name: /save settings/i });
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ spark_vllm_path: "/srv/spark" }),
      ),
    );
    expect(await screen.findByText(/Saved to/)).toBeInTheDocument();
  });

  it("says why a save was refused instead of claiming it worked", async () => {
    const user = userEvent.setup();
    vi.mocked(updateSettings).mockRejectedValue(new Error("API 403: settings are read-only"));
    render(<SettingsPage />);
    const container = await screen.findByDisplayValue("vllm-node");

    await user.type(container, "-x");
    await user.click(screen.getByRole("button", { name: /save settings/i }));

    expect(await screen.findByText("API 403: settings are read-only")).toBeInTheDocument();
    expect(screen.queryByText(/Saved to/)).toBeNull();
  });

  /** A field the environment owns cannot be edited here, because the process
   *  would read the env var back over whatever was typed on the next start. */
  it("locks a field the environment owns, and says which variable owns it", async () => {
    vi.mocked(fetchSettings).mockResolvedValue({ ...SETTINGS, env_managed: ["spark_vllm_path"] });
    render(<SettingsPage />);

    const path = await screen.findByDisplayValue("/opt/spark-vllm-docker");
    expect(path).toBeDisabled();
    expect(screen.getByText(/Controlled by SPARK_VLLM_PATH/)).toBeInTheDocument();
  });

  it("carries the Docker and cluster toggles into the saved form", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByDisplayValue("/opt/spark-vllm-docker");

    // Privileged defaults on; the toggles are the only unlabelled controls in
    // their rows, so each is reached through the text that describes it.
    const privileged = screen.getByText("Privileged mode").closest("div")!.parentElement!;
    await user.click(within(privileged).getByRole("button"));

    const clusterMode = screen.getByText("Cluster mode").closest("div")!.parentElement!;
    await user.click(within(clusterMode).getByRole("button"));

    await user.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          cluster_enabled: true,
          docker: expect.objectContaining({ privileged: false }),
        }),
      ),
    );
  });

  it("keeps the numeric limits numeric rather than saving the typed string", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByDisplayValue("/opt/spark-vllm-docker");

    // Selected-then-typed rather than cleared-then-typed: these number inputs
    // fall back to their default on an empty value, so clearing one puts the
    // default back and the next keystroke appends to it.
    const shm = screen.getByPlaceholderText("64");
    await user.tripleClick(shm);
    await user.keyboard("32");

    const retention = screen.getByDisplayValue("7");
    await user.clear(retention);
    await user.type(retention, "30");

    await user.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          job_retention_days: 30,
          docker: expect.objectContaining({ shm_size_gb: 32 }),
        }),
      ),
    );
  });

  /** Everything else on the form, in one pass. Each of these is a plain
   *  input, so the property worth holding is only that what was typed is what
   *  gets written — a field wired to the wrong key silently saves nothing. */
  it("writes every remaining field under the key it belongs to", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByDisplayValue("/opt/spark-vllm-docker");

    // `fireEvent.change` rather than typing: jsdom's `<input type="number">`
    // has no usable text selection, so "select all and type" appends to the
    // old value instead of replacing it. What is being asserted here is the
    // wiring, not the keystrokes.
    const replace = (element: HTMLElement, value: string) =>
      fireEvent.change(element, { target: { value } });

    replace(screen.getByDisplayValue("0.8"), "0.55");
    replace(screen.getByPlaceholderText("9000"), "9500");
    replace(screen.getByPlaceholderText("9100"), "9600");
    replace(screen.getByPlaceholderText("110"), "96");
    replace(screen.getByPlaceholderText("4096"), "8192");
    replace(screen.getByPlaceholderText("eugr/spark-vllm-docker:latest"), "acme/cluster:1");
    replace(screen.getByPlaceholderText("29501"), "29777");
    replace(screen.getByDisplayValue("8"), "4");
    replace(screen.getByPlaceholderText("vllm"), "sglang");

    const clusterToggle = screen.getByText("Enable cluster mode").closest("div")!.parentElement!;
    await user.click(within(clusterToggle).getByRole("button"));

    await user.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          default_gpu_mem_util: 0.55,
          default_port_range_start: 9500,
          default_port_range_end: 9600,
          default_engine: "sglang",
          docker: expect.objectContaining({
            memory_limit_gb: 96,
            pids_limit: 8192,
            cluster_image: "acme/cluster:1",
            ray_port: 29777,
            gpu_count: 4,
            cluster_enabled: true,
          }),
        }),
      ),
    );
  });

  /** The "Health Monitoring" switch is gone. It set a piece of React state and
   *  nothing else: there was no monitor behind it, it called no endpoint, and
   *  it reset on every reload. Engine metrics need no setting. */
  it("no longer offers a health-monitoring switch that does nothing", async () => {
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Settings" });

    expect(screen.queryByText("Health Monitoring:")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Health Monitoring" })).toBeNull();
  });
});

/** The HuggingFace token.
 *
 * It is written to a 0600 file and passed as `HF_TOKEN` to every deployment,
 * so it is never echoed back: the page shows only the last four characters of
 * whatever is stored. What has to hold is that saving clears the input (so the
 * token is not left sitting in the DOM), that clearing actually deletes it,
 * and that either failing says so.
 */
describe("SettingsPage secrets", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchSettings).mockResolvedValue(SETTINGS);
    vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "vllm", engines: [] });
    vi.mocked(fetchSecrets).mockResolvedValue({ hf_token: "" });
    vi.mocked(saveSecrets).mockResolvedValue({ hf_token: "••••cdef" });
    vi.mocked(deleteSecret).mockResolvedValue(undefined);
  });

  it("will not save an empty token, and clears the field once one is stored", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Secrets" });

    const field = screen.getByPlaceholderText("hf_…");
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();

    await user.type(field, "hf_abcdef");
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);

    await waitFor(() => expect(saveSecrets).toHaveBeenCalledWith({ hf_token: "hf_abcdef" }));
    await waitFor(() => expect(field).toHaveValue(""));
  });

  it("hides the token by default and reveals it only on request", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Secrets" });

    const field = screen.getByPlaceholderText("hf_…");
    expect(field).toHaveAttribute("type", "password");

    // The eye sits inside the field's own wrapper, next to no other button.
    await user.click(within(field.parentElement!).getByRole("button"));
    expect(field).toHaveAttribute("type", "text");
  });

  it("shows only the tail of a stored token, and offers to clear it", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchSecrets).mockResolvedValue({ hf_token: "••••••••wxyz" });
    render(<SettingsPage />);

    expect(await screen.findByText("Active ···wxyz")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Enter new token to replace…")).toBeInTheDocument();

    await user.click(screen.getByTitle("Clear token"));
    await waitFor(() => expect(deleteSecret).toHaveBeenCalledWith("hf_token"));
  });

  it("says why a token could not be stored", async () => {
    const user = userEvent.setup();
    vi.mocked(saveSecrets).mockRejectedValue(new Error("API 500: secrets file is read-only"));
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Secrets" });

    await user.type(screen.getByPlaceholderText("hf_…"), "hf_abcdef");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("API 500: secrets file is read-only")).toBeInTheDocument();
  });

  it("says why a token could not be cleared", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchSecrets).mockResolvedValue({ hf_token: "••••••••wxyz" });
    vi.mocked(deleteSecret).mockRejectedValue(new Error("API 500: secrets file is read-only"));
    render(<SettingsPage />);
    await screen.findByText("Active ···wxyz");

    await user.click(screen.getByTitle("Clear token"));

    expect(await screen.findByText("API 500: secrets file is read-only")).toBeInTheDocument();
  });
});

/** Engines, and the two states the page can be in before settings arrive. */
describe("SettingsPage engines and loading", () => {
  const ENGINE: EngineSummary = {
    engine: "vllm",
    variant: "default",
    key: "vllm/default",
    description: "",
    image: "ghcr.io/acme/engine/vllm",
    image_ref: "ghcr.io/acme/engine/vllm:0.1.0",
    version: "0.1.0",
    tag: "0.1.0",
    digest: null,
    legacy_tags: [],
    capabilities: { mods: true, solo: true, cluster: true },
    verified: [],
    ports: { api: 8000, rendezvous: 29500 },
    readiness: "/v1/models",
    models_endpoint: "/v1/models",
    metrics: null,
    source: "bundled",
    enabled: true,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchSettings).mockResolvedValue(SETTINGS);
    vi.mocked(fetchSecrets).mockResolvedValue({ hf_token: "" });
    vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "vllm", engines: [] });
    vi.mocked(refreshEngines).mockResolvedValue({ refreshed: true, engines: 1, indexes: [] });
  });

  it("re-reads the engine indexes on request", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Engines" });

    await user.click(screen.getByRole("button", { name: /refresh/i }));

    await waitFor(() => expect(refreshEngines).toHaveBeenCalled());
    // Refreshing an index is pointless unless the list is re-read after it.
    await waitFor(() => expect(vi.mocked(fetchEngines).mock.calls.length).toBeGreaterThan(1));
  });

  it("says why an index refresh failed", async () => {
    const user = userEvent.setup();
    vi.mocked(refreshEngines).mockRejectedValue(new Error("API 502: ghcr.io unreachable"));
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Engines" });

    await user.click(screen.getByRole("button", { name: /refresh/i }));

    expect(await screen.findByText("API 502: ghcr.io unreachable")).toBeInTheDocument();
  });

  it("lists the engines the registry holds, with the default marked", async () => {
    vi.mocked(fetchEngines).mockResolvedValue({
      default_engine: "vllm",
      engines: [ENGINE],
    });
    render(<SettingsPage />);

    expect(await screen.findByText("vllm")).toBeInTheDocument();
    expect(screen.getByText("default")).toBeInTheDocument();
    expect(screen.getByText("v0.1.0")).toBeInTheDocument();
  });

  it("says the registry is empty rather than showing a blank panel", async () => {
    render(<SettingsPage />);

    expect(await screen.findByText("No engines available.")).toBeInTheDocument();
  });

  it("shows nothing but a spinner until the settings arrive", () => {
    vi.mocked(fetchSettings).mockReturnValue(new Promise(() => {}));
    render(<SettingsPage />);

    expect(screen.queryByRole("heading", { name: "Settings" })).toBeNull();
  });

  it("surfaces a failed load in place of the whole form", async () => {
    vi.mocked(fetchSettings).mockRejectedValue(new Error("API 500: settings.json is corrupt"));
    render(<SettingsPage />);

    expect(await screen.findByText("API 500: settings.json is corrupt")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Settings" })).toBeNull();
  });
});
