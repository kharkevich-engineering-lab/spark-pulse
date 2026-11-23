/** The Library shell: one page where four used to be.
 *
 * The properties that matter here are the ones a merge can silently break.
 * Every old address still has to resolve — `/engines` and `/cache` (and `/oci`, now Recipes' sources)
 * were bookmarks, MCP tool targets and spec entry points — and choosing a tab
 * has to put that address back in the bar, so the browser's own Back button
 * still means what it looks like it means. And only the tab on screen is
 * mounted, because each one opens a stream and asks every node questions.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import EnginesPage from "@/pages/EnginesPage";
import ModelsPage from "@/pages/ModelsPage";
import type { CacheNode, ImageEntry, ModelEntry } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchModels: vi.fn(),
  fetchCache: vi.fn(),
  fetchImages: vi.fn(),
  fetchOciRegistries: vi.fn(),
  cleanCache: vi.fn(),
  cleanAllCaches: vi.fn(),
  // Whatever the mounted tab asks for on its own.
  fetchModelSources: vi.fn(() => Promise.resolve([{ name: "hf", type: "hf_hub" }])),
  saveModelSources: vi.fn(),
  refreshEngines: vi.fn(),
  fetchModelDownloads: vi.fn(() => Promise.resolve([])),
  fetchScheduledDeploys: vi.fn(() => Promise.resolve([])),
  fetchNodes: vi.fn(() => Promise.resolve([])),
  fetchModelPresence: vi.fn(() => Promise.resolve({ local: true, local_state: "verified", nodes: [] })),
  startModelDownload: vi.fn(),
  cancelModelDownload: vi.fn(),
  cancelScheduledDeploy: vi.fn(),
  deleteModel: vi.fn(),
  syncModelToNodes: vi.fn(),
  fetchEngines: vi.fn(() => Promise.resolve({ default_engine: "vllm", engines: [], indexes: [{ ref: "idx-a", engines: 1, fetched_at: null, error: null }, { ref: "idx-b", engines: 1, fetched_at: null, error: null }] })),
  fetchImagePulls: vi.fn(() => Promise.resolve([])),
  fetchImagePresence: vi.fn(),
  startImagePull: vi.fn(),
  cancelImagePull: vi.fn(),
  deleteImage: vi.fn(),
  syncImageToNodes: vi.fn(),
  fetchSettings: vi.fn(() => Promise.resolve({})),
  updateSettings: vi.fn(),
  fetchOciCollections: vi.fn(() => Promise.resolve([])),
  fetchOciMeta: vi.fn(() => Promise.resolve([])),
  fetchOciCollectionState: vi.fn(),
  applyOciCollection: vi.fn(),
  addOciRegistry: vi.fn(),
  updateOciRegistry: vi.fn(),
  removeOciRegistry: vi.fn(),
  testOciRegistry: vi.fn(),
  fetchOciRegistryVersions: vi.fn(() => Promise.resolve({ versions: [] })),
}));

import { fetchCache, fetchImages, fetchModels, fetchOciRegistries } from "@/lib/api";

const MODEL = {
  id: "acme/plain-7b",
  source: "hf",
  source_type: "hf_cache",
  path: "/hub/snap",
  revision: null,
  revisions: [],
  size_bytes: 10 * 1024 ** 3,
  last_modified: null,
  config: null,
  referenced_by: [],
} as ModelEntry;

const IMAGE = {
  ref: "ghcr.io/acme/engine/vllm:0.1.0",
  repository: "ghcr.io/acme/engine/vllm",
  tag: "0.1.0",
  engine: "vllm",
  engine_key: "vllm",
  variant: "default",
  version: "0.1.0",
  description: "",
  present: true,
  size_bytes: 5 * 1024 ** 3,
  local_digest: "sha256:aaaa",
  index_digest: "sha256:aaaa",
  digest_drift: false,
  update_available: false,
} as unknown as ImageEntry;

const CACHE: CacheNode = {
  node_id: "control-1",
  name: "spark-01",
  address: "192.168.1.100",
  is_control_plane: true,
  reachable: true,
  reason: null,
  total_bytes: 1024 ** 3,
  dirs: [
    {
      name: "huggingface",
      path: "/home/spark/.cache/huggingface",
      size_bytes: 1024 ** 3,
      file_count: 3,
      description: "",
      exists: true,
      truncated: false,
      error: null,
    },
  ],
};

const renderAt = (path: string, page = path.startsWith("/engines") ? <EnginesPage /> : <ModelsPage />) =>
  render(<MemoryRouter initialEntries={[path]}>{page}</MemoryRouter>);

describe("Library — the kind pages", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchModels).mockResolvedValue([MODEL]);
    vi.mocked(fetchImages).mockResolvedValue([IMAGE]);
    vi.mocked(fetchCache).mockResolvedValue({ nodes: [CACHE] });
    vi.mocked(fetchOciRegistries).mockResolvedValue([
      { name: "ghcr", url: "ghcr.io/acme", enabled: true, default: true, auth_type: "none" },
    ]);
  });

  it("says what is on the disk, and how much of it is cache", async () => {
    renderAt("/models");

    // 10 GB of models, 5 GB of images, 1 GB of cache.
    expect(await screen.findByText("16.0 GB on disk, 1.0 GB of it cache.")).toBeInTheDocument();
  });

  it("titles models once and offers the two sections, with the sources counted", async () => {
    renderAt("/models");
    expect(await screen.findByRole("heading", { level: 1, name: "Models." })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Catalogue" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("tab", { name: /^Sources/ })).toHaveTextContent("1");
  });

  it("opens the engine images at their own address, with Pull image as its action", async () => {
    renderAt("/engines");

    expect(await screen.findByTestId("engines-table")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Engines." })).toBeInTheDocument();
    expect(await screen.findByText("1 of 1 pulled, 5.0 GB on disk.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Pull image" }));
    expect(await screen.findByLabelText("Image reference")).toBeInTheDocument();
  });

  it("follows the section: Add index on the engine sources, and the sources count", async () => {
    renderAt("/engines#sources");

    expect(await screen.findByTestId("engine-sources")).toBeInTheDocument();
    expect(screen.queryByTestId("engines-table")).toBeNull();
    expect(screen.queryByRole("button", { name: "Pull image" })).toBeNull();
    await waitFor(() => expect(screen.getByRole("tab", { name: /^Sources/ })).toHaveTextContent("2"));
    fireEvent.click(screen.getByRole("button", { name: "Add index" }));
    expect(await screen.findByLabelText("Index 1")).toBeInTheDocument();
  });

  it("follows the section: Add source on the model sources", async () => {
    renderAt("/models#sources");

    expect(await screen.findByTestId("model-sources")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download model" })).toBeNull();
    await screen.findByLabelText("Source 1 name");
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));
    expect(await screen.findByLabelText("Source 2 name")).toBeInTheDocument();
  });

  it("answers /cache with the models catalogue", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    renderAt("/cache");
    expect(await screen.findByRole("heading", { level: 1, name: "Models." })).toBeInTheDocument();
  });

  it("opens the download dialog from the header and closes it on cancel", async () => {
    renderAt("/models");
    await screen.findByText("acme/plain-7b");
    fireEvent.click(screen.getByRole("button", { name: "Download model" }));
    expect(await screen.findByLabelText("Model id")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Model id")).toBeNull();
  });

  /** Only one section is mounted: the engines catalogue is what opens
   *  /sse/images and asks every node what it holds. */
  it("mounts only the page on screen", async () => {
    renderAt("/models");
    await screen.findByText("acme/plain-7b");

    expect(screen.queryByLabelText("Image reference")).toBeNull();
  });
});
