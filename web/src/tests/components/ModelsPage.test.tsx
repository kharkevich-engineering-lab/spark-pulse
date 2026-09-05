import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import ModelsPage, { describePrecision, shortRevision } from "@/pages/ModelsPage";
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
  fetchModels: vi.fn(),
  fetchModelSources: vi.fn(),
  fetchModelDownloads: vi.fn(),
  startModelDownload: vi.fn(),
  cancelModelDownload: vi.fn(),
  deleteModel: vi.fn(),
  saveModelSources: vi.fn(),
}));

import {
  cancelModelDownload,
  deleteModel,
  fetchModelDownloads,
  fetchModelSources,
  fetchModels,
  saveModelSources,
  startModelDownload,
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

const renderPage = () => render(<MemoryRouter><ModelsPage /></MemoryRouter>);

describe("ModelsPage helpers", () => {
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

describe("ModelsPage", () => {
  beforeEach(() => {
    CapturingEventSource.instances = [];
    vi.stubGlobal("EventSource", CapturingEventSource);
    vi.mocked(fetchModels).mockResolvedValue(models);
    vi.mocked(fetchModelSources).mockResolvedValue([
      { name: "hf", type: "hf_hub", endpoint: "https://huggingface.co", token_secret: "hf_token" },
      { name: "mirror", type: "hf_hub", endpoint: "http://mirror.local", token_secret: "" },
    ]);
    vi.mocked(fetchModelDownloads).mockResolvedValue([]);
  });

  it("renders the catalogue table", async () => {
    renderPage();
    expect(await screen.findByText("acme/plain-7b")).toBeInTheDocument();
    expect(screen.getByText("acme/quant-70b")).toBeInTheDocument();
    expect(screen.getByText("awq")).toBeInTheDocument();
    // Referenced-by count for the first model.
    expect(screen.getByText("1")).toBeInTheDocument();
    // Total on disk.
    expect(screen.getByText("24.0 GB")).toBeInTheDocument();
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

    await user.click(await screen.findByLabelText("Delete acme/plain-7b"));
    expect(screen.getByText(/Delete the cached snapshot/)).toBeInTheDocument();
    expect(deleteModel).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(deleteModel).toHaveBeenCalledWith("acme/plain-7b"));
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

    await user.click(await screen.findByLabelText("Delete acme/plain-7b"));
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

/** The sources editor.
 *
 * A source is where a model id is resolved from, so a broken one means every
 * download fails with a network error rather than a useful message. It is a
 * draft-then-save editor: nothing is written until Save, the shape of the row
 * follows the type (a hub source has an endpoint and a token, a local one has
 * a path), and a rejected save has to say so rather than looking applied.
 */
describe("ModelsPage sources editor", () => {
  const SOURCES: { name: string; type: "hf_hub" | "local_path"; endpoint?: string; token_secret?: string; path?: string }[] = [
    { name: "hf", type: "hf_hub", endpoint: "https://huggingface.co", token_secret: "hf_token" },
  ];

  beforeEach(() => {
    CapturingEventSource.instances = [];
    vi.stubGlobal("EventSource", CapturingEventSource);
    vi.mocked(fetchModels).mockResolvedValue([]);
    vi.mocked(fetchModelDownloads).mockResolvedValue([]);
    vi.mocked(fetchModelSources).mockResolvedValue(SOURCES);
    vi.mocked(saveModelSources).mockResolvedValue(SOURCES as never);
  });

  it("edits a source in a draft and writes it only on save", async () => {
    const user = userEvent.setup();
    renderPage();

    const endpoint = await screen.findByLabelText("Source 1 endpoint");
    await user.clear(endpoint);
    await user.type(endpoint, "http://mirror.local");
    expect(saveModelSources).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /^Save$/ }));

    await waitFor(() =>
      expect(saveModelSources).toHaveBeenCalledWith([
        expect.objectContaining({ name: "hf", endpoint: "http://mirror.local" }),
      ]),
    );
  });

  it("swaps a hub source's endpoint and token for a path when it becomes local", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.selectOptions(await screen.findByLabelText("Source 1 type"), "local_path");

    expect(screen.queryByLabelText("Source 1 endpoint")).toBeNull();
    expect(screen.queryByLabelText("Source 1 token secret")).toBeNull();
    const path = screen.getByLabelText("Source 1 path");
    await user.type(path, "/srv/models");
    await user.click(screen.getByRole("button", { name: /^Save$/ }));

    await waitFor(() =>
      expect(saveModelSources).toHaveBeenCalledWith([
        expect.objectContaining({ type: "local_path", path: "/srv/models" }),
      ]),
    );
  });

  it("adds and removes rows without touching the stored list until saved", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByLabelText("Source 1 name");

    await user.click(screen.getByRole("button", { name: /add source/i }));
    await user.type(screen.getByLabelText("Source 2 name"), "spare");

    await user.click(screen.getByLabelText("Remove source 1"));
    // The second row is now the first, and it is the one that survives.
    expect(screen.queryByLabelText("Source 2 name")).toBeNull();

    await user.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() =>
      expect(saveModelSources).toHaveBeenCalledWith([expect.objectContaining({ name: "spare" })]),
    );
  });

  it("says the list is empty rather than showing bare buttons", async () => {
    vi.mocked(fetchModelSources).mockResolvedValue([]);
    renderPage();

    expect(await screen.findByText("No sources configured.")).toBeInTheDocument();
  });

  it("says why a save was refused", async () => {
    const user = userEvent.setup();
    vi.mocked(saveModelSources).mockRejectedValue(new Error("endpoint is not a URL"));
    renderPage();
    await screen.findByLabelText("Source 1 name");

    await user.click(screen.getByRole("button", { name: /^Save$/ }));

    expect(await screen.findByRole("heading", { name: "Save failed" })).toBeInTheDocument();
    expect(screen.getByText("endpoint is not a URL")).toBeInTheDocument();
  });
});
