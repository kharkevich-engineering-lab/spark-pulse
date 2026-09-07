/** Engines: one page for what the cluster can run and what it costs in disk.
 *
 * Most of this file came from `ImagesPage.test.tsx`, because most of the page
 * did: pull progress over SSE, digest drift, the delete guard. What is new is
 * the merge itself — an engine's capabilities and its image's size on one row —
 * and the part that was missing entirely: `sync` fills every machine in the
 * cluster, and until now delete could only clean this one.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import EnginesPage, { joinEngines, shortDigest, updateReason } from "@/pages/EnginesPage";
import type { ClusterNode, EngineSummary, ImageEntry, Settings } from "@/lib/types";

/** The shared setupTests EventSource stub records listeners but cannot deliver
 *  frames; this subclass can push them. */
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

  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

vi.mock("@/lib/api", () => ({
  fetchImages: vi.fn(),
  fetchImagePulls: vi.fn(),
  fetchImagePresence: vi.fn(),
  startImagePull: vi.fn(),
  cancelImagePull: vi.fn(),
  deleteImage: vi.fn(),
  syncImageToNodes: vi.fn(),
  fetchEngines: vi.fn(),
  refreshEngines: vi.fn(),
  fetchNodes: vi.fn(),
  fetchSettings: vi.fn(),
  updateSettings: vi.fn(),
}));

import {
  cancelImagePull,
  deleteImage,
  fetchEngines,
  fetchImagePresence,
  fetchImagePulls,
  fetchImages,
  fetchNodes,
  fetchSettings,
  refreshEngines,
  startImagePull,
  syncImageToNodes,
  updateSettings,
} from "@/lib/api";

const PRESENT = "ghcr.io/acme/engine/vllm:0.1.0";
const DRIFTED = "ghcr.io/acme/engine/sglang:0.1.0";
const ABSENT = "ghcr.io/acme/engine/vllm:0.2.0";

function entry(overrides: Partial<ImageEntry>): ImageEntry {
  return {
    ref: PRESENT,
    repository: "ghcr.io/acme/engine/vllm",
    tag: "0.1.0",
    tagged_ref: PRESENT,
    engine: "vllm",
    variant: "default",
    engine_key: "vllm/default",
    version: "0.1.0",
    legacy_tags: [],
    source: "bundled",
    description: "",
    present: true,
    image_id: "sha256:aaaa",
    size_bytes: 26_843_545_600,
    created: "2026-01-01T00:00:00Z",
    local_digest: "sha256:aaaaaaaaaaaaaaaa",
    index_digest: "",
    digest_drift: false,
    update_available: false,
    ...overrides,
  };
}

const IMAGES: ImageEntry[] = [
  entry({}),
  entry({
    ref: DRIFTED,
    repository: "ghcr.io/acme/engine/sglang",
    engine: "sglang",
    engine_key: "sglang/default",
    local_digest: "sha256:bbbbbbbbbbbbbbbb",
    index_digest: "sha256:cccccccccccccccc",
    digest_drift: true,
    update_available: true,
  }),
  entry({
    ref: ABSENT,
    tag: "0.2.0",
    present: false,
    size_bytes: 0,
    local_digest: "",
    update_available: true,
  }),
];

function engine(overrides: Partial<EngineSummary> = {}): EngineSummary {
  return {
    engine: "vllm",
    variant: "default",
    key: "vllm/default",
    description: "vLLM built from source for DGX Spark.",
    image: "ghcr.io/acme/engine/vllm",
    image_ref: PRESENT,
    version: "0.1.0",
    tag: "0.1.0",
    digest: null,
    legacy_tags: [],
    capabilities: { mods: true, pr_mods: false, solo: true, cluster: true, mesh: false },
    verified: [],
    ports: { api: 8000, rendezvous: 29500 },
    readiness: "/v1/models",
    models_endpoint: "/v1/models",
    metrics: null,
    source: "bundled",
    enabled: true,
    available: true,
    usable: true,
    ...overrides,
  } as EngineSummary;
}

function node(address: string, control = false): ClusterNode {
  return {
    id: address,
    name: address,
    address,
    is_control_plane: control,
    ssh_user: "spark",
    ssh_key_path: "",
    ethernet_interface: "eth0",
    infiniband_interfaces: [],
    state: "healthy",
    last_seen: null,
    machine_id: address,
  };
}

const SETTINGS = {
  default_engine: "vllm",
  engine_indexes: ["https://acme.test/engines.json"],
  engine_index_cache_ttl_seconds: 3600,
} as Settings;

function renderPage() {
  return render(
    <MemoryRouter>
      <EnginesPage />
    </MemoryRouter>,
  );
}

describe("shortDigest", () => {
  it("strips the algorithm prefix and truncates", () => {
    expect(shortDigest("sha256:0123456789abcdef0123")).toBe("0123456789ab");
  });

  it("renders an em dash for nothing", () => {
    expect(shortDigest("")).toBe("—");
    expect(shortDigest(null)).toBe("—");
  });
});

describe("updateReason", () => {
  it("calls out a republished digest", () => {
    expect(updateReason(IMAGES[1])).toBe("newer digest published");
  });

  it("calls out an image that was never pulled", () => {
    expect(updateReason(IMAGES[2])).toBe("not pulled");
  });

  it("says nothing about an up-to-date image", () => {
    expect(updateReason(IMAGES[0])).toBe("");
  });
});

describe("joinEngines", () => {
  it("puts the engine and its image on one row", () => {
    const rows = joinEngines([IMAGES[0]], [engine()]);

    expect(rows[0].engine?.key).toBe("vllm/default");
    expect(rows[0].image.size_bytes).toBe(26_843_545_600);
  });

  /** Something pulled by hand is still occupying disk, so it keeps its row. */
  it("keeps an image no engine claims", () => {
    const rows = joinEngines([entry({ ref: "docker.io/me/thing:1", engine_key: "" })], []);

    expect(rows).toHaveLength(1);
    expect(rows[0].engine).toBeNull();
  });
});

describe("EnginesPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    CapturingEventSource.instances = [];
    vi.stubGlobal("EventSource", CapturingEventSource);
    vi.mocked(fetchImages).mockResolvedValue(IMAGES);
    vi.mocked(fetchImagePulls).mockResolvedValue([]);
    vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "vllm", engines: [engine()] });
    vi.mocked(fetchNodes).mockResolvedValue([node("10.0.0.1", true), node("10.0.0.2")]);
    vi.mocked(fetchSettings).mockResolvedValue(SETTINGS);
  });

  it("lists every image with its presence and size", async () => {
    renderPage();

    await waitFor(() => expect(screen.getAllByText("present").length).toBe(2));
    // Twice over: the status cell, and the chip saying why it wants attention.
    expect(screen.getAllByText("not pulled").length).toBe(2);
    expect(screen.getAllByText("25.0 GB").length).toBe(2);
  });

  it("marks digest drift and offers a one-click pull", async () => {
    const user = userEvent.setup();
    vi.mocked(startImagePull).mockResolvedValue({ id: "job1", ref: DRIFTED, status: "queued" } as never);
    renderPage();

    await waitFor(() => expect(screen.getByText("newer digest published")).toBeTruthy());
    expect(screen.getByText("cccccccccccc")).toBeTruthy();

    await user.click(screen.getByLabelText(`Pull ${DRIFTED}`));

    expect(startImagePull).toHaveBeenCalledWith(DRIFTED);
  });

  /** The point of the merge: what an engine *is* and what it costs, together. */
  it("shows the engine's own facts under its row", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchImagePresence).mockResolvedValue({
      ref: PRESENT,
      local: true,
      image_id: "sha256:aaaa",
      nodes: [{ node: "10.0.0.2", present: true, image_id: "sha256:aaaa", matches: true, error: null }],
    });
    renderPage();

    await user.click(await screen.findByRole("button", { name: `Details for ${PRESENT}` }));

    expect(await screen.findByText("vLLM built from source for DGX Spark.")).toBeInTheDocument();
    expect(screen.getByText(/mods, solo, cluster/)).toBeInTheDocument();
    expect(screen.getByText(/never verified on hardware/)).toBeInTheDocument();
  });

  it("asks the nodes what they hold, once, when a row is opened", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchImagePresence).mockResolvedValue({
      ref: PRESENT,
      local: true,
      image_id: "sha256:aaaa",
      nodes: [{ node: "10.0.0.2", present: false, image_id: "", matches: false, error: null }],
    });
    renderPage();

    await user.click(await screen.findByRole("button", { name: `Details for ${PRESENT}` }));

    await waitFor(() => expect(fetchImagePresence).toHaveBeenCalledWith(PRESENT, ["10.0.0.2"]));
    expect(await screen.findByText("absent")).toBeInTheDocument();
    // The control node is not a peer to ask about: it is the one answering.
    expect(vi.mocked(fetchImagePresence).mock.calls[0][1]).not.toContain("10.0.0.1");
  });

  /** `sync` fills every machine; before this, delete cleaned only this one. */
  it("deletes from the nodes the operator picks, not just from here", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchImagePresence).mockResolvedValue({
      ref: PRESENT,
      local: true,
      image_id: "sha256:aaaa",
      nodes: [{ node: "10.0.0.2", present: true, image_id: "sha256:aaaa", matches: true, error: null }],
    });
    vi.mocked(deleteImage).mockResolvedValue({ deleted: PRESENT, image_id: "", freed_bytes: 1 });
    renderPage();

    await user.click(await screen.findByRole("button", { name: `Details for ${PRESENT}` }));
    await screen.findByText("same image");
    await user.click(screen.getByLabelText(`Delete ${PRESENT}`));

    // A node that holds it is pre-ticked: the point is reclaiming disk.
    expect(screen.getByRole("checkbox", { name: /10\.0\.0\.2/ })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleteImage).toHaveBeenCalledWith(PRESENT, ["10.0.0.2"]));
  });

  it("says which nodes were not cleared rather than reporting success", async () => {
    const user = userEvent.setup();
    vi.mocked(deleteImage).mockResolvedValue({
      deleted: PRESENT,
      image_id: "",
      freed_bytes: 1,
      nodes: [{ node: "10.0.0.2", removed: false, error: "agent unreachable" }],
    });
    renderPage();

    await user.click(await screen.findByLabelText(`Delete ${PRESENT}`));
    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(await screen.findByText(/10\.0\.0\.2: agent unreachable/)).toBeInTheDocument();
  });

  it("warns what a delete costs to undo", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByLabelText(`Delete ${PRESENT}`));

    expect(screen.getByText(/Re-pulling it can take tens of minutes/)).toBeInTheDocument();
    expect(deleteImage).not.toHaveBeenCalled();
  });

  it("copies an image to every registered node", async () => {
    const user = userEvent.setup();
    vi.mocked(syncImageToNodes).mockResolvedValue({} as never);
    renderPage();

    await user.click(await screen.findByLabelText(`Copy ${PRESENT} to every node`));

    expect(syncImageToNodes).toHaveBeenCalledWith(PRESENT, ["10.0.0.2"]);
  });

  it("tracks live pull progress from the SSE stream", async () => {
    renderPage();
    await waitFor(() => expect(CapturingEventSource.instances.length).toBe(1));
    expect(CapturingEventSource.instances[0].url).toContain("/sse/images");

    act(() => {
      CapturingEventSource.instances[0].emit({
        type: "image.pull.progress",
        resource_type: "image",
        metadata: {
          id: "job9",
          ref: ABSENT,
          status: "running",
          percent: 42,
          bytes_done: 10,
          bytes_total: 100,
          layers: 3,
        },
      });
    });

    const bar = await screen.findByRole("progressbar", { name: `${ABSENT} progress` });
    expect(bar.getAttribute("aria-valuenow")).toBe("42");
  });

  it("ignores frames for other resource types", async () => {
    renderPage();
    await waitFor(() => expect(CapturingEventSource.instances.length).toBe(1));

    act(() => {
      CapturingEventSource.instances[0].emit({
        type: "model.download.progress",
        resource_type: "model",
        metadata: { id: "other", model: "acme/x" },
      });
    });

    expect(screen.queryByTestId("pull-other")).toBeNull();
  });

  it("cancels a running pull", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchImagePulls).mockResolvedValue([
      { id: "job9", ref: ABSENT, status: "running", percent: 10, bytes_done: 1, bytes_total: 10, layers: 2 } as never,
    ]);
    vi.mocked(cancelImagePull).mockResolvedValue({ id: "job9", status: "cancelled" } as never);
    renderPage();

    await user.click(await screen.findByLabelText(`Cancel pull of ${ABSENT}`));

    expect(cancelImagePull).toHaveBeenCalledWith("job9");
  });

  it("pulls an arbitrary reference typed into the form", async () => {
    const user = userEvent.setup();
    vi.mocked(startImagePull).mockResolvedValue({ id: "j", ref: "x", status: "queued" } as never);
    renderPage();

    await user.type(await screen.findByLabelText("Image reference"), "docker.io/me/thing:1");
    await user.click(screen.getByRole("button", { name: "Pull" }));

    expect(startImagePull).toHaveBeenCalledWith("docker.io/me/thing:1");
  });

  /** An engine with no published image cannot run anything; the row says so
   *  rather than looking like one that simply has not been pulled. */
  it("marks an engine whose image was never published", async () => {
    vi.mocked(fetchEngines).mockResolvedValue({
      default_engine: "vllm",
      engines: [engine({ available: false })],
    });
    renderPage();

    // Every row for that engine says so — three images, one unpublished engine.
    expect((await screen.findAllByText("no image published")).length).toBeGreaterThan(0);
  });
});

// ── The registry settings, which used to be a Settings tab ──────────────────

describe("EnginesPage registry settings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("EventSource", CapturingEventSource);
    vi.mocked(fetchImages).mockResolvedValue([]);
    vi.mocked(fetchImagePulls).mockResolvedValue([]);
    vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "vllm", engines: [] });
    vi.mocked(fetchNodes).mockResolvedValue([]);
    vi.mocked(fetchSettings).mockResolvedValue(SETTINGS);
    vi.mocked(refreshEngines).mockResolvedValue({ refreshed: true, engines: 1, indexes: [] } as never);
    vi.mocked(updateSettings).mockResolvedValue(SETTINGS);
  });

  it("edits the indexes as a list, one URL per line", async () => {
    const user = userEvent.setup();
    renderPage();
    const indexes = await screen.findByLabelText("Engine indexes");
    await waitFor(() => expect(indexes).toHaveValue("https://acme.test/engines.json"));

    fireEvent.change(indexes, {
      target: { value: "https://a.test/e.json\nhttps://b.test/e.json" },
    });
    await user.click(screen.getByRole("button", { name: /save registry/i }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({
          engine_indexes: ["https://a.test/e.json", "https://b.test/e.json"],
        }),
      ),
    );
  });

  it("re-reads the index on request", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: /refresh/i }));

    await waitFor(() => expect(refreshEngines).toHaveBeenCalled());
    // Refreshing an index is pointless unless the list is re-read after it.
    await waitFor(() => expect(vi.mocked(fetchEngines).mock.calls.length).toBeGreaterThan(1));
  });

  it("says why an index refresh failed", async () => {
    const user = userEvent.setup();
    vi.mocked(refreshEngines).mockRejectedValue(new Error("API 502: ghcr.io unreachable"));
    renderPage();

    await user.click(await screen.findByRole("button", { name: /refresh/i }));

    expect(await screen.findByText("API 502: ghcr.io unreachable")).toBeInTheDocument();
  });

  it("says the page is empty rather than showing a blank table", async () => {
    renderPage();

    expect(await screen.findByText(/No engines yet/)).toBeInTheDocument();
  });
});
