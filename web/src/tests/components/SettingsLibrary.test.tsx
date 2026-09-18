/** The Library tab: where engines, models and recipes come from.
 *
 * Three settings panels from three different pages are one tab here, and they
 * are saved two different ways on purpose. The engine fields and the model
 * sources are *form* values — the page's one Save writes them, which is what
 * the page's own tests cover. Everything in this file is the other half: the
 * registry actions, which take effect immediately because each is an action
 * against a registry rather than a value the form holds, and every one of
 * which can fail.
 *
 * A failure here is a line under the control that failed. The original was a
 * modal per action, which puts the message a long way from the thing that
 * produced it and makes "this registry did not answer" look like an outage.
 */

import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SettingsLibrary from "@/components/SettingsLibrary";
import type { ModelSource, OciRegistry } from "@/lib/types";

vi.mock("@/lib/api", () => ({
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
  fetchOciAutoUpdateSettings,
  fetchOciRegistries,
  refreshEngines,
  removeOciRegistry,
  runOciAutoUpdate,
  testOciRegistry,
  updateOciAutoUpdateSettings,
  updateOciRegistry,
} from "@/lib/api";

const REGISTRY: OciRegistry = {
  name: "acme",
  url: "ghcr.io/acme/recipes",
  enabled: true,
  default: false,
  connected: true,
  auth_type: "none",
} as OciRegistry;

const SOURCES: ModelSource[] = [
  { name: "huggingface", type: "hf_hub", endpoint: "https://huggingface.co", token_secret: "" },
];

/** The page owns the form and the sources; this stands in for it. */
function Harness({ sources = SOURCES }: { sources?: ModelSource[] | null }) {
  const [form, setForm] = useState<Record<string, unknown>>({
    default_engine: "vllm",
    engine_indexes: ["https://acme.test/engines.json"],
    engine_index_cache_ttl_seconds: 3600,
  });
  const [draft, setDraft] = useState<ModelSource[] | null>(sources);
  return (
    <SettingsLibrary form={form} setForm={setForm} sources={draft} setSources={setDraft} />
  );
}

function seed() {
  vi.clearAllMocks();
  vi.mocked(fetchOciRegistries).mockResolvedValue([REGISTRY]);
  vi.mocked(fetchOciAutoUpdateSettings).mockResolvedValue({
    enabled: false,
    schedule: "0 3 * * *",
    overwrite_local: false,
  });
  vi.mocked(refreshEngines).mockResolvedValue({ refreshed: true, engines: 2, indexes: [] });
  vi.mocked(updateOciRegistry).mockResolvedValue(REGISTRY);
  vi.mocked(testOciRegistry).mockResolvedValue({ ok: true });
  vi.mocked(removeOciRegistry).mockResolvedValue(undefined);
  vi.mocked(updateOciAutoUpdateSettings).mockResolvedValue({
    enabled: true,
    schedule: "0 3 * * *",
    overwrite_local: false,
  });
}

describe("SettingsLibrary engines", () => {
  beforeEach(seed);

  it("says why the index could not be re-read", async () => {
    const user = userEvent.setup();
    vi.mocked(refreshEngines).mockRejectedValue(new Error("index 404"));
    render(<Harness />);

    await user.click(await screen.findByRole("button", { name: /refresh/i }));

    expect(await screen.findByText("index 404")).toBeInTheDocument();
  });
});

describe("SettingsLibrary model sources", () => {
  beforeEach(seed);

  it("says so when no source is configured", async () => {
    render(<Harness sources={[]} />);

    expect(await screen.findByText("No sources configured.")).toBeInTheDocument();
  });

  /** A local path has no endpoint and no token, so those two fields are
   *  replaced by one — the path. */
  it("offers a path instead of an endpoint for a local source", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.selectOptions(await screen.findByLabelText("Source 1 type"), "local_path");

    expect(await screen.findByLabelText("Source 1 path")).toBeInTheDocument();
    expect(screen.queryByLabelText("Source 1 endpoint")).toBeNull();
  });

  it("edits the endpoint and the token of a hub source", async () => {
    render(<Harness />);

    const endpoint = await screen.findByLabelText("Source 1 endpoint");
    fireEvent.change(endpoint, { target: { value: "https://hf-mirror.test" } });
    expect(endpoint).toHaveValue("https://hf-mirror.test");

    const token = screen.getByLabelText("Source 1 token secret");
    fireEvent.change(token, { target: { value: "hf_mirror" } });
    expect(token).toHaveValue("hf_mirror");
  });
});

describe("SettingsLibrary registries", () => {
  beforeEach(seed);

  it("turns a registry off", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByText("acme");

    await user.click(screen.getByRole("button", { name: /disable/i }));

    await waitFor(() => expect(updateOciRegistry).toHaveBeenCalledWith("acme", { enabled: false }));
  });

  it("says why a registry could not be switched", async () => {
    const user = userEvent.setup();
    vi.mocked(updateOciRegistry).mockRejectedValue(new Error("registries.yaml is read-only"));
    render(<Harness />);
    await screen.findByText("acme");

    await user.click(screen.getByRole("button", { name: /disable/i }));

    expect(await screen.findByText("registries.yaml is read-only")).toBeInTheDocument();
  });

  /** A registry that answers "no" is not an error; it is fresh status. The
   *  operator still has to be told which one, and that it was asked. */
  it("names the registry that did not answer a connection test", async () => {
    const user = userEvent.setup();
    // The test button is offered only to a registry that is not already
    // connected — there is nothing to find out about one that is.
    vi.mocked(fetchOciRegistries).mockResolvedValue([{ ...REGISTRY, connected: false }]);
    vi.mocked(testOciRegistry).mockResolvedValue({ ok: false });
    render(<Harness />);
    await screen.findByText("acme");

    await user.click(screen.getByRole("button", { name: "Test connection" }));

    expect(await screen.findByText("acme did not answer.")).toBeInTheDocument();
  });

  it("says why a connection test could not be run at all", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchOciRegistries).mockResolvedValue([{ ...REGISTRY, connected: false }]);
    vi.mocked(testOciRegistry).mockRejectedValue(new Error("no network"));
    render(<Harness />);
    await screen.findByText("acme");

    await user.click(screen.getByRole("button", { name: "Test connection" }));

    expect(await screen.findByText("no network")).toBeInTheDocument();
  });

  /** Forgetting a registry is confirmed. Two of the three removal paths in the
   *  original asked nothing at all. */
  it("asks before forgetting a registry, and says what survives it", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByText("acme");

    await user.click(screen.getByRole("button", { name: "Remove registry" }));
    expect(
      await screen.findByText(/The collections already installed from it stay where they are/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(removeOciRegistry).toHaveBeenCalledWith("acme"));
  });

  it("says why a registry could not be forgotten", async () => {
    const user = userEvent.setup();
    vi.mocked(removeOciRegistry).mockRejectedValue(new Error("registry is a default"));
    render(<Harness />);
    await screen.findByText("acme");

    await user.click(screen.getByRole("button", { name: "Remove registry" }));
    await user.click(await screen.findByRole("button", { name: "Delete" }));

    expect(await screen.findByText("registry is a default")).toBeInTheDocument();
  });

  it("lets the dialog close without forgetting anything", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await screen.findByText("acme");

    await user.click(screen.getByRole("button", { name: "Remove registry" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("button", { name: "Delete" })).toBeNull());
    expect(removeOciRegistry).not.toHaveBeenCalled();
  });

  it("says why a registry could not be added", async () => {
    const user = userEvent.setup();
    vi.mocked(addOciRegistry).mockRejectedValue(new Error("name already taken"));
    render(<Harness />);

    await user.click(await screen.findByRole("button", { name: /add registry/i }));
    await user.type(screen.getByLabelText("Name"), "acme");
    await user.type(screen.getByLabelText("URL"), "ghcr.io/acme/recipes");
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByText("name already taken")).toBeInTheDocument();
  });

  it("abandons the add form without sending anything", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(await screen.findByRole("button", { name: /add registry/i }));
    await user.type(screen.getByLabelText("Name"), "acme");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByLabelText("Name")).toBeNull());
    expect(addOciRegistry).not.toHaveBeenCalled();
  });

  it("says so when there is no registry to show", async () => {
    vi.mocked(fetchOciRegistries).mockResolvedValue([]);
    render(<Harness />);

    expect(await screen.findByText("No registries configured")).toBeInTheDocument();
  });
});

describe("SettingsLibrary auto-update", () => {
  beforeEach(seed);

  it("turns the scheduled check on", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(await screen.findByRole("switch", { name: "Enable Auto-Update" }));

    await waitFor(() =>
      expect(updateOciAutoUpdateSettings).toHaveBeenCalledWith({ enabled: true }),
    );
  });

  it("says why the scheduled check could not be switched", async () => {
    const user = userEvent.setup();
    vi.mocked(updateOciAutoUpdateSettings).mockRejectedValue(new Error("no scheduler"));
    render(<Harness />);

    await user.click(await screen.findByRole("switch", { name: "Enable Auto-Update" }));

    expect(await screen.findByText("no scheduler")).toBeInTheDocument();
  });

  /** Blurring a field nobody typed in must not write anything: the original
   *  PUT the whole settings object on every keystroke. */
  it("writes nothing when the schedule was not edited", async () => {
    render(<Harness />);

    fireEvent.blur(await screen.findByDisplayValue("0 3 * * *"));

    await waitFor(() => expect(screen.getByDisplayValue("0 3 * * *")).toBeInTheDocument());
    expect(updateOciAutoUpdateSettings).not.toHaveBeenCalled();
  });

  it("says why a schedule could not be saved", async () => {
    vi.mocked(updateOciAutoUpdateSettings).mockRejectedValue(new Error("not a cron expression"));
    render(<Harness />);

    const schedule = await screen.findByDisplayValue("0 3 * * *");
    fireEvent.change(schedule, { target: { value: "every tuesday" } });
    fireEvent.blur(schedule);

    expect(await screen.findByText("not a cron expression")).toBeInTheDocument();
  });

  it("reports a sweep that was skipped, with the reason it was skipped", async () => {
    const user = userEvent.setup();
    vi.mocked(runOciAutoUpdate).mockResolvedValue({ skipped: true, reason: "already running" });
    render(<Harness />);

    await user.click(await screen.findByRole("button", { name: /run now/i }));

    expect(await screen.findByText("already running")).toBeInTheDocument();
  });

  it("reports a sweep that could not be started", async () => {
    const user = userEvent.setup();
    vi.mocked(runOciAutoUpdate).mockRejectedValue(new Error("no registries enabled"));
    render(<Harness />);

    await user.click(await screen.findByRole("button", { name: /run now/i }));

    expect(await screen.findByText("no registries enabled")).toBeInTheDocument();
  });

  it("says why the auto-update settings could not be read", async () => {
    vi.mocked(fetchOciAutoUpdateSettings).mockRejectedValue(new Error("oci.json is corrupt"));
    render(<Harness />);

    await waitFor(() => expect(screen.queryByRole("switch")).toBeNull());
    expect(screen.getByRole("heading", { name: "Auto-Update" })).toBeInTheDocument();
  });
});
