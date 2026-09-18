import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import LibraryPage from "@/pages/LibraryPage";
import { describePrecision, describeWhere, shortRevision } from "@/components/library/ModelsTab";
import { translate } from "@/lib/i18n";
import type { ModelEntry } from "@/lib/types";

/**
 * The shared setupTests EventSource stub records listeners but cannot deliver
 * frames. Swap in a capturing subclass so this file can push SSE payloads.
 */
class CapturingEventSource {
  static instances: CapturingEventSource[] = [];
  url: string;
  readyState = 1;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    CapturingEventSource.instances.push(this);
  }

  addEventListener() {}
  removeEventListener() {}
  close() {
    this.readyState = 2;
  }

  /** Deliver an unnamed `data:` frame, the shape /sse/models emits. */
  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

vi.mock("@/lib/api", () => ({
  // The Library shell's own reads: the header counts every byte on the disk,
  // and the tab bar counts what each tab holds.
  fetchCache: vi.fn(() => Promise.resolve({ entries: [] })),
  cleanCache: vi.fn(),
  fetchImages: vi.fn(() => Promise.resolve([])),
  fetchOciRegistries: vi.fn(() => Promise.resolve([])),
  fetchModels: vi.fn(),
  fetchModelSources: vi.fn(),
  fetchModelDownloads: vi.fn(),
  startModelDownload: vi.fn(),
  cancelModelDownload: vi.fn(),
  cancelScheduledDeploy: vi.fn(),
  fetchScheduledDeploys: vi.fn(),
  deleteModel: vi.fn(),
  // The cluster the model might also be sitting on. Inert by default: only
  // the delete tests care, and a page that asks for nodes must not depend on
  // there being any.
  fetchNodes: vi.fn(() => Promise.resolve([])),
  fetchModelPresence: vi.fn(),
  syncModelToNodes: vi.fn(),
}));

import {
  cancelModelDownload,
  cancelScheduledDeploy,
  fetchScheduledDeploys,
  deleteModel,
  fetchModelDownloads,
  fetchModelPresence,
  fetchModelSources,
  fetchModels,
  fetchNodes,
  startModelDownload,
  syncModelToNodes,
} from "@/lib/api";

const models: ModelEntry[] = [
  {
    id: "acme/plain-7b",
    source: "hf",
    source_type: "hf_cache",
    path: "/hub/snap",
    revision: "aaaabbbbccccdddd",
    revisions: [],
    size_bytes: 4 * 1024 ** 3,
    last_modified: null,
    config: { architectures: ["LlamaForCausalLM"], model_type: "llama", torch_dtype: "bfloat16", quantization: [], quantization_method: null },
    referenced_by: ["recipes/plain.yaml"],
  },
  {
    id: "acme/quant-70b",
    source: "hf",
    source_type: "hf_cache",
    path: "/hub/snap2",
    revision: "1111222233334444",
    revisions: [],
    size_bytes: 20 * 1024 ** 3,
    last_modified: null,
    config: { architectures: ["Qwen3MoeForCausalLM"], model_type: "qwen3_moe", torch_dtype: "float16", quantization: ["bits"], quantization_method: "awq" },
    referenced_by: [],
  },
];

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={["/models"]}>
      <LibraryPage />
    </MemoryRouter>,
  );

const t = (key: string, vars?: Record<string, string | number>) => translate("en", key, vars);

describe("the models tab's helpers", () => {
  it("shortens a revision", () => {
    expect(shortRevision("aaaabbbbccccdddd")).toBe("aaaabbbbcc");
    expect(shortRevision(null)).toBe("—");
  });

  it("prefers the quantization method over the dtype", () => {
    expect(describePrecision(models[1])).toBe("awq");
    expect(describePrecision(models[0])).toBe("bfloat16");
    expect(describePrecision({ ...models[0], config: null })).toBe("—");
  });
});

describe("Library — models", () => {
  beforeEach(() => {
    CapturingEventSource.instances = [];
    vi.stubGlobal("EventSource", CapturingEventSource);
    vi.mocked(fetchModels).mockResolvedValue(models);
    vi.mocked(fetchModelSources).mockResolvedValue([
      { name: "hf", type: "hf_hub", endpoint: "https://huggingface.co", token_secret: "hf_token" },
      { name: "mirror", type: "hf_hub", endpoint: "http://mirror.local", token_secret: "" },
    ]);
    vi.mocked(fetchModelDownloads).mockResolvedValue([]);
    vi.mocked(fetchScheduledDeploys).mockResolvedValue([]);
  });

  it("renders the catalogue table", async () => {
    renderPage();
    expect(await screen.findByText("acme/plain-7b")).toBeInTheDocument();
    expect(screen.getByText("acme/quant-70b")).toBeInTheDocument();
    expect(screen.getByText("awq")).toBeInTheDocument();
    // The count of recipes referencing a model went; the column that replaced
    // it says the thing an operator reclaiming disk actually needs.
    expect(screen.getByRole("columnheader", { name: "Where" })).toBeInTheDocument();
    // Everything on the disk, and how much of it is cache — the one line the
    // four pages this replaced could not say between them.
    expect(
      await screen.findByText(t("library.onDisk", { total: "24.0 GB", cache: "0 B" })),
    ).toBeInTheDocument();
  });

  /** One machine holds every model it has, by definition — so the column says
   *  so rather than leaving the operator to wonder whether it was asked. */
  it("says the model is on the only node there is", async () => {
    renderPage();
    await screen.findByText("acme/plain-7b");

    expect(screen.getAllByText("1 of 1 nodes")).toHaveLength(2);
  });

  it("lists hub sources in the download form", async () => {
    renderPage();
    await screen.findByText("acme/plain-7b");
    const select = screen.getByLabelText("Source") as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual(["", "hf", "mirror"]);
  });

  it("starts a download from the form", async () => {
    vi.mocked(startModelDownload).mockResolvedValue({
      id: "job1", model: "acme/new", source: "mirror", revision: "v1", allow_patterns: null,
      status: "queued", bytes_done: 0, bytes_total: 100, current_file: null, path: null,
      error: null, created_at: "", started_at: null, finished_at: null,
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("acme/plain-7b");

    await user.type(screen.getByLabelText("Model id"), "acme/new");
    await user.selectOptions(screen.getByLabelText("Source"), "mirror");
    await user.type(screen.getByLabelText("Revision"), "v1");
    await user.click(screen.getByRole("button", { name: /^Download$/ }));

    await waitFor(() =>
      expect(startModelDownload).toHaveBeenCalledWith({ model: "acme/new", source: "mirror", revision: "v1" }),
    );
    expect(await screen.findByTestId("job-job1")).toBeInTheDocument();
  });

  it("updates progress bars from the SSE stream", async () => {
    renderPage();
    await screen.findByText("acme/plain-7b");

    const source = CapturingEventSource.instances.find((s) => s.url === "/sse/models");
    expect(source).toBeDefined();

    act(() => {
      source!.emit({
        type: "model.download.progress",
        resource: "job9",
        resource_type: "model",
        metadata: {
          id: "job9", model: "acme/streamed", source: "hf", revision: null, allow_patterns: null,
          status: "running", bytes_done: 50, bytes_total: 100, current_file: "shard-1.safetensors",
          path: null, error: null, created_at: "", started_at: null, finished_at: null,
        },
      });
    });

    const bar = await screen.findByRole("progressbar", { name: /acme\/streamed progress/ });
    expect(bar).toHaveAttribute("aria-valuenow", "50");
    expect(screen.getByText(/shard-1.safetensors/)).toBeInTheDocument();
  });

  it("cancels an active download", async () => {
    vi.mocked(fetchModelDownloads).mockResolvedValue([
      {
        id: "job5", model: "acme/running", source: "hf", revision: null, allow_patterns: null,
        status: "running", bytes_done: 10, bytes_total: 100, current_file: null, path: null,
        error: null, created_at: "", started_at: null, finished_at: null,
      },
    ]);
    vi.mocked(cancelModelDownload).mockResolvedValue({} as never);
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Cancel download of acme/running"));
    expect(cancelModelDownload).toHaveBeenCalledWith("job5");
  });

  it("asks for confirmation before deleting", async () => {
    vi.mocked(deleteModel).mockResolvedValue({ deleted: "acme/plain-7b", path: "/hub", freed_bytes: 1 });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Remove acme/plain-7b"));
    expect(screen.getByText(/Delete the cached snapshot/)).toBeInTheDocument();
    expect(deleteModel).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(deleteModel).toHaveBeenCalledWith("acme/plain-7b", []));
  });

  it("shows an empty state when nothing is cached", async () => {
    vi.mocked(fetchModels).mockResolvedValue([]);
    renderPage();
    expect(await screen.findByText("No models cached yet.")).toBeInTheDocument();
  });

  it("surfaces a failed catalogue read instead of an empty page", async () => {
    vi.mocked(fetchModels).mockRejectedValue(new Error("API 500: hub cache unreadable"));
    renderPage();
    expect(await screen.findByText("API 500: hub cache unreadable")).toBeInTheDocument();
  });

  /** Every one of these ends in the same modal, and each carries a different
   *  title, because "Delete failed" and "Download failed" are different
   *  problems with different next steps. */
  it("names which operation failed, and why", async () => {
    const user = userEvent.setup();
    vi.mocked(startModelDownload).mockRejectedValue(new Error("no such repo on the hub"));
    renderPage();
    await screen.findByText("acme/plain-7b");

    await user.type(screen.getByLabelText("Model id"), "acme/missing");
    await user.click(screen.getByRole("button", { name: /^Download$/ }));

    expect(await screen.findByRole("heading", { name: "Download failed" })).toBeInTheDocument();
    expect(screen.getByText("no such repo on the hub")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "OK" }));
    await waitFor(() => expect(screen.queryByText("no such repo on the hub")).toBeNull());
  });

  it("says why a delete failed rather than leaving the row in place unexplained", async () => {
    const user = userEvent.setup();
    vi.mocked(deleteModel).mockRejectedValue(new Error("snapshot is in use"));
    renderPage();

    await user.click(await screen.findByLabelText("Remove acme/plain-7b"));
    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(await screen.findByRole("heading", { name: "Delete failed" })).toBeInTheDocument();
    expect(screen.getByText("snapshot is in use")).toBeInTheDocument();
  });

  it("says why a cancel failed rather than leaving the job looking cancelled", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchModelDownloads).mockResolvedValue([
      {
        id: "job5", model: "acme/running", source: "hf", revision: null, allow_patterns: null,
        status: "running", bytes_done: 10, bytes_total: 100, current_file: null, path: null,
        error: null, created_at: "", started_at: null, finished_at: null,
      },
    ]);
    vi.mocked(cancelModelDownload).mockRejectedValue(new Error("job already finished"));
    renderPage();

    await user.click(await screen.findByLabelText("Cancel download of acme/running"));

    expect(await screen.findByRole("heading", { name: "Cancel failed" })).toBeInTheDocument();
  });

  it("refuses to start a download with no model id", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("acme/plain-7b");
    const before = vi.mocked(startModelDownload).mock.calls.length;

    await user.click(screen.getByRole("button", { name: /^Download$/ }));

    expect(vi.mocked(startModelDownload).mock.calls.length).toBe(before);
  });

  it("re-reads the catalogue when a download reports itself finished", async () => {
    renderPage();
    await screen.findByText("acme/plain-7b");
    const before = vi.mocked(fetchModels).mock.calls.length;
    const source = CapturingEventSource.instances.find((s) => s.url === "/sse/models")!;

    act(() => {
      source.emit({
        type: "model.download.completed",
        resource: "job9",
        resource_type: "model",
        metadata: {
          id: "job9", model: "acme/streamed", source: "hf", revision: null, allow_patterns: null,
          status: "completed", bytes_done: 100, bytes_total: 100, current_file: null,
          path: "/hub/acme", error: null, created_at: "", started_at: null, finished_at: null,
        },
      });
    });

    await waitFor(() =>
      expect(vi.mocked(fetchModels).mock.calls.length).toBeGreaterThan(before),
    );
  });

  it("ignores stream frames about something other than a model", async () => {
    renderPage();
    await screen.findByText("acme/plain-7b");
    const source = CapturingEventSource.instances.find((s) => s.url === "/sse/models")!;

    act(() => {
      source.emit({
        type: "image.pull.progress",
        resource_type: "image",
        metadata: { id: "job-other", ref: "ghcr.io/acme/x" },
      });
    });
    // And a model frame with no job in it at all.
    act(() => source.emit({ type: "model.download.progress", resource_type: "model", metadata: {} }));

    expect(screen.queryByTestId("job-job-other")).toBeNull();
  });
});

// ── Deployments waiting on a download ───────────────────────────────────────

/**
 * A download started from the deploy page carries a deployment behind it, and
 * this is the only page the operator watches it on. A progress bar with no
 * purpose attached is what this exists to stop showing: without it there is no
 * way to know a deployment is queued on these bytes, and no way to call it off
 * short of cancelling the download and guessing what that did.
 */
describe("Library — deployments waiting on a download", () => {
  const runningJob = {
    id: "job9", model: "acme/big", source: "hf", revision: null, allow_patterns: null,
    status: "running", bytes_done: 10, bytes_total: 100, current_file: null, path: null,
    error: null, created_at: "", started_at: null, finished_at: null,
  };

  const schedule = (over: Record<string, unknown> = {}) => ({
    id: "s-1", model: "acme/big", download_job_id: "job9", status: "waiting",
    name: "big-serve", recipe_id: "r1", request: {}, deployment_id: "", error: "",
    created_at: "2026-01-01T00:00:00Z", finished_at: "", ...over,
  });

  beforeEach(() => {
    CapturingEventSource.instances = [];
    vi.stubGlobal("EventSource", CapturingEventSource);
    vi.mocked(fetchModels).mockResolvedValue(models);
    vi.mocked(fetchModelSources).mockResolvedValue([]);
    vi.mocked(fetchModelDownloads).mockResolvedValue([runningJob] as never);
  });

  it("says what a download is for", async () => {
    vi.mocked(fetchScheduledDeploys).mockResolvedValue([schedule()] as never);
    renderPage();

    expect(await screen.findByTestId("scheduled-s-1")).toHaveTextContent(
      /Scheduled to deploy.*big-serve.*when this finishes/,
    );
  });

  it("cancels the scheduled deploy in flight", async () => {
    vi.mocked(fetchScheduledDeploys).mockResolvedValue([schedule()] as never);
    vi.mocked(cancelScheduledDeploy).mockResolvedValue({} as never);
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Cancel the scheduled deploy of big-serve"));

    expect(cancelScheduledDeploy).toHaveBeenCalledWith("s-1");
  });

  it("does not offer to cancel one that is already deploying", async () => {
    vi.mocked(fetchScheduledDeploys).mockResolvedValue([schedule({ status: "deploying" })] as never);
    renderPage();

    expect(await screen.findByTestId("scheduled-s-1")).toHaveTextContent(/Deploying.*big-serve/);
    expect(screen.queryByLabelText("Cancel the scheduled deploy of big-serve")).not.toBeInTheDocument();
  });

  /** The case an operator would otherwise wait on for ever: the bytes landed,
   *  the progress bar reads 100%, and the deployment quietly did not start. */
  it("shows a deploy that failed after its download succeeded", async () => {
    vi.mocked(fetchScheduledDeploys).mockResolvedValue([
      schedule({ status: "failed", error: "port 8000 is already bound" }),
    ] as never);
    renderPage();

    expect(await screen.findByTestId("scheduled-s-1")).toHaveTextContent(
      /could not be deployed: port 8000 is already bound/,
    );
  });

  it("hides one the operator called off themselves", async () => {
    vi.mocked(fetchScheduledDeploys).mockResolvedValue([schedule({ status: "cancelled" })] as never);
    renderPage();

    await screen.findByText("acme/big");
    expect(screen.queryByTestId("scheduled-s-1")).not.toBeInTheDocument();
  });

  it("re-reads the schedules when a download reaches a terminal state", async () => {
    vi.mocked(fetchScheduledDeploys).mockResolvedValue([schedule()] as never);
    renderPage();
    await screen.findByTestId("scheduled-s-1");
    vi.mocked(fetchScheduledDeploys).mockClear();

    await act(async () => {
      CapturingEventSource.instances[0].emit({
        type: "model.download.completed",
        resource_type: "model",
        metadata: { ...runningJob, status: "completed", bytes_done: 100 },
      });
    });

    await waitFor(() => expect(fetchScheduledDeploys).toHaveBeenCalled());
  });
});

/** Deleting a model that is on more than one machine.
 *
 * A 26 GB model replicated to four Sparks is on four disks. The dialog used to
 * delete it from this one and the page then said it was gone, which is how a
 * cluster fills with copies nobody can see. */
describe("Library — deleting a model across nodes", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchModels).mockResolvedValue(models);
    vi.mocked(fetchModelSources).mockResolvedValue([{ name: "hf", type: "hf_hub" }]);
    vi.mocked(fetchModelDownloads).mockResolvedValue([]);
    vi.mocked(fetchScheduledDeploys).mockResolvedValue([]);
    vi.mocked(fetchNodes).mockResolvedValue([
      { is_control_plane: true, address: "192.168.1.100" },
      { is_control_plane: false, address: "10.0.0.11" },
      { is_control_plane: false, address: "10.0.0.12" },
    ] as never);
    vi.mocked(fetchModelPresence).mockResolvedValue({
      model: "acme/plain-7b",
      local: true,
      nodes: [
        { node: "10.0.0.11", present: true, error: null },
        { node: "10.0.0.12", present: false, error: null },
      ],
    });
    vi.mocked(deleteModel).mockResolvedValue({
      deleted: "acme/plain-7b",
      path: "/hub",
      freed_bytes: 26_000_000_000,
      nodes: [
        { node: "", removed: true, freed_bytes: 13_000_000_000, error: null },
        { node: "10.0.0.11", removed: true, freed_bytes: 13_000_000_000, error: null },
      ],
    });
  });

  it("offers the other machines, and preselects the ones that have a copy", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Remove acme/plain-7b"));

    const dialog = within(screen.getByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("10.0.0.11")).toBeInTheDocument());
    // Checked where the model is; offered but unchecked where it is not.
    expect(dialog.getByRole("checkbox", { name: /10\.0\.0\.11/ })).toBeChecked();
    expect(dialog.getByRole("checkbox", { name: /10\.0\.0\.12/ })).not.toBeChecked();
    expect(dialog.getByText("not there")).toBeInTheDocument();
  });

  it("deletes from the nodes that were ticked", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Remove acme/plain-7b"));
    const dialog = within(screen.getByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("10.0.0.11")).toBeInTheDocument());
    await user.click(dialog.getByRole("button", { name: /Delete everywhere selected/ }));

    await waitFor(() =>
      expect(deleteModel).toHaveBeenCalledWith("acme/plain-7b", ["10.0.0.11"]),
    );
  });

  it("keeps a node the operator ticked when presence answers late", async () => {
    // The answer arrives after a click; preselecting over it would undo the
    // operator's own choice.
    let settle: (value: never) => void = () => {};
    vi.mocked(fetchModelPresence).mockReturnValue(
      new Promise((resolve) => {
        settle = resolve as never;
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Remove acme/plain-7b"));
    const dialog = within(screen.getByRole("dialog"));
    await user.click(dialog.getByRole("checkbox", { name: /10\.0\.0\.12/ }));
    await act(async () => {
      settle({
        model: "acme/plain-7b",
        local: true,
        nodes: [{ node: "10.0.0.11", present: true, error: null }],
      } as never);
    });

    expect(dialog.getByRole("checkbox", { name: /10\.0\.0\.12/ })).toBeChecked();
  });

  it("reports what was actually freed, and on how many machines", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Remove acme/plain-7b"));
    const dialog = within(screen.getByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("10.0.0.11")).toBeInTheDocument());
    await user.click(dialog.getByRole("button", { name: /Delete everywhere selected/ }));

    expect(await screen.findByText(/Removed from 2 node/)).toBeInTheDocument();
  });

  it("names the node that refused rather than claiming the model is gone", async () => {
    vi.mocked(deleteModel).mockResolvedValue({
      deleted: "acme/plain-7b",
      path: "/hub",
      freed_bytes: 13_000_000_000,
      nodes: [
        { node: "", removed: true, freed_bytes: 13_000_000_000, error: null },
        { node: "10.0.0.11", removed: false, freed_bytes: 0, error: "no enrolled agent" },
      ],
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Remove acme/plain-7b"));
    const dialog = within(screen.getByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("10.0.0.11")).toBeInTheDocument());
    await user.click(dialog.getByRole("button", { name: /Delete everywhere selected/ }));

    expect(await screen.findByRole("heading", { name: "Delete failed" })).toBeInTheDocument();
    expect(screen.getByText(/10\.0\.0\.11: no enrolled agent/)).toBeInTheDocument();
  });

  it("asks nothing of a single-machine install", async () => {
    vi.mocked(fetchNodes).mockResolvedValue([
      { is_control_plane: true, address: "192.168.1.100" },
    ] as never);
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Remove acme/plain-7b"));

    expect(fetchModelPresence).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(deleteModel).toHaveBeenCalledWith("acme/plain-7b", []));
  });
});

/** Replicating a model to the nodes that do not yet have it.
 *
 * The inverse of delete: a node presence already reports as holding the
 * model has nothing to gain from a transfer, so the dialog preselects the
 * nodes that are missing it rather than the ones that already hold it.
 */
describe("Library — replicating a model across nodes", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchModels).mockResolvedValue(models);
    vi.mocked(fetchModelSources).mockResolvedValue([{ name: "hf", type: "hf_hub" }]);
    vi.mocked(fetchModelDownloads).mockResolvedValue([]);
    vi.mocked(fetchScheduledDeploys).mockResolvedValue([]);
    vi.mocked(fetchNodes).mockResolvedValue([
      { is_control_plane: true, address: "192.168.1.100" },
      { is_control_plane: false, address: "10.0.0.11" },
      { is_control_plane: false, address: "10.0.0.12" },
    ] as never);
    vi.mocked(fetchModelPresence).mockResolvedValue({
      model: "acme/plain-7b",
      local: true,
      nodes: [
        { node: "10.0.0.11", present: true, error: null },
        { node: "10.0.0.12", present: false, error: null },
      ],
    });
  });

  it("offers the other machines, and preselects the ones missing a copy", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Replicate acme/plain-7b to other nodes"));

    const dialog = within(screen.getByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("10.0.0.11")).toBeInTheDocument());
    // Unchecked where it is already there; checked where it is missing.
    expect(dialog.getByRole("checkbox", { name: /10\.0\.0\.11/ })).not.toBeChecked();
    expect(dialog.getByRole("checkbox", { name: /10\.0\.0\.12/ })).toBeChecked();
    expect(dialog.getByText("already there")).toBeInTheDocument();
  });

  it("replicates to the nodes ticked, carrying the force flag", async () => {
    vi.mocked(syncModelToNodes).mockResolvedValue({
      model: "acme/plain-7b",
      path: "/hub",
      ok: true,
      results: [{ node: "10.0.0.12", ok: true, error: null, duration_s: 1.2, skipped: false }],
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Replicate acme/plain-7b to other nodes"));
    const dialog = within(screen.getByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("10.0.0.11")).toBeInTheDocument());
    await user.click(dialog.getByLabelText("Re-transfer even if already present"));
    await user.click(dialog.getByRole("button", { name: "Replicate" }));

    await waitFor(() =>
      expect(syncModelToNodes).toHaveBeenCalledWith(
        "acme/plain-7b",
        ["10.0.0.12"],
        undefined,
        { force: true },
      ),
    );
    expect(await dialog.findByText("verified")).toBeInTheDocument();
  });

  it("shows a node the server skipped because it already verified", async () => {
    vi.mocked(syncModelToNodes).mockResolvedValue({
      model: "acme/plain-7b",
      path: "/hub",
      ok: true,
      results: [{ node: "10.0.0.12", ok: true, error: null, duration_s: 0.1, skipped: true }],
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Replicate acme/plain-7b to other nodes"));
    const dialog = within(screen.getByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("10.0.0.11")).toBeInTheDocument());
    await user.click(dialog.getByRole("button", { name: "Replicate" }));

    expect(await dialog.findByText("already verified — skipped")).toBeInTheDocument();
  });

  it("names the node that failed rather than claiming success", async () => {
    vi.mocked(syncModelToNodes).mockResolvedValue({
      model: "acme/plain-7b",
      path: "/hub",
      ok: false,
      results: [{ node: "10.0.0.12", ok: false, error: "no space left on device", duration_s: 2, skipped: false }],
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Replicate acme/plain-7b to other nodes"));
    const dialog = within(screen.getByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("10.0.0.11")).toBeInTheDocument());
    await user.click(dialog.getByRole("button", { name: "Replicate" }));

    expect(await dialog.findByText("no space left on device")).toBeInTheDocument();
  });

  it("surfaces a transport-level replication error inline", async () => {
    vi.mocked(syncModelToNodes).mockRejectedValue(new Error("agent unreachable"));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Replicate acme/plain-7b to other nodes"));
    const dialog = within(screen.getByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("10.0.0.11")).toBeInTheDocument());
    await user.click(dialog.getByRole("button", { name: "Replicate" }));

    expect(await dialog.findByText("agent unreachable")).toBeInTheDocument();
  });

  it("re-reads presence after replicating so the checkboxes reflect the transfer", async () => {
    vi.mocked(syncModelToNodes).mockResolvedValue({
      model: "acme/plain-7b",
      path: "/hub",
      ok: true,
      results: [{ node: "10.0.0.12", ok: true, error: null, duration_s: 1, skipped: false }],
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Replicate acme/plain-7b to other nodes"));
    const dialog = within(screen.getByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("10.0.0.11")).toBeInTheDocument());
    const before = vi.mocked(fetchModelPresence).mock.calls.length;

    await user.click(dialog.getByRole("button", { name: "Replicate" }));

    await waitFor(() =>
      expect(vi.mocked(fetchModelPresence).mock.calls.length).toBeGreaterThan(before),
    );
  });

  it("does not offer replication on a single-machine install", async () => {
    vi.mocked(fetchNodes).mockResolvedValue([
      { is_control_plane: true, address: "192.168.1.100" },
    ] as never);
    renderPage();

    await screen.findByText("acme/plain-7b");
    expect(screen.queryByLabelText("Replicate acme/plain-7b to other nodes")).not.toBeInTheDocument();
  });

  it("lets the operator override the preselection by hand", async () => {
    vi.mocked(syncModelToNodes).mockResolvedValue({
      model: "acme/plain-7b",
      path: "/hub",
      ok: true,
      results: [],
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Replicate acme/plain-7b to other nodes"));
    const dialog = within(screen.getByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("10.0.0.11")).toBeInTheDocument());

    // Untick the preselected node and tick the one that already has it.
    await user.click(dialog.getByRole("checkbox", { name: /10\.0\.0\.12/ }));
    await user.click(dialog.getByRole("checkbox", { name: /10\.0\.0\.11/ }));
    await user.click(dialog.getByRole("button", { name: "Replicate" }));

    await waitFor(() =>
      expect(syncModelToNodes).toHaveBeenCalledWith(
        "acme/plain-7b",
        ["10.0.0.11"],
        undefined,
        { force: false },
      ),
    );
  });

  it("keeps a node the operator ticked when presence answers late", async () => {
    let settle: (value: never) => void = () => {};
    vi.mocked(fetchModelPresence).mockReturnValue(
      new Promise((resolve) => {
        settle = resolve as never;
      }),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Replicate acme/plain-7b to other nodes"));
    const dialog = within(screen.getByRole("dialog"));
    await user.click(dialog.getByRole("checkbox", { name: /10\.0\.0\.11/ }));
    await act(async () => {
      settle({
        model: "acme/plain-7b",
        local: true,
        nodes: [{ node: "10.0.0.12", present: false, error: null }],
      } as never);
    });

    // The operator's own tick on 10.0.0.11 survives the late-arriving answer.
    expect(dialog.getByRole("checkbox", { name: /10\.0\.0\.11/ })).toBeChecked();
  });

  it("leaves presence unresolved when a node cannot be asked", async () => {
    vi.mocked(fetchModelPresence).mockRejectedValue(new Error("agent unreachable"));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText("Replicate acme/plain-7b to other nodes"));
    const dialog = within(screen.getByRole("dialog"));

    await waitFor(() => expect(dialog.queryByText("Loading…")).not.toBeInTheDocument());
    expect(dialog.queryByText("already there")).not.toBeInTheDocument();
  });
});

/** The "Where" column.
 *
 * A model on one of four machines and a model on all four are different
 * answers to "can I delete this", and the page used to give neither: the
 * dialog knew, once it was open, and the row said nothing. `describeWhere` is
 * the verdict, and it is deliberately unwilling to read silence as absence.
 */
describe("Library — where a model is", () => {
  const entry = (node: string, state?: string, error?: string) => ({ node, state, error });

  it("says every node holds it", () => {
    expect(describeWhere([entry("a", "verified"), entry("b", "verified")], t)).toMatchObject({
      state: "ok",
      label: "2 of 2 nodes",
    });
  });

  it("names the single holder rather than counting to one", () => {
    expect(describeWhere([entry("gx10-ced2", "verified"), entry("b", "absent")], t)).toMatchObject({
      state: "warn",
      label: "gx10-ced2 only",
    });
  });

  it("counts the holders when there is more than one but not all", () => {
    const answer = describeWhere(
      [entry("a", "verified"), entry("b", "verified"), entry("c", "absent")],
      t,
    );
    expect(answer).toMatchObject({ state: "warn", label: "2 of 3 nodes" });
  });

  /** Half a snapshot is worse than none: it deploys and then fails on a shard
   *  nobody notices is missing, so it is the bad state, not a warning. */
  it("calls out a partial copy, and which machine has it", () => {
    expect(describeWhere([entry("a", "verified"), entry("b", "partial")], t)).toMatchObject({
      state: "bad",
      label: "partial on b",
    });
  });

  it("says nothing was checked when nothing answered, and why", () => {
    const answer = describeWhere([entry("a", undefined, "no enrolled agent")], t);
    expect(answer.state).toBe("unknown");
    expect(answer.label).toBe("not checked");
    expect(answer.title).toContain("no enrolled agent");
  });

  it("keeps a node that could not be asked out of the count, on hover", () => {
    const answer = describeWhere(
      [entry("a", "verified"), entry("b", undefined, "connection reset")],
      t,
    );
    expect(answer).toMatchObject({ state: "warn", label: "a only" });
    expect(answer.title).toContain("connection reset");
  });

  it("has no verdict before anything has been asked", () => {
    expect(describeWhere(null, t).label).toBe("not checked");
    expect(describeWhere([], t).label).toBe("not checked");
  });

  it("asks every node about every model, and renders the answer in the row", async () => {
    vi.mocked(fetchNodes).mockResolvedValue([
      { is_control_plane: true, address: "192.168.1.100", name: "spark-01" },
      { is_control_plane: false, address: "10.0.0.11" },
    ] as never);
    vi.mocked(fetchModelPresence).mockImplementation((id: string) =>
      Promise.resolve({
        model: id,
        local: true,
        local_state: "verified",
        nodes: [{ node: "10.0.0.11", present: false, state: "absent", error: null }],
      }),
    );
    renderPage();

    expect(await screen.findAllByText("spark-01 only")).toHaveLength(2);
  });

  it("leaves the row unchecked when the presence read fails", async () => {
    vi.mocked(fetchNodes).mockResolvedValue([
      { is_control_plane: true, address: "192.168.1.100" },
      { is_control_plane: false, address: "10.0.0.11" },
    ] as never);
    vi.mocked(fetchModelPresence).mockRejectedValue(new Error("no route to host"));
    renderPage();

    await screen.findByText("acme/plain-7b");
    await waitFor(() => expect(screen.getAllByText("not checked")).toHaveLength(2));
  });
});

/** The phone.
 *
 * A six-column table at 390px is a horizontal scrollbar with the actions off
 * the end of it. Under 900 the rows are cards — one set of markup, not two, so
 * there is exactly one button named "Remove acme/plain-7b" on the page.
 */
describe("Library — models on a phone", () => {
  beforeEach(() => {
    Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 390 });
  });

  it("renders cards rather than a table, and only one of each action", async () => {
    renderPage();
    await screen.findByText("acme/plain-7b");

    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByTestId("model-acme/plain-7b")).toHaveTextContent("4.0 GB · bfloat16");
    expect(screen.getAllByLabelText("Remove acme/plain-7b")).toHaveLength(1);
  });
});
