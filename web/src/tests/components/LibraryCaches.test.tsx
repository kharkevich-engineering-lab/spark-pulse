/** The caches: a section of Library, and one sub-section per node.
 *
 * Cleaning a cache is irreversible and re-downloading a model catalogue is an
 * afternoon, so the properties worth holding are that the confirmation names
 * *which* cache on *which* node is about to go, that nothing is deleted until
 * it is confirmed, and that a failed clean says so instead of leaving the
 * operator to guess whether it worked.
 *
 * What is new is the node. The section used to list four directories with no
 * machine attached, because the control plane walked its own `~/.cache` and the
 * page presented that as the cluster. Two nodes are now two sub-sections, each
 * with its own total and its own buttons, and a node that could not be asked
 * keeps its heading and says why — an absent section and an idle machine look
 * identical, and only one of them is fine.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import LibraryPage from "@/pages/LibraryPage";
import type { CacheNode, CacheResponse } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchCache: vi.fn(),
  cleanCache: vi.fn(),
  cleanAllCaches: vi.fn(),
  fetchModels: vi.fn(() => Promise.resolve([])),
  fetchImages: vi.fn(() => Promise.resolve([])),
  fetchOciRegistries: vi.fn(() => Promise.resolve([])),
  fetchModelSources: vi.fn(() => Promise.resolve([])),
  fetchModelDownloads: vi.fn(() => Promise.resolve([])),
  fetchScheduledDeploys: vi.fn(() => Promise.resolve([])),
  fetchNodes: vi.fn(() => Promise.resolve([])),
  fetchModelPresence: vi.fn(),
  startModelDownload: vi.fn(),
  cancelModelDownload: vi.fn(),
  cancelScheduledDeploy: vi.fn(),
  deleteModel: vi.fn(),
  syncModelToNodes: vi.fn(),
}));

import { cleanAllCaches, cleanCache, fetchCache } from "@/lib/api";

const dir = (name: string, bytes: number, files: number, extra = {}) => ({
  name,
  path: `/home/spark/.cache/${name}`,
  size_bytes: bytes,
  file_count: files,
  description: "",
  exists: true,
  truncated: false,
  error: null,
  ...extra,
});

const CONTROL: CacheNode = {
  node_id: "control-1",
  name: "spark-01",
  address: "192.168.1.100",
  is_control_plane: true,
  reachable: true,
  reason: null,
  total_bytes: 27_917_287_424,
  dirs: [
    dir("huggingface", 26_843_545_600, 412),
    dir("vllm", 1_073_741_824, 1),
  ],
};

const PEER: CacheNode = {
  node_id: "peer-1",
  name: "spark-02",
  address: "10.0.0.11",
  is_control_plane: false,
  reachable: true,
  reason: null,
  total_bytes: 2_147_483_648,
  dirs: [dir("huggingface", 2_147_483_648, 40), dir("vllm", 0, 0, { exists: false })],
};

const FLEET: CacheResponse = { nodes: [CONTROL, PEER] };

const cleaned = (node: string, name: string) => ({
  node,
  reachable: true,
  reason: null,
  results: [{ name, path: `/home/spark/.cache/${name}`, removed: true, freed_bytes: 1, error: null }],
});

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <LibraryPage />
    </MemoryRouter>,
  );

describe("Library — caches per node", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchCache).mockResolvedValue(FLEET);
    vi.mocked(cleanCache).mockResolvedValue(cleaned("control-1", "huggingface"));
    vi.mocked(cleanAllCaches).mockResolvedValue(cleaned("control-1", "vllm"));
  });

  it("gives every node its own sub-section, with its own total", async () => {
    renderAt("/models");

    expect(await screen.findByRole("heading", { name: "spark-01" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "spark-02" })).toBeInTheDocument();
    // Each node's own total, beside its own heading: 25.0 + 1.0 here, 2.0 there.
    expect(within(screen.getByTestId("cache-node-control-1")).getByText("26.0 GB")).toBeInTheDocument();
    expect(within(screen.getByTestId("cache-node-peer-1")).getAllByText("2.0 GB").length).toBeGreaterThan(0);
  });

  it("lists each node's caches with the path that node resolved", async () => {
    renderAt("/models");

    await screen.findByRole("heading", { name: "spark-01" });
    const control = screen.getByTestId("cache-control-1-huggingface");
    expect(control).toHaveTextContent("/home/spark/.cache/huggingface");
    expect(control).toHaveTextContent("412 files");
    // One file is one file, not "1 files".
    expect(screen.getByTestId("cache-control-1-vllm")).toHaveTextContent("1 file");
  });

  it("sums only the nodes that answered into the page header", async () => {
    renderAt("/models");

    // 26.0 GB on one node and 2.0 GB on the other, and no models or images.
    expect(await screen.findByText(/28\.0 GB of it cache/)).toBeInTheDocument();
  });

  /** `/cache` was a page. It is an address that still has to arrive
   *  somewhere true, which is the Models tab with this section on screen. */
  it("answers the old /cache address on the models tab", async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    renderAt("/cache");

    expect(await screen.findByRole("heading", { name: "Caches" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Models/ })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
  });

  it("names the cache and the node it is about to empty, and empties nothing until confirmed", async () => {
    const user = userEvent.setup();
    renderAt("/models");
    await screen.findByRole("heading", { name: "spark-02" });

    await user.click(screen.getByLabelText("Clean the huggingface cache on spark-02"));
    expect(
      await screen.findByText(/Clean cache "huggingface" on spark-02\?/),
    ).toBeInTheDocument();
    expect(cleanCache).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Clean" }));
    await waitFor(() => expect(cleanCache).toHaveBeenCalledWith("peer-1", "huggingface"));
  });

  it("sweeps one node at a time, in different words, and says the models stay", async () => {
    const user = userEvent.setup();
    renderAt("/models");
    await screen.findByRole("heading", { name: "spark-01" });

    const control = screen.getByTestId("cache-node-control-1");
    await user.click(within(control).getByRole("button", { name: "Clean all on this node" }));

    expect(
      await screen.findByRole("heading", { name: "Clean this node's caches" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/except the downloaded models/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clean" }));
    await waitFor(() => expect(cleanAllCaches).toHaveBeenCalledWith("control-1"));
    expect(cleanCache).not.toHaveBeenCalled();
  });

  it("backs out without deleting when the confirmation is dismissed", async () => {
    const user = userEvent.setup();
    renderAt("/models");
    await screen.findByRole("heading", { name: "spark-01" });

    const control = screen.getByTestId("cache-node-control-1");
    await user.click(within(control).getByRole("button", { name: "Clean all on this node" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(cleanAllCaches).not.toHaveBeenCalled();
  });

  it("says why a clean failed rather than silently leaving the cache in place", async () => {
    const user = userEvent.setup();
    vi.mocked(cleanCache).mockRejectedValue(new Error("API 500: permission denied"));
    renderAt("/models");
    await screen.findByRole("heading", { name: "spark-01" });

    await user.click(screen.getByLabelText("Clean the vllm cache on spark-01"));
    await user.click(screen.getByRole("button", { name: "Clean" }));

    expect(await screen.findByText("API 500: permission denied")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "OK" }));
    await waitFor(() => expect(screen.queryByText("API 500: permission denied")).toBeNull());
  });

  it("surfaces a node that went away between the listing and the clean", async () => {
    const user = userEvent.setup();
    vi.mocked(cleanCache).mockResolvedValue({
      node: "peer-1",
      reachable: false,
      reason: "no agent for 10.0.0.11",
      results: [],
    });
    renderAt("/models");
    await screen.findByRole("heading", { name: "spark-02" });

    await user.click(screen.getByLabelText("Clean the huggingface cache on spark-02"));
    await user.click(screen.getByRole("button", { name: "Clean" }));

    expect(await screen.findByText("no agent for 10.0.0.11")).toBeInTheDocument();
  });

  it("surfaces a refusal the node made, such as the model cache", async () => {
    const user = userEvent.setup();
    vi.mocked(cleanCache).mockResolvedValue({
      node: "peer-1",
      reachable: true,
      reason: null,
      results: [
        {
          name: "huggingface",
          path: "/home/spark/.cache/huggingface",
          removed: false,
          freed_bytes: 0,
          error: "refused: the hub cache holds downloaded models",
        },
      ],
    });
    renderAt("/models");
    await screen.findByRole("heading", { name: "spark-02" });

    await user.click(screen.getByLabelText("Clean the huggingface cache on spark-02"));
    await user.click(screen.getByRole("button", { name: "Clean" }));

    expect(
      await screen.findByText("refused: the hub cache holds downloaded models"),
    ).toBeInTheDocument();
  });

  it("keeps an unreachable node's heading and says why, with no buttons to press", async () => {
    vi.mocked(fetchCache).mockResolvedValue({
      nodes: [
        CONTROL,
        {
          ...PEER,
          reachable: false,
          reason: "this node's agent is too old to measure caches; update it",
          total_bytes: 0,
          dirs: [],
        },
      ],
    });
    renderAt("/models");

    expect(await screen.findByRole("heading", { name: "spark-02" })).toBeInTheDocument();
    expect(
      screen.getByText("this node's agent is too old to measure caches; update it"),
    ).toBeInTheDocument();
    const peer = screen.getByTestId("cache-node-peer-1");
    expect(within(peer).queryByRole("button")).toBeNull();
    // And the other node is untouched by its neighbour's silence.
    expect(screen.getByTestId("cache-control-1-huggingface")).toBeInTheDocument();
  });

  it("leaves an unreachable node out of the page's cache total", async () => {
    vi.mocked(fetchCache).mockResolvedValue({
      nodes: [CONTROL, { ...PEER, reachable: false, reason: "no agent", total_bytes: 0, dirs: [] }],
    });
    renderAt("/models");

    expect(await screen.findByText(/26\.0 GB of it cache/)).toBeInTheDocument();
  });

  it("says a size is a floor when the node's walk stopped at its ceiling", async () => {
    vi.mocked(fetchCache).mockResolvedValue({
      nodes: [
        {
          ...CONTROL,
          dirs: [dir("huggingface", 26_843_545_600, 1_000_000, { truncated: true })],
        },
      ],
    });
    renderAt("/models");

    expect(await screen.findByText(/at least 25\.0 GB/)).toBeInTheDocument();
  });

  it("shows a directory's own error without losing its neighbours", async () => {
    vi.mocked(fetchCache).mockResolvedValue({
      nodes: [
        {
          ...CONTROL,
          dirs: [
            dir("huggingface", 0, 0, { error: "/home/spark/.cache/huggingface: denied" }),
            dir("vllm", 1_073_741_824, 1),
          ],
        },
      ],
    });
    renderAt("/models");

    expect(
      await screen.findByText("/home/spark/.cache/huggingface: denied"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("cache-control-1-vllm")).toBeInTheDocument();
  });

  it("says there is nothing to show rather than drawing an empty grid", async () => {
    vi.mocked(fetchCache).mockResolvedValue({ nodes: [] });
    renderAt("/models");

    expect(await screen.findByText("No cache entries found.")).toBeInTheDocument();
  });

  it("surfaces a failed load instead of an empty section", async () => {
    vi.mocked(fetchCache).mockRejectedValue(new Error("API 503: cache unavailable"));
    renderAt("/models");

    expect(await screen.findByText("API 503: cache unavailable")).toBeInTheDocument();
  });
});
