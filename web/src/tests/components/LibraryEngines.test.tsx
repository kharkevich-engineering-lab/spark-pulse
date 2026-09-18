/** Engines: one page for what the cluster can run and what it costs in disk.
 *
 * Most of this file came from `ImagesPage.test.tsx`, because most of the page
 * did: pull progress over SSE, digest drift, the delete guard. What is new is
 * the merge itself — an engine's capabilities and its image's size on one row —
 * and the part that was missing entirely: `sync` fills every machine in the
 * cluster, and until now delete could only clean this one.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { translatorFor } from "@/lib/i18n";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import LibraryPage from "@/pages/LibraryPage";
import {
  isDigestTag,
  joinEngines,
  shortDigest,
  shortImageTag,
  updateReason,
} from "@/components/library/EnginesTab";
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
  // The Library shell's own reads. Engines is a tab of it now, so the page
  // around this one still asks what else is on the disk.
  fetchModels: vi.fn(() => Promise.resolve([])),
  fetchCache: vi.fn(() => Promise.resolve({ entries: [] })),
  fetchOciRegistries: vi.fn(() => Promise.resolve([])),
  cleanCache: vi.fn(),
  fetchImages: vi.fn(),
  fetchImagePulls: vi.fn(),
  fetchImagePresence: vi.fn(),
  startImagePull: vi.fn(),
  cancelImagePull: vi.fn(),
  deleteImage: vi.fn(),
  syncImageToNodes: vi.fn(),
  fetchEngines: vi.fn(),
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
    <MemoryRouter initialEntries={["/engines"]}>
      <LibraryPage />
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

describe("isDigestTag", () => {
  it("recognizes a digest-pinned tag", () => {
    expect(isDigestTag(`sha256:${"a".repeat(64)}`)).toBe(true);
  });

  it("rejects a floating tag", () => {
    expect(isDigestTag("latest")).toBe(false);
    expect(isDigestTag("26.5.0")).toBe(false);
    expect(isDigestTag(null)).toBe(false);
    expect(isDigestTag(undefined)).toBe(false);
  });
});

describe("shortImageTag", () => {
  // 64 hex characters: 8 distinctive at the front, 4 at the back, noise between.
  const HEX = "01234567" + "9".repeat(52) + "89ab";

  it("keeps the algorithm prefix and enough of each end to compare by eye", () => {
    expect(shortImageTag(`sha256:${HEX}`)).toBe("sha256:01234567…89ab");
  });

  it("leaves a tag-pinned ref exactly as it was", () => {
    expect(shortImageTag("26.5.0")).toBe("26.5.0");
    expect(shortImageTag("latest")).toBe("latest");
  });
});

/** The reason names itself through the dictionary; these quote the
 *  English one. */
const T = translatorFor("en").t;

describe("updateReason", () => {
  it("calls out a republished digest", () => {
    expect(updateReason(IMAGES[1], T)).toBe("newer digest published");
  });

  it("calls out an image that was never pulled", () => {
    expect(updateReason(IMAGES[2], T)).toBe("not pulled");
  });

  it("says nothing about an up-to-date image", () => {
    expect(updateReason(IMAGES[0], T)).toBe("");
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

describe("Library — engines", () => {
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
    // Once, not twice: the chip beside the status is for what the status word
    // cannot say, and "not pulled  not pulled" is one fact in two colours.
    expect(screen.getAllByText("not pulled").length).toBe(1);
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

  /** "Copy to every registered node" was one click and no question. On a
   *  four-node cluster that is 26 GB a machine, decided by a button whose
   *  label said "every" — so it asks which, the same way delete does. */
  it("asks which nodes a copy lands on before sending 26 GB to each", async () => {
    const user = userEvent.setup();
    vi.mocked(syncImageToNodes).mockResolvedValue({} as never);
    renderPage();

    await user.click(await screen.findByLabelText(`Copy ${PRESENT} to other nodes`));
    expect(syncImageToNodes).not.toHaveBeenCalled();
    expect(await screen.findByText(/Each node selected pulls its own copy/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() => expect(syncImageToNodes).toHaveBeenCalledWith(PRESENT, ["10.0.0.2"]));
  });

  it("says why a copy failed rather than leaving the nodes looking fed", async () => {
    const user = userEvent.setup();
    vi.mocked(syncImageToNodes).mockRejectedValue(new Error("registry refused the pull"));
    renderPage();

    await user.click(await screen.findByLabelText(`Copy ${PRESENT} to other nodes`));
    await user.click(screen.getByRole("button", { name: "Copy" }));

    expect(await screen.findByText("registry refused the pull")).toBeInTheDocument();
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

// ── Digest-pinned refs in the image column ───────────────────────────────────
//
// A digest-pinned tag is 64 hex characters; rendered in full it pushed the
// table past the viewport. `shortImageTag` shortens it for display, but the
// full ref has to stay reachable — on hover, and in the DOM for a screen
// reader or a copy.

describe("Library — digest-pinned image refs", () => {
  const DIGEST_HEX = "01234567" + "9".repeat(52) + "89ab";
  const DIGEST_REF = `ghcr.io/acme/engine/llama-cpp:sha256:${DIGEST_HEX}`;

  beforeEach(() => {
    vi.clearAllMocks();
    CapturingEventSource.instances = [];
    vi.stubGlobal("EventSource", CapturingEventSource);
    vi.mocked(fetchImagePulls).mockResolvedValue([]);
    vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "vllm", engines: [] });
    vi.mocked(fetchNodes).mockResolvedValue([node("10.0.0.1", true)]);
    vi.mocked(fetchSettings).mockResolvedValue(SETTINGS);
  });

  it("shortens a digest-pinned ref, keeping the full ref on hover and in the DOM", async () => {
    vi.mocked(fetchImages).mockResolvedValue([
      entry({
        ref: DIGEST_REF,
        repository: "ghcr.io/acme/engine/llama-cpp",
        tag: `sha256:${DIGEST_HEX}`,
        engine: "llama-cpp",
        engine_key: "llama-cpp/default",
      }),
    ]);
    renderPage();

    const shortened = await screen.findByTitle(DIGEST_REF);
    expect(shortened.textContent).toContain("sha256:01234567…89ab");
    expect(shortened.textContent).not.toContain(DIGEST_HEX);
    // The full ref is still in the DOM — for a screen reader, and for copy.
    expect(screen.getByText(DIGEST_REF)).toBeInTheDocument();
  });

  it("leaves a tag-pinned ref exactly as it was, with no hover title", async () => {
    vi.mocked(fetchImages).mockResolvedValue([entry({})]);
    renderPage();

    await screen.findByText("ghcr.io/acme/engine/vllm");
    expect(screen.getByText(":0.1.0")).toBeInTheDocument();
    expect(screen.queryByTitle(PRESENT)).not.toBeInTheDocument();
  });
});

// ── Enabling and disabling an engine ─────────────────────────────────────────
//
// `config.engines` already gates which engines a recipe may pick; only the UI
// to flip it was missing. The switch sits per engine name, not per image row
// — two variants of "vllm" share one switch — and a save has to round-trip
// through the *current* settings.engines map, because `config.update` replaces
// the whole map rather than merging it itself.

describe("Library — the engine enable switch", () => {
  const TWO_ENGINES = [
    engine({ engine: "vllm", key: "vllm/default", image_ref: PRESENT, enabled: true }),
    engine({ engine: "sglang", key: "sglang/default", image_ref: DRIFTED, enabled: true }),
  ];

  beforeEach(() => {
    vi.clearAllMocks();
    CapturingEventSource.instances = [];
    vi.stubGlobal("EventSource", CapturingEventSource);
    // Just the vllm and sglang rows — IMAGES also has a second, unpulled vllm
    // tag, which would give this engine two rows and its switch two matches.
    vi.mocked(fetchImages).mockResolvedValue([IMAGES[0], IMAGES[1]]);
    vi.mocked(fetchImagePulls).mockResolvedValue([]);
    vi.mocked(fetchNodes).mockResolvedValue([node("10.0.0.1", true), node("10.0.0.2")]);
    vi.mocked(fetchSettings).mockResolvedValue(SETTINGS);
  });

  it("shows each engine's switch matching its enabled state", async () => {
    vi.mocked(fetchEngines).mockResolvedValue({
      default_engine: "vllm",
      engines: [TWO_ENGINES[0], { ...TWO_ENGINES[1], enabled: false }],
    });
    renderPage();

    const vllm = await screen.findByRole("switch", { name: "Enable or disable vllm" });
    const sglang = screen.getByRole("switch", { name: "Enable or disable sglang" });
    expect(vllm).toHaveAttribute("aria-checked", "true");
    expect(sglang).toHaveAttribute("aria-checked", "false");
  });

  it("saves the merged engines map, preserving the other engine untouched", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "vllm", engines: TWO_ENGINES });
    vi.mocked(fetchSettings).mockResolvedValue({
      ...SETTINGS,
      engines: { sglang: { enabled: true, note: "keep me" } },
    } as unknown as Settings);
    vi.mocked(updateSettings).mockResolvedValue(SETTINGS);
    renderPage();

    await user.click(await screen.findByRole("switch", { name: "Enable or disable vllm" }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith({
        engines: {
          sglang: { enabled: true, note: "keep me" },
          vllm: { enabled: false },
        },
      }),
    );
    // The list is re-read so the badge and any recipe engine-support reflect it.
    await waitFor(() => expect(vi.mocked(fetchEngines).mock.calls.length).toBeGreaterThan(1));
  });

  it("normalizes a legacy bare-boolean entry for the engine being toggled", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "vllm", engines: TWO_ENGINES });
    vi.mocked(fetchSettings).mockResolvedValue({
      ...SETTINGS,
      engines: { sglang: true, vllm: true },
    } as unknown as Settings);
    vi.mocked(updateSettings).mockResolvedValue(SETTINGS);
    renderPage();

    await user.click(await screen.findByRole("switch", { name: "Enable or disable sglang" }));

    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith({
        engines: { sglang: { enabled: false }, vllm: true },
      }),
    );
  });

  it("reverts the switch and shows an error when the save fails", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "vllm", engines: TWO_ENGINES });
    vi.mocked(updateSettings).mockRejectedValue(new Error("network down"));
    renderPage();

    const toggle = await screen.findByRole("switch", { name: "Enable or disable vllm" });
    await user.click(toggle);

    expect(await screen.findByText("network down")).toBeInTheDocument();
    expect(screen.getByText("Could not change that engine")).toBeInTheDocument();
    // Nothing was persisted, so the switch reads as it did before the click.
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
  });

  it("refuses to disable the last enabled engine", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "vllm", engines: [engine({ enabled: true })] });
    renderPage();

    await user.click(await screen.findByRole("switch", { name: "Enable or disable vllm" }));

    expect(await screen.findByText("Can't disable the last engine")).toBeInTheDocument();
    expect(updateSettings).not.toHaveBeenCalled();
  });
});

/** What the tab says about itself, and what it says on a phone. */
describe("Library — engines, and the width they are read at", () => {
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

  /** The indexes are configuration and move to Settings; leaving no trace of
   *  where they went is how an operator concludes the feature was removed. */
  it("says where engine indexes are configured now", async () => {
    renderPage();

    expect(
      await screen.findByText("Engine indexes are configured in Settings."),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Engine indexes")).toBeNull();
  });

  it("counts the images that want attention", async () => {
    renderPage();

    expect(await screen.findByText("2 need attention")).toBeInTheDocument();
  });

  it("says the catalogue is empty rather than showing a blank table", async () => {
    vi.mocked(fetchImages).mockResolvedValue([]);
    vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "vllm", engines: [] });
    renderPage();

    expect(await screen.findByText(/No engines yet/)).toBeInTheDocument();
  });

  it("renders cards rather than a six-column table on a phone", async () => {
    Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 390 });
    renderPage();

    await screen.findByTestId(`engine-${PRESENT}`);
    expect(screen.queryByRole("table")).toBeNull();
    // One card, so exactly one of each action rather than a hidden second copy.
    expect(screen.getAllByLabelText(`Delete ${PRESENT}`)).toHaveLength(1);
  });

  it("opens a row's per-node detail inside the card", async () => {
    Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 390 });
    vi.mocked(fetchImagePresence).mockResolvedValue({
      ref: PRESENT,
      local: true,
      image_id: "sha256:aaaa",
      nodes: [{ node: "10.0.0.2", present: true, image_id: "sha256:aaaa", matches: true, error: null }],
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: `Details for ${PRESENT}` }));

    expect(await screen.findByText("same image")).toBeInTheDocument();
  });
});
