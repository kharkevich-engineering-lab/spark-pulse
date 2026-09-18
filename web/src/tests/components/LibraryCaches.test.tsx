/** The caches, now a section of Library rather than a page of their own.
 *
 * Cleaning a cache is irreversible and re-downloading a model catalogue is an
 * afternoon, so the properties worth holding are that the confirmation names
 * *which* cache is about to go (the "all" wording is deliberately different
 * from a single entry's), that nothing is deleted until it is confirmed, and
 * that a failed clean says so instead of leaving the operator to guess whether
 * it worked. What is new is that `/cache` still resolves: it opens the Models
 * tab at this section rather than 404ing on a bookmark.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import LibraryPage from "@/pages/LibraryPage";
import type { CacheEntry } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchCache: vi.fn(),
  cleanCache: vi.fn(),
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

import { cleanCache, fetchCache } from "@/lib/api";

const ENTRIES: CacheEntry[] = [
  {
    name: "huggingface",
    path: "/home/spark/.cache/huggingface",
    size_bytes: 26_843_545_600,
    file_count: 412,
    description: "Model weights pulled from the hub",
  },
  {
    name: "pip",
    path: "/home/spark/.cache/pip",
    size_bytes: 1_073_741_824,
    file_count: 1,
    description: "",
  },
];

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <LibraryPage />
    </MemoryRouter>,
  );

describe("Library — caches", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchCache).mockResolvedValue({ entries: ENTRIES });
    vi.mocked(cleanCache).mockResolvedValue({ huggingface: "cleaned" });
  });

  it("lists every cache with its path, size and file count", async () => {
    renderAt("/models");

    expect(await screen.findByText("huggingface")).toBeInTheDocument();
    expect(screen.getByText("/home/spark/.cache/huggingface")).toBeInTheDocument();
    expect(screen.getByText(/25\.0 GB/)).toBeInTheDocument();
    expect(screen.getByText(/412 files/)).toBeInTheDocument();
    // One file is one file, not "1 files".
    expect(screen.getByText(/1 file$/)).toBeInTheDocument();
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

  it("names the single cache it is about to delete, and deletes nothing until confirmed", async () => {
    const user = userEvent.setup();
    renderAt("/models");
    await screen.findByText("huggingface");

    await user.click(screen.getByLabelText("Clean the huggingface cache"));
    expect(await screen.findByText(/Clean cache "huggingface"\?/)).toBeInTheDocument();
    expect(cleanCache).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Clean" }));
    await waitFor(() => expect(cleanCache).toHaveBeenCalledWith(["huggingface"]));
  });

  it("warns in different words when the header button would take every cache", async () => {
    const user = userEvent.setup();
    renderAt("/models");
    await screen.findByText("huggingface");

    await user.click(screen.getByRole("button", { name: "Clean caches" }));

    expect(await screen.findByRole("heading", { name: "Clean All Caches" })).toBeInTheDocument();
    expect(screen.getByText(/This will clean ALL caches/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clean" }));
    await waitFor(() => expect(cleanCache).toHaveBeenCalledWith(["all"]));
  });

  it("backs out without deleting when the confirmation is dismissed", async () => {
    const user = userEvent.setup();
    renderAt("/models");
    await screen.findByText("huggingface");

    await user.click(screen.getByRole("button", { name: "Clean caches" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(cleanCache).not.toHaveBeenCalled();
  });

  it("says why a clean failed rather than silently leaving the cache in place", async () => {
    const user = userEvent.setup();
    vi.mocked(cleanCache).mockRejectedValue(new Error("API 500: permission denied"));
    renderAt("/models");
    await screen.findByText("huggingface");

    await user.click(screen.getByRole("button", { name: "Clean caches" }));
    await user.click(screen.getByRole("button", { name: "Clean" }));

    expect(await screen.findByText("API 500: permission denied")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "OK" }));
    await waitFor(() => expect(screen.queryByText("API 500: permission denied")).toBeNull());
  });

  it("says why cleaning one cache failed", async () => {
    const user = userEvent.setup();
    vi.mocked(cleanCache).mockRejectedValue(new Error("API 500: directory is busy"));
    renderAt("/models");
    await screen.findByText("huggingface");

    await user.click(screen.getByLabelText("Clean the pip cache"));
    await user.click(screen.getByRole("button", { name: "Clean" }));

    expect(await screen.findByText("API 500: directory is busy")).toBeInTheDocument();
  });

  it("says the cache is empty rather than showing an empty grid", async () => {
    vi.mocked(fetchCache).mockResolvedValue({ entries: [] });
    renderAt("/models");

    expect(await screen.findByText("No cache entries found.")).toBeInTheDocument();
  });

  it("surfaces a failed load instead of an empty section", async () => {
    vi.mocked(fetchCache).mockRejectedValue(new Error("API 503: cache unavailable"));
    renderAt("/models");

    expect(await screen.findByText("API 503: cache unavailable")).toBeInTheDocument();
  });
});
