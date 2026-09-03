import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import RecipeImportPanel from "@/components/RecipeImportPanel";
import { importRecipes, fetchRecipeImportStatus } from "@/lib/api";
import type { RecipeImportResult } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  importRecipes: vi.fn(),
  fetchRecipeImportStatus: vi.fn(),
}));

const mockImport = vi.mocked(importRecipes);
const mockStatus = vi.mocked(fetchRecipeImportStatus);

const RESULT: RecipeImportResult = {
  source: "/home/user/spark-vllm-docker",
  source_url: null,
  ref: null,
  git_sha: "abc123",
  imported_at: "2026-09-03T10:00:00+00:00",
  dest: "/home/user/.config/spark-pulse/imported",
  recipes: [
    { file: "tiny.yaml", id: "imported/tiny", status: "ok", message: "", name: "Tiny", recipe_version: "1" },
    { file: "broken.yaml", id: "imported/broken", status: "error", message: "command: field required" },
  ],
  mods: [{ name: "nemotron-nano", status: "ok", message: "" }],
  counts: {
    recipes: { ok: 1, skipped: 0, error: 1 },
    mods: { ok: 1, skipped: 0, error: 0 },
  },
};

async function openPanel() {
  render(<RecipeImportPanel />);
  await waitFor(() => expect(mockStatus).toHaveBeenCalled());
  fireEvent.click(screen.getByRole("button", { name: /import from upstream/i }));
}

describe("RecipeImportPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockStatus.mockResolvedValue({ imported: false });
  });

  it("is collapsed until the header is clicked", async () => {
    render(<RecipeImportPanel />);
    await waitFor(() => expect(mockStatus).toHaveBeenCalled());
    expect(screen.queryByLabelText(/local checkout path/i)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /import from upstream/i }));
    expect(screen.getByLabelText(/local checkout path/i)).toBeInTheDocument();
  });

  it("disables Import until a path is entered", async () => {
    await openPanel();
    const button = screen.getByRole("button", { name: /^import$/i });
    expect(button).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/local checkout path/i), {
      target: { value: "/home/user/spark-vllm-docker" },
    });
    expect(button).not.toBeDisabled();
  });

  it("posts the path and lists the per-file results", async () => {
    mockImport.mockResolvedValue(RESULT);
    await openPanel();

    fireEvent.change(screen.getByLabelText(/local checkout path/i), {
      target: { value: "/home/user/spark-vllm-docker" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^import$/i }));

    await waitFor(() =>
      expect(mockImport).toHaveBeenCalledWith({ path: "/home/user/spark-vllm-docker" }),
    );
    expect(
      await screen.findByText(/1 recipe imported, 1 failed, 1 mod imported/i),
    ).toBeInTheDocument();
    expect(screen.getByText("tiny.yaml")).toBeInTheDocument();
    expect(screen.getByText(/command: field required/)).toBeInTheDocument();
    expect(screen.getByText("mods/nemotron-nano")).toBeInTheDocument();
  });

  it("switches to the git URL form and sends url and ref", async () => {
    mockImport.mockResolvedValue({ ...RESULT, source_url: "https://example.invalid/x.git", ref: "main" });
    await openPanel();

    fireEvent.click(screen.getByRole("button", { name: /git url/i }));
    fireEvent.change(screen.getByLabelText(/git repository url/i), {
      target: { value: "https://example.invalid/x.git" },
    });
    fireEvent.change(screen.getByLabelText(/git ref/i), { target: { value: "main" } });
    fireEvent.click(screen.getByRole("button", { name: /^import$/i }));

    await waitFor(() =>
      expect(mockImport).toHaveBeenCalledWith({
        url: "https://example.invalid/x.git",
        ref: "main",
      }),
    );
  });

  it("shows the error returned by the API", async () => {
    mockImport.mockRejectedValue(new Error("API 404: not a directory"));
    await openPanel();

    fireEvent.change(screen.getByLabelText(/local checkout path/i), {
      target: { value: "/nope" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^import$/i }));

    expect(await screen.findByText(/API 404: not a directory/)).toBeInTheDocument();
  });

  it("shows when the last import happened", async () => {
    mockStatus.mockResolvedValue({ imported: true, ...RESULT });
    render(<RecipeImportPanel />);
    expect(await screen.findByText(/last imported 2026-09-03/)).toBeInTheDocument();
  });
});
