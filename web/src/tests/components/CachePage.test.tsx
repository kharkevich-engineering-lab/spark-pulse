/** The Cache Manager: what is on disk, and the one button that deletes it.
 *
 * Cleaning a cache is irreversible and re-downloading a model catalogue is an
 * afternoon, so the properties worth holding are that the confirmation names
 * *which* cache is about to go (the "all" wording is deliberately different
 * from a single entry's), that nothing is deleted until it is confirmed, and
 * that a failed clean says so instead of leaving the operator to guess whether
 * it worked.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CachePage from "@/pages/CachePage";
import type { CacheEntry } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchCache: vi.fn(),
  cleanCache: vi.fn(),
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

describe("CachePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchCache).mockResolvedValue({ entries: ENTRIES });
    vi.mocked(cleanCache).mockResolvedValue({ huggingface: "cleaned" });
  });

  it("totals what is on disk, and lists every cache with its path and size", async () => {
    render(<CachePage />);

    // 25 GB + 1 GB, so the header total is not just the first entry's size.
    expect(await screen.findByText("26.0 GB")).toBeInTheDocument();
    expect(screen.getByText("huggingface")).toBeInTheDocument();
    expect(screen.getByText("/home/spark/.cache/huggingface")).toBeInTheDocument();
    expect(screen.getByText("25.0 GB")).toBeInTheDocument();
    expect(screen.getByText("412 files")).toBeInTheDocument();
    // One file is one file, not "1 files".
    expect(screen.getByText("1 file")).toBeInTheDocument();
  });

  it("names the single cache it is about to delete, and deletes nothing until confirmed", async () => {
    const user = userEvent.setup();
    render(<CachePage />);
    await screen.findByText("huggingface");

    await user.click(screen.getAllByTitle("Clean cache")[0]);
    expect(await screen.findByText(/Clean cache "huggingface"\?/)).toBeInTheDocument();
    expect(cleanCache).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Clean" }));
    await waitFor(() => expect(cleanCache).toHaveBeenCalledWith(["huggingface"]));
  });

  it("warns in different words when the button would take every cache", async () => {
    const user = userEvent.setup();
    render(<CachePage />);
    await screen.findByText("huggingface");

    await user.click(screen.getByRole("button", { name: /clean all/i }));

    expect(await screen.findByRole("heading", { name: "Clean All Caches" })).toBeInTheDocument();
    expect(screen.getByText(/This will clean ALL caches/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clean" }));
    await waitFor(() => expect(cleanCache).toHaveBeenCalledWith(["all"]));
  });

  it("backs out without deleting when the confirmation is dismissed", async () => {
    const user = userEvent.setup();
    render(<CachePage />);
    await screen.findByText("huggingface");

    await user.click(screen.getByRole("button", { name: /clean all/i }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(cleanCache).not.toHaveBeenCalled();
  });

  it("says why a clean failed rather than silently leaving the cache in place", async () => {
    const user = userEvent.setup();
    vi.mocked(cleanCache).mockRejectedValue(new Error("API 500: permission denied"));
    render(<CachePage />);
    await screen.findByText("huggingface");

    await user.click(screen.getByRole("button", { name: /clean all/i }));
    await user.click(screen.getByRole("button", { name: "Clean" }));

    expect(await screen.findByText("API 500: permission denied")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "OK" }));
    await waitFor(() => expect(screen.queryByText("API 500: permission denied")).toBeNull());
  });

  it("says the cache is empty rather than showing an empty grid", async () => {
    vi.mocked(fetchCache).mockResolvedValue({ entries: [] });
    render(<CachePage />);

    expect(await screen.findByText("No cache entries found.")).toBeInTheDocument();
    expect(screen.getByText("0 B")).toBeInTheDocument();
  });

  it("surfaces a failed load instead of an empty page", async () => {
    vi.mocked(fetchCache).mockRejectedValue(new Error("API 503: cache unavailable"));
    render(<CachePage />);

    expect(await screen.findByText("API 503: cache unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /clean all/i })).toBeNull();
  });
});
