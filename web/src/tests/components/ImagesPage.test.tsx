import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import ImagesPage, { shortDigest, updateReason } from "@/pages/ImagesPage";
import type { ImageEntry } from "@/lib/types";

/**
 * The shared setupTests EventSource stub records listeners but cannot deliver
 * frames; swap in a capturing subclass so this file can push SSE payloads.
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

  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

vi.mock("@/lib/api", () => ({
  fetchImages: vi.fn(),
  fetchImagePulls: vi.fn(),
  startImagePull: vi.fn(),
  cancelImagePull: vi.fn(),
  deleteImage: vi.fn(),
}));

import { cancelImagePull, deleteImage, fetchImagePulls, fetchImages, startImagePull } from "@/lib/api";

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

const images: ImageEntry[] = [
  entry({}),
  entry({
    ref: DRIFTED,
    repository: "ghcr.io/acme/engine/sglang",
    engine: "sglang",
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

function renderPage() {
  return render(
    <MemoryRouter>
      <ImagesPage />
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
    expect(updateReason(images[1])).toBe("newer digest published");
  });

  it("calls out an image that was never pulled", () => {
    expect(updateReason(images[2])).toBe("not pulled");
  });

  it("says nothing about an up-to-date image", () => {
    expect(updateReason(images[0])).toBe("");
  });
});

describe("ImagesPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    CapturingEventSource.instances = [];
    vi.stubGlobal("EventSource", CapturingEventSource);
    vi.mocked(fetchImages).mockResolvedValue(images);
    vi.mocked(fetchImagePulls).mockResolvedValue([]);
  });

  it("lists every image with its presence and size", async () => {
    renderPage();

    await waitFor(() => expect(screen.getAllByText("present").length).toBe(2));
    expect(screen.getByText("not pulled", { selector: "span.text-text-muted" })).toBeTruthy();
    expect(screen.getAllByText("25.0 GB").length).toBe(2);
  });

  it("marks digest drift and offers a one-click pull", async () => {
    const user = userEvent.setup();
    vi.mocked(startImagePull).mockResolvedValue({ id: "job1", ref: DRIFTED, status: "queued" } as never);
    renderPage();

    await waitFor(() => expect(screen.getByText("newer digest published")).toBeTruthy());
    // The old digest and the advertised one are both shown, so they can be compared.
    expect(screen.getByText("cccccccccccc")).toBeTruthy();

    await user.click(screen.getByLabelText(`Pull ${DRIFTED}`));

    expect(startImagePull).toHaveBeenCalledWith(DRIFTED);
  });

  it("offers no pull button for an up-to-date image", async () => {
    renderPage();

    await waitFor(() => expect(screen.getAllByText("present").length).toBe(2));
    expect(screen.queryByLabelText(`Pull ${PRESENT}`)).toBeNull();
    expect(screen.getByLabelText(`Delete ${PRESENT}`)).toBeTruthy();
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
      {
        id: "job9",
        ref: ABSENT,
        status: "running",
        percent: 10,
        bytes_done: 1,
        bytes_total: 10,
        layers: 2,
      } as never,
    ]);
    vi.mocked(cancelImagePull).mockResolvedValue({ id: "job9", status: "cancelled" } as never);
    renderPage();

    await user.click(await screen.findByLabelText(`Cancel pull of ${ABSENT}`));

    expect(cancelImagePull).toHaveBeenCalledWith("job9");
  });

  it("confirms before deleting", async () => {
    const user = userEvent.setup();
    vi.mocked(deleteImage).mockResolvedValue({ deleted: PRESENT, image_id: "", freed_bytes: 1 });
    renderPage();

    await user.click(await screen.findByLabelText(`Delete ${PRESENT}`));
    expect(deleteImage).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(deleteImage).toHaveBeenCalledWith(PRESENT));
  });
});
