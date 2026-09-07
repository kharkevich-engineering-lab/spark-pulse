/** Settings, as tabs over one form.
 *
 * Six cards on one scrolling page put "how much VRAM per deployment" beside
 * "which origins may call this API". They are not the same kind of decision
 * and are never made at the same time, so the page is grouped by *when you go
 * looking*. That makes tab selection part of nearly every test here.
 *
 * Two properties matter more than the layout. Every control has to write the
 * key it claims to — the Docker card used to render literals from the JSX and
 * post a `docker` block the API refused outright, so it displayed values this
 * machine did not have and saved none of them. And the read-only tab has to
 * stay read-only: what it reports is exactly what a browser must not be able
 * to change.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SettingsPage from "@/pages/SettingsPage";
import type { EngineSummary, Settings } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchSettings: vi.fn(),
  updateSettings: vi.fn(),
  fetchSecrets: vi.fn(),
  saveSecrets: vi.fn(),
  deleteSecret: vi.fn(),
  fetchEngines: vi.fn(),
  refreshEngines: vi.fn(),
}));

import {
  deleteSecret,
  fetchEngines,
  fetchSecrets,
  fetchSettings,
  refreshEngines,
  saveSecrets,
  updateSettings,
} from "@/lib/api";

const SETTINGS: Settings = {
  spark_vllm_path: "/opt/spark-vllm-docker",
  default_port_range_start: 9000,
  default_port_range_end: 9100,
  webui_port: 8100,
  cluster_enabled: false,
  cluster_experimental: true,
  job_retention_days: 7,
  benchmarking_enabled: false,
  runtime: "native",
  deploy_ready_timeout_seconds: 600,
  docker_pull_stall_timeout_seconds: 300,
  default_engine: "vllm",
  engine_indexes: ["https://acme.test/engines.json"],
  engine_index_cache_ttl_seconds: 3600,
  docker: {
    privileged: true,
    memory_limit_gb: 110,
    memory_swap_limit_gb: null,
    shm_size_gb: 64,
    pids_limit: 4096,
    nofile_limit: 1048576,
    cache_dirs: ["~/.cache/vllm", "~/.triton"],
    keep_entrypoint: false,
  },
  mod: { network_policy: "warn" },
  env_managed: [],
  environment: {
    database_url: "postgresql+psycopg://pulse:***@db.internal/spark_pulse",
    database_backend: "postgresql+psycopg",
    external_url: "https://pulse.acme.test",
    cors_allowed_origins: ["http://localhost:8100"],
    auth_enabled: true,
    oidc_provider_url: "https://id.acme.test",
    mcp_enabled: true,
    mcp_path: "/mcp",
    cluster_experimental: true,
    thread_pool_size: 40,
    image_registry: { mode: "proxy", address: "10.0.0.1", port: 5000, upstream: "https://ghcr.io" },
  },
};

/** Open one tab. The page remembers the last one per browser, so every test
 *  that needs a particular tab has to say so rather than assume the default. */
async function openTab(name: RegExp | string) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("tab", { name }));
}

function seedApi() {
  vi.clearAllMocks();
  localStorage.clear();
  vi.mocked(fetchSettings).mockResolvedValue(SETTINGS);
  vi.mocked(fetchSecrets).mockResolvedValue({ hf_token: "" });
  vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "vllm", engines: [] });
  vi.mocked(updateSettings).mockResolvedValue(SETTINGS);
}

// ── The tabs themselves ─────────────────────────────────────────────────────

describe("SettingsPage tabs", () => {
  beforeEach(seedApi);

  it("opens on Deployment and shows only that tab's controls", async () => {
    render(<SettingsPage />);

    expect(await screen.findByRole("heading", { name: "Deployment Defaults" })).toBeInTheDocument();
    // Container limits live on another tab; showing them all at once is the
    // thing the tabs exist to stop.
    expect(screen.queryByRole("heading", { name: "Container Limits" })).toBeNull();
  });

  it("switches to the tab that was clicked", async () => {
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Deployment Defaults" });

    await openTab(/containers/i);

    expect(await screen.findByRole("heading", { name: "Container Limits" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Deployment Defaults" })).toBeNull();
  });

  /** A save re-reads the settings, which re-renders the page. Landing back on
   *  the first tab after every save would make editing anything else a chore. */
  it("comes back to the tab the operator was last on", async () => {
    const { unmount } = render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Deployment Defaults" });
    await openTab(/engines/i);
    await screen.findByRole("heading", { name: "Engine Registry" });
    unmount();

    render(<SettingsPage />);

    expect(await screen.findByRole("heading", { name: "Engine Registry" })).toBeInTheDocument();
  });

  it("still renders when the browser refuses to remember anything", async () => {
    const boom = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("site data is blocked");
    });
    try {
      render(<SettingsPage />);
      expect(await screen.findByRole("heading", { name: "Deployment Defaults" })).toBeInTheDocument();
    } finally {
      boom.mockRestore();
    }
  });
});

// ── Deployment ──────────────────────────────────────────────────────────────

describe("SettingsPage deployment tab", () => {
  beforeEach(seedApi);

  it("seeds every field from the settings the server sent", async () => {
    render(<SettingsPage />);

    expect(await screen.findByDisplayValue("/opt/spark-vllm-docker")).toBeInTheDocument();
    expect(screen.getByLabelText("Port range start")).toHaveValue(9000);
    expect(screen.getByDisplayValue("600")).toBeInTheDocument();
    expect(screen.getByDisplayValue("300")).toBeInTheDocument();
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

  it("writes each deployment field under the key it belongs to", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await screen.findByDisplayValue("/opt/spark-vllm-docker");

    // `fireEvent.change` rather than typing: jsdom's `<input type="number">`
    // has no usable text selection, so "select all and type" appends to the
    // old value. What is asserted here is the wiring, not the keystrokes.
    const replace = (el: HTMLElement, value: string) => fireEvent.change(el, { target: { value } });
    replace(screen.getByLabelText("Port range start"), "9500");
    replace(screen.getByLabelText("Port range end"), "9600");
    replace(screen.getByDisplayValue("600"), "900");
    replace(screen.getByDisplayValue("300"), "450");
    replace(screen.getByDisplayValue("7"), "30");

    await user.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          default_port_range_start: 9500,
          default_port_range_end: 9600,
          deploy_ready_timeout_seconds: 900,
          docker_pull_stall_timeout_seconds: 450,
          job_retention_days: 30,
        }),
      ),
    );
  });

  /** Two fields on this tab reached no launch: `default_container` named a v1
   *  image that engines replaced, and `default_gpu_mem_util` was superseded by
   *  the recipe's own value. Neither had a reader in the backend. */
  it("no longer offers the v1 container name or a global VRAM share", async () => {
    render(<SettingsPage />);
    await screen.findByDisplayValue("/opt/spark-vllm-docker");

    expect(screen.queryByDisplayValue("vllm-node")).toBeNull();
    expect(screen.queryByText(/Default container/i)).toBeNull();
    expect(screen.queryByText(/GPU memory utilization/i)).toBeNull();
  });
});

// ── Features ────────────────────────────────────────────────────────────────

describe("SettingsPage features tab", () => {
  beforeEach(seedApi);

  /** Benchmarking is not a timeout, which is the card it used to sit in. It
   *  decides whether a route and a sidebar entry exist at all. */
  it("carries the benchmarking switch into the saved form", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await openTab(/features/i);

    await user.click(await screen.findByRole("switch", { name: "Benchmarking" }));
    await user.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ benchmarking_enabled: true }),
      ),
    );
  });

  it("reports the MCP endpoint rather than offering to switch it", async () => {
    render(<SettingsPage />);
    await openTab(/features/i);

    expect(await screen.findByText("/mcp")).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /MCP/i })).toBeNull();
  });

  /** The switch is real — the recipes page reads `cluster_enabled` to decide
   *  whether a `cluster_only` recipe is offered — but it is a feature switch,
   *  not a page of its own. What went away is the tab, and the second copy of
   *  the value in `/api/config` that nothing read. */
  it("keeps the cluster switch, on this tab, still writing what recipes read", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await openTab(/features/i);

    expect(screen.queryByRole("tab", { name: /cluster/i })).toBeNull();
    await user.click(await screen.findByRole("switch", { name: "Cluster mode" }));
    await user.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ cluster_enabled: true }),
      ),
    );
  });

  it("says multi-node is experimental when the build says so", async () => {
    render(<SettingsPage />);
    await openTab(/features/i);

    expect(await screen.findByText(/marked experimental in this build/)).toBeInTheDocument();
  });
});

// ── Containers ──────────────────────────────────────────────────────────────

describe("SettingsPage containers tab", () => {
  beforeEach(seedApi);

  /** The card used to render its defaults from literals in the JSX, so it
   *  showed 110 GB and 64 GB whatever the machine was actually configured
   *  with — and the `docker` block it posted was refused by the API outright,
   *  so none of it ever saved. Both halves are asserted here. */
  it("shows what this machine is configured with, not what the markup says", async () => {
    vi.mocked(fetchSettings).mockResolvedValue({
      ...SETTINGS,
      docker: { ...SETTINGS.docker, memory_limit_gb: 32, shm_size_gb: 8, pids_limit: 512 },
    });
    render(<SettingsPage />);
    await openTab(/containers/i);

    expect(await screen.findByDisplayValue("32")).toBeInTheDocument();
    expect(screen.getByDisplayValue("8")).toBeInTheDocument();
    expect(screen.getByDisplayValue("512")).toBeInTheDocument();
  });

  it("writes each container limit under the key it belongs to", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await openTab(/containers/i);
    await screen.findByRole("heading", { name: "Container Limits" });

    const replace = (el: HTMLElement, value: string) => fireEvent.change(el, { target: { value } });
    replace(screen.getByDisplayValue("110"), "96");
    replace(screen.getByDisplayValue("64"), "32");
    replace(screen.getByDisplayValue("4096"), "8192");
    replace(screen.getByDisplayValue("1048576"), "65536");
    await user.click(screen.getByRole("switch", { name: "Privileged mode" }));

    await user.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          docker: expect.objectContaining({
            memory_limit_gb: 96,
            shm_size_gb: 32,
            pids_limit: 8192,
            nofile_limit: 65536,
            privileged: false,
          }),
        }),
      ),
    );
  });

  /** Empty is "no limit", which is not a limit of zero. Saving 0 would cap
   *  every container at nothing and every deployment would die on start. */
  it("saves an emptied limit as no limit rather than as zero", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await openTab(/containers/i);
    await screen.findByRole("heading", { name: "Container Limits" });

    fireEvent.change(screen.getByDisplayValue("110"), { target: { value: "" } });
    await user.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ docker: expect.objectContaining({ memory_limit_gb: null }) }),
      ),
    );
  });

  it("edits the cache directories as a list, one per line", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await openTab(/containers/i);
    const dirs = await screen.findByLabelText("Cache directories");
    expect(dirs).toHaveValue("~/.cache/vllm\n~/.triton");

    fireEvent.change(dirs, { target: { value: "~/.cache/vllm\n\n  ~/.cache/flashinfer  \n" } });
    await user.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          // Blank lines dropped and each entry trimmed: a stray space would
          // become a mount path that does not exist.
          docker: expect.objectContaining({
            cache_dirs: ["~/.cache/vllm", "~/.cache/flashinfer"],
          }),
        }),
      ),
    );
  });

  it("saves the mod network policy under its own block", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await openTab(/containers/i);

    await user.selectOptions(await screen.findByLabelText("Mod network access policy"), "deny");
    await user.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ mod: { network_policy: "deny" } }),
      ),
    );
  });

  /** These three sat in the Docker block and in this form with no reader
   *  anywhere in the backend — a page that looked like configuration and
   *  configured nothing. The API now refuses them; the form must not send
   *  them either. */
  it("no longer offers the cluster image, Ray port or GPU count", async () => {
    render(<SettingsPage />);
    await openTab(/containers/i);
    await screen.findByRole("heading", { name: "Container Limits" });

    expect(screen.queryByPlaceholderText("eugr/spark-vllm-docker:latest")).toBeNull();
    expect(screen.queryByPlaceholderText("29501")).toBeNull();
    expect(screen.queryByText(/GPU count per node/i)).toBeNull();
    expect(screen.queryByRole("heading", { name: "Cluster Orchestration" })).toBeNull();
  });
});

// ── Cluster ─────────────────────────────────────────────────────────────────

// ── Saving ──────────────────────────────────────────────────────────────────

describe("SettingsPage saving", () => {
  beforeEach(seedApi);

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

  /** One form across every tab. An edit made on Containers and then a switch
   *  to Deployment must not quietly drop the container change. */
  it("saves edits made on a tab the operator has since left", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await openTab(/containers/i);
    fireEvent.change(await screen.findByDisplayValue("4096"), { target: { value: "8192" } });

    await openTab(/deployment/i);
    fireEvent.change(await screen.findByLabelText("Port range end"), { target: { value: "9600" } });
    await user.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          default_port_range_end: 9600,
          docker: expect.objectContaining({ pids_limit: 8192 }),
        }),
      ),
    );
  });

  it("says why a save was refused instead of claiming it worked", async () => {
    const user = userEvent.setup();
    vi.mocked(updateSettings).mockRejectedValue(new Error("API 403: settings are read-only"));
    render(<SettingsPage />);
    const path = await screen.findByDisplayValue("/opt/spark-vllm-docker");

    await user.type(path, "-x");
    await user.click(screen.getByRole("button", { name: /save settings/i }));

    expect(await screen.findByText("API 403: settings are read-only")).toBeInTheDocument();
    expect(screen.queryByText(/Saved to/)).toBeNull();
  });

  /** The "Health Monitoring" switch is gone. It set a piece of React state and
   *  nothing else: there was no monitor behind it, it called no endpoint, and
   *  it reset on every reload. Engine metrics need no setting. */
  it("no longer offers a health-monitoring switch that does nothing", async () => {
    render(<SettingsPage />);
    await screen.findByRole("heading", { name: "Deployment Defaults" });

    expect(screen.queryByText("Health Monitoring:")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Health Monitoring" })).toBeNull();
  });
});

// ── Environment ─────────────────────────────────────────────────────────────

describe("SettingsPage environment tab", () => {
  beforeEach(seedApi);

  it("reports how the process is configured", async () => {
    render(<SettingsPage />);
    await openTab(/environment/i);

    expect(await screen.findByText("postgresql+psycopg://pulse:***@db.internal/spark_pulse")).toBeInTheDocument();
    expect(screen.getByText("https://pulse.acme.test")).toBeInTheDocument();
    expect(screen.getByText("http://localhost:8100")).toBeInTheDocument();
    expect(screen.getByText("enabled")).toBeInTheDocument();
    expect(screen.getByText("/mcp")).toBeInTheDocument();
  });

  /** Everything on this tab is exactly what a browser must not be able to
   *  change: turning authentication off from the settings page would make the
   *  settings page the way past every other check in the system. */
  it("offers nothing to edit and no way to save", async () => {
    render(<SettingsPage />);
    await openTab(/environment/i);
    await screen.findByRole("heading", { name: "Access" });

    expect(screen.queryAllByRole("textbox")).toHaveLength(0);
    expect(screen.queryAllByRole("switch")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /save settings/i })).toBeNull();
  });

  /** How a worker node gets an engine image without pulling from the internet.
   *  It had no UI anywhere, so "why is this node still pulling" had no answer
   *  on any page. */
  it("says where worker nodes pull their images from", async () => {
    render(<SettingsPage />);
    await openTab(/environment/i);

    expect(await screen.findByText("proxy · 10.0.0.1:5000")).toBeInTheDocument();
    expect(screen.getByText(/Pull-through cache of https:\/\/ghcr.io/)).toBeInTheDocument();
  });

  it("says nothing about a registry it could not resolve", async () => {
    vi.mocked(fetchSettings).mockResolvedValue({
      ...SETTINGS,
      environment: { ...SETTINGS.environment!, image_registry: {} },
    });
    render(<SettingsPage />);
    await openTab(/environment/i);
    await screen.findByRole("heading", { name: "Runtime" });

    expect(screen.queryByText("Image registry")).toBeNull();
  });

  it("warns when this control plane answers without authentication", async () => {
    vi.mocked(fetchSettings).mockResolvedValue({
      ...SETTINGS,
      environment: { ...SETTINGS.environment!, auth_enabled: false },
    });
    render(<SettingsPage />);
    await openTab(/environment/i);

    expect(await screen.findByText("disabled")).toBeInTheDocument();
    expect(screen.getByText(/answers every caller that can reach it/)).toBeInTheDocument();
  });
});

// ── Preferences ─────────────────────────────────────────────────────────────

/** The theme used to be one unlabelled button in the header that stepped
 *  dark → light → system, so the only way to find out what a click would do
 *  was to click it. It is three named choices here, and it is a *preference* —
 *  per browser, never sent to the server. */
describe("SettingsPage preferences tab", () => {
  beforeEach(() => {
    seedApi();
    document.documentElement.className = "";
  });

  it("says which theme is on", async () => {
    localStorage.setItem("spark-pulse-theme", "dark");
    render(<SettingsPage />);
    await openTab(/preferences/i);

    expect(await screen.findByRole("button", { name: "Dark" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "System" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("defaults to following the operating system", async () => {
    render(<SettingsPage />);
    await openTab(/preferences/i);

    expect(await screen.findByRole("button", { name: "System" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("applies the choice and remembers it", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await openTab(/preferences/i);

    await user.click(await screen.findByRole("button", { name: "Light" }));

    expect(localStorage.getItem("spark-pulse-theme")).toBe("light");
    expect(document.documentElement).toHaveClass("light");
    expect(document.documentElement).not.toHaveClass("dark");
  });

  /** It is the operator's preference, not the control plane's setting: a save
   *  button here would imply it reached the server, and a second browser would
   *  then be expected to follow it. */
  it("needs no save, and offers none", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await openTab(/preferences/i);

    await user.click(await screen.findByRole("button", { name: "Dark" }));

    expect(screen.queryByRole("button", { name: /save settings/i })).toBeNull();
    expect(updateSettings).not.toHaveBeenCalled();
  });
});

// ── Secrets ─────────────────────────────────────────────────────────────────

/** The HuggingFace token is written to a 0600 file and passed as `HF_TOKEN` to
 *  every deployment, so it is never echoed back: the page shows only the last
 *  four characters of whatever is stored. */
describe("SettingsPage secrets", () => {
  beforeEach(() => {
    seedApi();
    vi.mocked(saveSecrets).mockResolvedValue({ hf_token: "••••cdef" });
    vi.mocked(deleteSecret).mockResolvedValue(undefined);
  });

  it("will not save an empty token, and clears the field once one is stored", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await openTab(/secrets/i);
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
    await openTab(/secrets/i);

    const field = await screen.findByPlaceholderText("hf_…");
    expect(field).toHaveAttribute("type", "password");

    await user.click(screen.getByRole("button", { name: "Show token" }));
    expect(field).toHaveAttribute("type", "text");
  });

  it("shows only the tail of a stored token, and offers to clear it", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchSecrets).mockResolvedValue({ hf_token: "••••••••wxyz" });
    render(<SettingsPage />);
    await openTab(/secrets/i);

    expect(await screen.findByText("Active ···wxyz")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Enter new token to replace…")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear token" }));
    await waitFor(() => expect(deleteSecret).toHaveBeenCalledWith("hf_token"));
  });

  it("says why a token could not be stored", async () => {
    const user = userEvent.setup();
    vi.mocked(saveSecrets).mockRejectedValue(new Error("API 500: secrets file is read-only"));
    render(<SettingsPage />);
    await openTab(/secrets/i);

    await user.type(await screen.findByPlaceholderText("hf_…"), "hf_abcdef");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("API 500: secrets file is read-only")).toBeInTheDocument();
  });

  it("says why a token could not be cleared", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchSecrets).mockResolvedValue({ hf_token: "••••••••wxyz" });
    vi.mocked(deleteSecret).mockRejectedValue(new Error("API 500: secrets file is read-only"));
    render(<SettingsPage />);
    await openTab(/secrets/i);
    await screen.findByText("Active ···wxyz");

    await user.click(screen.getByRole("button", { name: "Clear token" }));

    expect(await screen.findByText("API 500: secrets file is read-only")).toBeInTheDocument();
  });
});

// ── Engines, and the states before settings arrive ──────────────────────────

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
    seedApi();
    vi.mocked(refreshEngines).mockResolvedValue({ refreshed: true, engines: 1, indexes: [] });
  });

  it("re-reads the engine indexes on request", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await openTab(/engines/i);
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
    await openTab(/engines/i);

    await user.click(await screen.findByRole("button", { name: /refresh/i }));

    expect(await screen.findByText("API 502: ghcr.io unreachable")).toBeInTheDocument();
  });

  it("lists the engines the registry holds, with the default marked", async () => {
    vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "vllm", engines: [ENGINE] });
    render(<SettingsPage />);
    await openTab(/engines/i);

    expect(await screen.findByText("vllm")).toBeInTheDocument();
    expect(screen.getByText("default")).toBeInTheDocument();
    expect(screen.getByText("v0.1.0")).toBeInTheDocument();
  });

  it("says the registry is empty rather than showing a blank panel", async () => {
    render(<SettingsPage />);
    await openTab(/engines/i);

    expect(await screen.findByText("No engines available.")).toBeInTheDocument();
  });

  it("edits the indexes as a list, one URL per line", async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await openTab(/engines/i);
    const indexes = await screen.findByLabelText("Engine indexes");
    expect(indexes).toHaveValue("https://acme.test/engines.json");

    fireEvent.change(indexes, { target: { value: "https://a.test/e.json\nhttps://b.test/e.json" } });
    await user.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          engine_indexes: ["https://a.test/e.json", "https://b.test/e.json"],
        }),
      ),
    );
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
