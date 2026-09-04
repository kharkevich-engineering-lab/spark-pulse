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
});
