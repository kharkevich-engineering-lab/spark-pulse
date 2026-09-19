/** Settings, as tabs over one form.
 *
 * Every setting in the product is on this page now — including the three that
 * used to be panels on the pages they governed (the engine registry, the model
 * sources, the OCI auto-update schedule) and the MCP endpoint, which was a
 * route of its own. So the page is grouped by *when you go looking*, and tab
 * selection is part of nearly every test here.
 *
 * Three properties matter more than the layout. Every control has to write the
 * key it claims to — the Docker card used to render literals from the JSX and
 * post a `docker` block the API refused outright. The read-only tab has to
 * stay read-only: what it reports is exactly what a browser must not be able
 * to change. And **Save has to be on every tab**: it was rendered on three of
 * six, so an edit made on Containers and then read back from Environment had
 * no button to write it with.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import SettingsPage from "@/pages/SettingsPage";
import type { ModelSource, Settings } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchSettings: vi.fn(),
  updateSettings: vi.fn(),
  fetchSecrets: vi.fn(),
  saveSecrets: vi.fn(),
  deleteSecret: vi.fn(),
  fetchModelSources: vi.fn(),
  saveModelSources: vi.fn(),
  refreshEngines: vi.fn(),
  fetchOciRegistries: vi.fn(),
  addOciRegistry: vi.fn(),
  updateOciRegistry: vi.fn(),
  removeOciRegistry: vi.fn(),
  testOciRegistry: vi.fn(),
  fetchOciAutoUpdateSettings: vi.fn(),
  updateOciAutoUpdateSettings: vi.fn(),
  runOciAutoUpdate: vi.fn(),
}));

import {
  addOciRegistry,
  deleteSecret,
  fetchModelSources,
  fetchOciAutoUpdateSettings,
  fetchOciRegistries,
  fetchSecrets,
  fetchSettings,
  refreshEngines,
  runOciAutoUpdate,
  saveModelSources,
  saveSecrets,
  updateOciAutoUpdateSettings,
  updateSettings,
} from "@/lib/api";

const SETTINGS: Settings = {
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

const SOURCES: ModelSource[] = [
  { name: "huggingface", type: "hf_hub", endpoint: "https://huggingface.co", token_secret: "" },
];

/** The page lives under a router: the tab is deep-linked by hash, so
 *  `/settings#mcp` — and the redirect from the old `/mcp` route — land on the
 *  tab they name. */
function renderPage(entry = "/settings") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <SettingsPage />
    </MemoryRouter>,
  );
}

/** Open one tab. The page remembers the last one per browser, so every test
 *  that needs a particular tab has to say so rather than assume the default. */
async function openTab(name: RegExp | string) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("tab", { name }));
}

const saveButton = () => screen.getByRole("button", { name: /save settings/i });

function seedApi() {
  vi.clearAllMocks();
  localStorage.clear();
  vi.mocked(fetchSettings).mockResolvedValue(SETTINGS);
  vi.mocked(fetchSecrets).mockResolvedValue({ hf_token: "" });
  vi.mocked(updateSettings).mockResolvedValue(SETTINGS);
  vi.mocked(fetchModelSources).mockResolvedValue(SOURCES);
  vi.mocked(saveModelSources).mockResolvedValue(SOURCES);
  vi.mocked(fetchOciRegistries).mockResolvedValue([]);
  vi.mocked(fetchOciAutoUpdateSettings).mockResolvedValue({
    enabled: false,
    schedule: "0 3 * * *",
    overwrite_local: false,
  });
  vi.mocked(refreshEngines).mockResolvedValue({ refreshed: true, engines: 2, indexes: [] });
}

// ── The tabs themselves ─────────────────────────────────────────────────────

describe("SettingsPage tabs", () => {
  beforeEach(seedApi);

  it("opens on Deployment and shows only that tab's controls", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "Run defaults" })).toBeInTheDocument();
    // Container limits live on another tab; showing them all at once is the
    // thing the tabs exist to stop.
    expect(screen.queryByRole("heading", { name: "Container limits" })).toBeNull();
  });

  it("switches to the tab that was clicked", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Run defaults" });

    await openTab(/containers/i);

    expect(await screen.findByRole("heading", { name: "Container limits" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Run defaults" })).toBeNull();
  });

  /** A save re-reads the settings, which re-renders the page. Landing back on
   *  the first tab after every save would make editing anything else a chore. */
  it("comes back to the tab the operator was last on", async () => {
    const { unmount } = renderPage();
    await screen.findByRole("heading", { name: "Run defaults" });
    await openTab(/features/i);
    await screen.findByRole("heading", { name: "Optional features" });
    unmount();

    renderPage();

    expect(await screen.findByRole("heading", { name: "Optional features" })).toBeInTheDocument();
  });

  /** The hash is the addressable part: `/settings#mcp` is where the old `/mcp`
   *  route now points, and it has to beat whatever this browser last looked
   *  at. */
  it("opens the tab the hash names, over the remembered one", async () => {
    localStorage.setItem("spark-pulse:settings-tab", "containers");

    renderPage("/settings#mcp");

    expect(await screen.findByRole("heading", { name: "Server status" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Container limits" })).toBeNull();
  });

  it("ignores a hash that names no tab", async () => {
    renderPage("/settings#nothing-is-called-this");

    expect(await screen.findByRole("heading", { name: "Run defaults" })).toBeInTheDocument();
  });

  it("still renders when the browser refuses to remember anything", async () => {
    const boom = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("site data is blocked");
    });
    try {
      renderPage();
      expect(await screen.findByRole("heading", { name: "Run defaults" })).toBeInTheDocument();
    } finally {
      boom.mockRestore();
    }
  });

  it("still switches tabs when the browser refuses to remember the choice", async () => {
    const boom = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("site data is blocked");
    });
    try {
      renderPage();
      await screen.findByRole("heading", { name: "Run defaults" });
      await openTab(/containers/i);
      expect(await screen.findByRole("heading", { name: "Container limits" })).toBeInTheDocument();
    } finally {
      boom.mockRestore();
    }
  });
});

// ── Deployment ──────────────────────────────────────────────────────────────

describe("SettingsPage deployment tab", () => {
  beforeEach(seedApi);

  it("seeds every field from the settings the server sent", async () => {
    renderPage();

    expect(await screen.findByLabelText("Port range start")).toHaveValue(9000);
    expect(screen.getByLabelText("Port range end")).toHaveValue(9100);
    expect(screen.getByDisplayValue("600")).toBeInTheDocument();
    expect(screen.getByDisplayValue("300")).toBeInTheDocument();
  });


  it("writes each deployment field under the key it belongs to", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByLabelText("Port range start");

    // `fireEvent.change` rather than typing: jsdom's `<input type="number">`
    // has no usable text selection, so "select all and type" appends to the
    // old value. What is asserted here is the wiring, not the keystrokes.
    const replace = (el: HTMLElement, value: string) => fireEvent.change(el, { target: { value } });
    replace(screen.getByLabelText("Port range start"), "9500");
    replace(screen.getByLabelText("Port range end"), "9600");
    replace(screen.getByDisplayValue("600"), "900");
    replace(screen.getByDisplayValue("300"), "450");
    replace(screen.getByDisplayValue("7"), "30");

    await user.click(saveButton());

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
    renderPage();
    await screen.findByLabelText("Port range start");

    expect(screen.queryByDisplayValue("vllm-node")).toBeNull();
    expect(screen.queryByText(/Default container/i)).toBeNull();
    expect(screen.queryByText(/GPU memory utilization/i)).toBeNull();
  });
});

// ── Features ────────────────────────────────────────────────────────────────

describe("SettingsPage features tab", () => {
  beforeEach(seedApi);

  /** Benchmarking is not a timeout, which is the card it used to sit in. It
   *  decides whether a route and a nav entry exist at all. */
  it("carries the benchmarking switch into the saved form", async () => {
    const user = userEvent.setup();
    renderPage();
    await openTab(/features/i);

    await user.click(await screen.findByRole("switch", { name: "Benchmarking" }));
    await user.click(saveButton());

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ benchmarking_enabled: true }),
      ),
    );
  });

  it("reports the MCP endpoint rather than offering to switch it", async () => {
    renderPage();
    await openTab(/features/i);

    expect(await screen.findByText("/mcp")).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /MCP/i })).toBeNull();
  });

  /** The switch is real — it forces a `cluster_only` recipe on below two
   *  nodes — but it is a feature switch, not a page of its own. */
  it("keeps the cluster override, on this tab, still writing what recipes read", async () => {
    const user = userEvent.setup();
    renderPage();
    await openTab(/features/i);

    expect(screen.queryByRole("tab", { name: /cluster/i })).toBeNull();
    await user.click(await screen.findByRole("switch", { name: "Force cluster mode" }));
    await user.click(saveButton());

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ cluster_enabled: true }),
      ),
    );
  });

  it("says multi-node is experimental when the build says so", async () => {
    renderPage();
    await openTab(/features/i);

    expect(await screen.findByText(/marked experimental in this build/)).toBeInTheDocument();
  });

  it("carries the agent auto-update switch into the saved form", async () => {
    const user = userEvent.setup();
    renderPage();
    await openTab(/features/i);

    await user.click(await screen.findByRole("switch", { name: "Automatically update agents" }));
    await user.click(saveButton());

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ agent_auto_update: false }),
      ),
    );
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
    renderPage();
    await openTab(/containers/i);

    expect(await screen.findByDisplayValue("32")).toBeInTheDocument();
    expect(screen.getByDisplayValue("8")).toBeInTheDocument();
    expect(screen.getByDisplayValue("512")).toBeInTheDocument();
  });

  it("writes each container limit under the key it belongs to", async () => {
    const user = userEvent.setup();
    renderPage();
    await openTab(/containers/i);
    await screen.findByRole("heading", { name: "Container limits" });

    const replace = (el: HTMLElement, value: string) => fireEvent.change(el, { target: { value } });
    replace(screen.getByDisplayValue("110"), "96");
    replace(screen.getByDisplayValue("64"), "32");
    replace(screen.getByDisplayValue("4096"), "8192");
    replace(screen.getByDisplayValue("1048576"), "65536");
    await user.click(screen.getByRole("switch", { name: "Privileged mode" }));

    await user.click(saveButton());

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
    renderPage();
    await openTab(/containers/i);
    await screen.findByRole("heading", { name: "Container limits" });

    fireEvent.change(screen.getByDisplayValue("110"), { target: { value: "" } });
    await user.click(saveButton());

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ docker: expect.objectContaining({ memory_limit_gb: null }) }),
      ),
    );
  });

  it("edits the cache directories as a list, one per line", async () => {
    const user = userEvent.setup();
    renderPage();
    await openTab(/containers/i);
    const dirs = await screen.findByLabelText("Cache directories");
    expect(dirs).toHaveValue("~/.cache/vllm\n~/.triton");

    fireEvent.change(dirs, { target: { value: "~/.cache/vllm\n\n  ~/.cache/flashinfer  \n" } });
    await user.click(saveButton());

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
    renderPage();
    await openTab(/containers/i);

    await user.selectOptions(await screen.findByLabelText("Mod network access policy"), "deny");
    await user.click(saveButton());

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ mod: { network_policy: "deny" } }),
      ),
    );
  });

  /** The help under the shared-memory field was English in the JSX, so a
   *  French page showed one paragraph of English in the middle of the form. */
  it("translates the help that used to be hard-coded in the markup", async () => {
    renderPage();
    await openTab(/containers/i);

    expect(
      await screen.findByText(/Tensor-parallel workers pass tensors through it/),
    ).toBeInTheDocument();
  });

  /** These three sat in the Docker block and in this form with no reader
   *  anywhere in the backend — a page that looked like configuration and
   *  configured nothing. */
  it("no longer offers the cluster image, Ray port or GPU count", async () => {
    renderPage();
    await openTab(/containers/i);
    await screen.findByRole("heading", { name: "Container limits" });

    expect(screen.queryByPlaceholderText("eugr/spark-vllm-docker:latest")).toBeNull();
    expect(screen.queryByPlaceholderText("29501")).toBeNull();
    expect(screen.queryByText(/GPU count per node/i)).toBeNull();
    expect(screen.queryByRole("heading", { name: "Cluster Orchestration" })).toBeNull();
  });
});

// ── Library ─────────────────────────────────────────────────────────────────

/** Three panels on three pages became one tab. The engine fields are
 *  `/api/settings` keys and ride the page's Save; the model sources are their
 *  own endpoint and ride it too. */
describe("SettingsPage library tab", () => {
  beforeEach(seedApi);

  it("shows the engine registry the settings response described", async () => {
    renderPage("/settings#library");

    expect(await screen.findByDisplayValue("vllm")).toBeInTheDocument();
    expect(screen.getByDisplayValue("https://acme.test/engines.json")).toBeInTheDocument();
    expect(screen.getByDisplayValue("3600")).toBeInTheDocument();
  });

  it("writes the engine registry fields through the page's one Save", async () => {
    const user = userEvent.setup();
    renderPage("/settings#library");
    await screen.findByDisplayValue("vllm");

    fireEvent.change(screen.getByDisplayValue("https://acme.test/engines.json"), {
      target: { value: "https://acme.test/a.json\n\n  https://acme.test/b.json  " },
    });
    fireEvent.change(screen.getByDisplayValue("3600"), { target: { value: "60" } });
    await user.click(saveButton());

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          engine_indexes: ["https://acme.test/a.json", "https://acme.test/b.json"],
          engine_index_cache_ttl_seconds: 60,
        }),
      ),
    );
  });

  it("re-reads the engine index on request", async () => {
    const user = userEvent.setup();
    renderPage("/settings#library");
    await screen.findByDisplayValue("vllm");

    await user.click(screen.getByRole("button", { name: /refresh/i }));

    await waitFor(() => expect(refreshEngines).toHaveBeenCalled());
  });

  /** The sources are a different endpoint but the same form: a second Save
   *  button inside a form that already has one is how half an edit gets lost. */
  it("saves an edited model source with everything else", async () => {
    const user = userEvent.setup();
    renderPage("/settings#library");

    const name = await screen.findByLabelText("Source 1 name");
    fireEvent.change(name, { target: { value: "mirror" } });
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await user.click(saveButton());

    await waitFor(() =>
      expect(saveModelSources).toHaveBeenCalledWith([expect.objectContaining({ name: "mirror" })]),
    );
    // Nothing on `/api/settings` changed, so it is not written.
    expect(updateSettings).not.toHaveBeenCalled();
  });

  it("adds and removes a model source", async () => {
    const user = userEvent.setup();
    renderPage("/settings#library");
    await screen.findByLabelText("Source 1 name");

    await user.click(screen.getByRole("button", { name: /add source/i }));
    expect(await screen.findByLabelText("Source 2 name")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Remove source 2" }));
    await waitFor(() => expect(screen.queryByLabelText("Source 2 name")).toBeNull());
  });

  it("adds an OCI registry", async () => {
    const user = userEvent.setup();
    vi.mocked(addOciRegistry).mockResolvedValue({
      name: "acme",
      url: "ghcr.io/acme/recipes",
      enabled: true,
      default: false,
      connected: true,
    } as never);
    renderPage("/settings#library");

    await user.click(await screen.findByRole("button", { name: /add registry/i }));
    await user.type(screen.getByLabelText("Name"), "acme");
    await user.type(screen.getByLabelText("URL"), "ghcr.io/acme/recipes");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() =>
      expect(addOciRegistry).toHaveBeenCalledWith(
        expect.objectContaining({ name: "acme", url: "ghcr.io/acme/recipes" }),
      ),
    );
  });

  /** Saved when the field loses focus, not per keystroke: the original handler
   *  PUT the settings on every character, so a cron expression was saved once
   *  per prefix and every invalid one along the way was briefly the schedule. */
  it("saves the auto-update schedule on blur, not on every keystroke", async () => {
    const user = userEvent.setup();
    vi.mocked(updateOciAutoUpdateSettings).mockResolvedValue({
      enabled: false,
      schedule: "0 4 * * *",
      overwrite_local: false,
    });
    renderPage("/settings#library");

    const schedule = await screen.findByDisplayValue("0 3 * * *");
    await user.clear(schedule);
    await user.type(schedule, "0 4 * * *");
    expect(updateOciAutoUpdateSettings).not.toHaveBeenCalled();

    fireEvent.blur(schedule);
    await waitFor(() =>
      expect(updateOciAutoUpdateSettings).toHaveBeenCalledWith({ schedule: "0 4 * * *" }),
    );
  });

  it("runs the update sweep now and says what it did", async () => {
    const user = userEvent.setup();
    vi.mocked(runOciAutoUpdate).mockResolvedValue({ success: true, updated: 2 });
    renderPage("/settings#library");

    await user.click(await screen.findByRole("button", { name: /run now/i }));

    expect(await screen.findByText("2 recipe(s) updated.")).toBeInTheDocument();
  });

  it("says why the update sweep failed rather than claiming it ran", async () => {
    const user = userEvent.setup();
    vi.mocked(runOciAutoUpdate).mockResolvedValue({ success: false, error: "registry refused" });
    renderPage("/settings#library");

    await user.click(await screen.findByRole("button", { name: /run now/i }));

    expect(await screen.findByText("registry refused")).toBeInTheDocument();
  });
});

// ── MCP ─────────────────────────────────────────────────────────────────────

describe("SettingsPage mcp tab", () => {
  beforeEach(() => {
    seedApi();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          jsonrpc: "2.0",
          id: 1,
          result: { tools: [{ name: "list_recipes", description: "List all deployment recipes" }] },
        }),
      }),
    );
  });

  /** The list used to be a nine-entry array in the page. The server has
   *  offered more than twice that for a while, so the page documented a subset
   *  and nothing could notice. */
  it("lists the tools the endpoint itself reports", async () => {
    renderPage("/settings#mcp");

    expect(await screen.findByText("list_recipes")).toBeInTheDocument();
    expect(screen.getByText("List all deployment recipes")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Tools (1)" })).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(
      "http://localhost:8100/mcp",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("says the tool list could not be read rather than showing an empty one", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 503 }));
    renderPage("/settings#mcp");

    expect(
      await screen.findByText("Could not read the tool list from http://localhost:8100/mcp."),
    ).toBeInTheDocument();
  });

  /** jsdom serves this suite from port 3000, and the backend says 8100, so the
   *  page is looking at the dev server — where an operator who copies the
   *  browser's own origin gets a 404 from a server with no MCP on it. */
  it("points at the backend's port, not the browser's, and says why", async () => {
    renderPage("/settings#mcp");

    expect(await screen.findByText("http://localhost:8100/mcp")).toBeInTheDocument();
    expect(screen.getByText(/Vite dev server on port 3000/)).toBeInTheDocument();
  });

  it("says the origin is already the backend's when the backend serves this page", async () => {
    vi.mocked(fetchSettings).mockResolvedValue({ ...SETTINGS, webui_port: 3000 });
    renderPage("/settings#mcp");

    expect(await screen.findByText(/is the same origin/)).toBeInTheDocument();
    expect(screen.queryByText(/Vite dev server/)).toBeNull();
  });
});

// ── Saving ──────────────────────────────────────────────────────────────────

describe("SettingsPage saving", () => {
  beforeEach(seedApi);

  it("will not save a form nobody has changed", async () => {
    renderPage();
    await screen.findByLabelText("Port range start");

    expect(saveButton()).toBeDisabled();
  });

  /** It was rendered on three tabs of six. An edit made on Containers and then
   *  read back from Environment had no button to write it with, and the
   *  operator's only clue was that the button had gone. */
  it("offers Save on every tab", async () => {
    renderPage();
    await screen.findByLabelText("Port range start");

    for (const tab of [
      /containers/i,
      /features/i,
      /library/i,
      /mcp/i,
      /preferences/i,
      /secrets/i,
      /environment/i,
      /runs/i,
    ]) {
      await openTab(tab);
      expect(saveButton()).toBeVisible();
    }
  });

  it("saves the edited fields and confirms where they landed", async () => {
    const user = userEvent.setup();
    renderPage();
    const start = await screen.findByLabelText("Port range start");

    fireEvent.change(start, { target: { value: "9500" } });
    await waitFor(() => expect(saveButton()).toBeEnabled());
    await user.click(saveButton());

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ default_port_range_start: 9500 }),
      ),
    );
    expect(await screen.findByText(/Saved to/)).toBeInTheDocument();
  });

  /** One form across every tab. An edit made on Containers and then a switch
   *  to Deployment must not quietly drop the container change. */
  it("saves edits made on a tab the operator has since left", async () => {
    const user = userEvent.setup();
    renderPage();
    await openTab(/containers/i);
    fireEvent.change(await screen.findByDisplayValue("4096"), { target: { value: "8192" } });

    await openTab(/runs/i);
    fireEvent.change(await screen.findByLabelText("Port range end"), { target: { value: "9600" } });
    await user.click(saveButton());

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          default_port_range_end: 9600,
          docker: expect.objectContaining({ pids_limit: 8192 }),
        }),
      ),
    );
  });

  /** A failure is a line beside the button that caused it, not a modal that
   *  has to be dismissed before the form can be seen again. */
  it("says why a save was refused instead of claiming it worked", async () => {
    const user = userEvent.setup();
    vi.mocked(updateSettings).mockRejectedValue(new Error("API 403: settings are read-only"));
    renderPage();
    const start = await screen.findByLabelText("Port range start");

    fireEvent.change(start, { target: { value: "9500" } });
    await user.click(saveButton());

    expect(await screen.findByText("API 403: settings are read-only")).toBeInTheDocument();
    expect(screen.queryByText(/Saved to/)).toBeNull();
  });

  /** The "Health Monitoring" switch is gone. It set a piece of React state and
   *  nothing else: there was no monitor behind it, it called no endpoint, and
   *  it reset on every reload. */
  it("no longer offers a health-monitoring switch that does nothing", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "Run defaults" });

    expect(screen.queryByText("Health Monitoring:")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Health Monitoring" })).toBeNull();
  });
});

// ── Environment ─────────────────────────────────────────────────────────────

describe("SettingsPage environment tab", () => {
  beforeEach(seedApi);

  it("reports how the process is configured", async () => {
    renderPage();
    await openTab(/environment/i);

    expect(
      await screen.findByText("postgresql+psycopg://pulse:***@db.internal/spark_pulse"),
    ).toBeInTheDocument();
    expect(screen.getByText("https://pulse.acme.test")).toBeInTheDocument();
    expect(screen.getByText("http://localhost:8100")).toBeInTheDocument();
    expect(screen.getByText("enabled")).toBeInTheDocument();
    expect(screen.getByText("/mcp")).toBeInTheDocument();
  });

  /** Everything on this tab is exactly what a browser must not be able to
   *  change: turning authentication off from the settings page would make the
   *  settings page the way past every other check in the system. Save is on
   *  screen — it is on every tab — but there is nothing here for it to write. */
  it("offers nothing to edit", async () => {
    renderPage();
    await openTab(/environment/i);
    await screen.findByRole("heading", { name: "Access" });

    expect(screen.queryAllByRole("textbox")).toHaveLength(0);
    expect(screen.queryAllByRole("switch")).toHaveLength(0);
    expect(saveButton()).toBeDisabled();
  });

  /** A field the environment owns cannot be changed here, because the process
   *  would read the variable back over whatever was saved on the next start.
   *  `WEBUI_PORT` is the one field that can be owned this way. */
  it("marks the port when an environment variable owns it, and names it", async () => {
    vi.mocked(fetchSettings).mockResolvedValue({ ...SETTINGS, env_managed: ["webui_port"] });
    renderPage();
    await openTab(/environment/i);

    expect(await screen.findByText(/Controlled by the WEBUI_PORT/)).toBeInTheDocument();
  });

  it("says nothing about the environment owning a port it does not own", async () => {
    renderPage();
    await openTab(/environment/i);
    await screen.findByRole("heading", { name: "Runtime" });

    expect(screen.queryByText(/Controlled by the WEBUI_PORT/)).toBeNull();
  });

  /** How a worker node gets an engine image without pulling from the internet.
   *  It had no UI anywhere, so "why is this node still pulling" had no answer
   *  on any page. */
  it("says where worker nodes pull their images from", async () => {
    renderPage();
    await openTab(/environment/i);

    expect(await screen.findByText("proxy · 10.0.0.1:5000")).toBeInTheDocument();
    expect(screen.getByText(/Pull-through cache of https:\/\/ghcr.io/)).toBeInTheDocument();
  });

  it("says nothing about a registry it could not resolve", async () => {
    vi.mocked(fetchSettings).mockResolvedValue({
      ...SETTINGS,
      environment: { ...SETTINGS.environment!, image_registry: {} },
    });
    renderPage();
    await openTab(/environment/i);
    await screen.findByRole("heading", { name: "Runtime" });

    expect(screen.queryByText("Image registry")).toBeNull();
  });

  it("warns when this control plane answers without authentication", async () => {
    vi.mocked(fetchSettings).mockResolvedValue({
      ...SETTINGS,
      environment: { ...SETTINGS.environment!, auth_enabled: false },
    });
    renderPage();
    await openTab(/environment/i);

    expect(await screen.findByText("disabled")).toBeInTheDocument();
    expect(screen.getByText(/answers every caller that can reach it/)).toBeInTheDocument();
  });
});

// ── Preferences ─────────────────────────────────────────────────────────────

/** The theme and the language are *preferences* — per browser, never sent to
 *  the server. Save is on this tab like every other, and touching either of
 *  them leaves it disabled, because neither is a setting of the control
 *  plane's. */
describe("SettingsPage preferences tab", () => {
  beforeEach(() => {
    seedApi();
    document.documentElement.className = "";
  });

  it("says which theme is on", async () => {
    localStorage.setItem("spark-pulse-theme", "dark");
    renderPage();
    await openTab(/preferences/i);

    expect(await screen.findByRole("button", { name: "Dark" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "System" })).toHaveAttribute("aria-pressed", "false");
  });

  it("defaults to following the operating system", async () => {
    renderPage();
    await openTab(/preferences/i);

    expect(await screen.findByRole("button", { name: "System" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("applies the choice and remembers it", async () => {
    const user = userEvent.setup();
    renderPage();
    await openTab(/preferences/i);

    await user.click(await screen.findByRole("button", { name: "Light" }));

    expect(localStorage.getItem("spark-pulse-theme")).toBe("light");
    expect(document.documentElement).toHaveClass("light");
    expect(document.documentElement).not.toHaveClass("dark");
  });

  it("sends nothing to the server when a preference changes", async () => {
    const user = userEvent.setup();
    renderPage();
    await openTab(/preferences/i);

    await user.click(await screen.findByRole("button", { name: "Dark" }));

    expect(saveButton()).toBeDisabled();
    expect(updateSettings).not.toHaveBeenCalled();
  });

  /** Each language is offered in its own name — somebody looking for French
   *  looks for "Français", not for the English word for it. */
  it("offers each language in its own name, and marks the one in use", async () => {
    renderPage();
    await openTab(/preferences/i);

    expect(await screen.findByRole("button", { name: "English" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Français" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
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
    renderPage();
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
    renderPage();
    await openTab(/secrets/i);

    const field = await screen.findByPlaceholderText("hf_…");
    expect(field).toHaveAttribute("type", "password");

    await user.click(screen.getByRole("button", { name: "Show token" }));
    expect(field).toHaveAttribute("type", "text");
  });

  it("shows only the tail of a stored token, and offers to clear it", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchSecrets).mockResolvedValue({ hf_token: "••••••••wxyz" });
    renderPage();
    await openTab(/secrets/i);

    expect(await screen.findByText("Active ···wxyz")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Enter new token to replace…")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear token" }));
    await waitFor(() => expect(deleteSecret).toHaveBeenCalledWith("hf_token"));
  });

  it("says why a token could not be stored", async () => {
    const user = userEvent.setup();
    vi.mocked(saveSecrets).mockRejectedValue(new Error("API 500: secrets file is read-only"));
    renderPage();
    await openTab(/secrets/i);

    await user.type(await screen.findByPlaceholderText("hf_…"), "hf_abcdef");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("API 500: secrets file is read-only")).toBeInTheDocument();
  });

  it("says why a token could not be cleared", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchSecrets).mockResolvedValue({ hf_token: "••••••••wxyz" });
    vi.mocked(deleteSecret).mockRejectedValue(new Error("API 500: secrets file is read-only"));
    renderPage();
    await openTab(/secrets/i);
    await screen.findByText("Active ···wxyz");

    await user.click(screen.getByRole("button", { name: "Clear token" }));

    expect(await screen.findByText("API 500: secrets file is read-only")).toBeInTheDocument();
  });
});

// ── The states before settings arrive ───────────────────────────────────────

describe("SettingsPage loading", () => {
  beforeEach(seedApi);

  it("shows nothing but a spinner until the settings arrive", () => {
    vi.mocked(fetchSettings).mockReturnValue(new Promise(() => {}));
    renderPage();

    expect(screen.queryByRole("heading", { name: "Settings." })).toBeNull();
  });

  it("surfaces a failed load in place of the whole form", async () => {
    vi.mocked(fetchSettings).mockRejectedValue(new Error("API 500: settings.json is corrupt"));
    renderPage();

    expect(await screen.findByText("API 500: settings.json is corrupt")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Settings." })).toBeNull();
  });
});
