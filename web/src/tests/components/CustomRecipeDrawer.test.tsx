/** The custom-recipe editor: the YAML it loads, and what it refuses to save.
 *
 * A custom recipe is a file on disk that nothing else backs up, so the two
 * properties worth holding are that the drawer loads the file's real content
 * before offering to overwrite it, and that Delete asks first.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CustomRecipeDrawer from "@/components/CustomRecipeDrawer";
import type { CustomRecipeInfo } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  getCustomRecipeContent: vi.fn(),
}));

/** A plain textarea stands in for the lazily-imported code editor: same
 *  value/onChange contract, no dynamic import to await. */
vi.mock("@/components/LazyCodeEditor", () => ({
  default: ({
    value,
    onChange,
    disabled,
  }: {
    value: string;
    onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
    disabled?: boolean;
  }) => (
    <textarea aria-label="Recipe YAML" value={value} onChange={onChange} disabled={disabled} />
  ),
}));

import { getCustomRecipeContent } from "@/lib/api";

const RECIPE: CustomRecipeInfo = {
  id: "custom/my-recipe",
  name: "My Recipe",
  filename: "my-recipe.yaml",
  filepath: "/home/spark/.config/spark-pulse/recipes/my-recipe.yaml",
  created_at: 1_700_000_000,
};

const YAML = "name: My Recipe\nmodel: Qwen/Qwen3-8B\n";

function renderDrawer(props: Partial<React.ComponentProps<typeof CustomRecipeDrawer>> = {}) {
  const onClose = vi.fn();
  const onSave = vi.fn().mockResolvedValue(undefined);
  const onDelete = vi.fn().mockResolvedValue(undefined);
  const onError = vi.fn();
  const result = render(
    <CustomRecipeDrawer
      open
      recipe={RECIPE}
      onClose={onClose}
      onSave={onSave}
      onDelete={onDelete}
      onError={onError}
      {...props}
    />,
  );
  return { ...result, onClose, onSave, onDelete, onError };
}

describe("CustomRecipeDrawer", () => {
  beforeEach(() => {
    vi.mocked(getCustomRecipeContent).mockResolvedValue({ content: YAML, id: RECIPE.id });
  });

  it("renders nothing at all when it is closed or has no recipe", () => {
    const { container, unmount } = renderDrawer({ open: false });
    expect(container).toBeEmptyDOMElement();
    unmount();

    const bare = renderDrawer({ recipe: null });
    expect(bare.container).toBeEmptyDOMElement();
  });

  it("names the recipe and the file it edits", async () => {
    renderDrawer();
    expect(screen.getByRole("heading", { name: "My Recipe" })).toBeInTheDocument();
    expect(screen.getByText("my-recipe.yaml")).toBeInTheDocument();
    await waitFor(() => expect(getCustomRecipeContent).toHaveBeenCalledWith(RECIPE.id));
  });

  it("loads the file's YAML into the editor", async () => {
    renderDrawer();
    await waitFor(() =>
      expect(screen.getByLabelText("Recipe YAML")).toHaveValue(YAML),
    );
  });

  /** Saving a file the drawer failed to read would truncate it, so the error
   *  reaches the page and the editor stays empty and unsaveable. */
  it("reports a file it could not read, and refuses to save over it", async () => {
    vi.mocked(getCustomRecipeContent).mockRejectedValue(new Error("permission denied"));
    const { onError } = renderDrawer();

    await waitFor(() => expect(onError).toHaveBeenCalledWith("permission denied"));
    expect(screen.getByRole("button", { name: /Save/ })).toBeDisabled();
  });

  it("saves the edited YAML and closes", async () => {
    const { onSave, onClose } = renderDrawer();
    await waitFor(() => expect(screen.getByLabelText("Recipe YAML")).toHaveValue(YAML));

    const editor = screen.getByLabelText("Recipe YAML");
    await userEvent.clear(editor);
    await userEvent.type(editor, "name: Edited");
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith(RECIPE.id, "name: Edited"));
    expect(onClose).toHaveBeenCalled();
  });

  it("keeps the drawer open and reports a save that failed", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("disk full"));
    const { onError, onClose } = renderDrawer({ onSave });
    await waitFor(() => expect(screen.getByLabelText("Recipe YAML")).toHaveValue(YAML));

    await userEvent.click(screen.getByRole("button", { name: /Save/ }));

    await waitFor(() => expect(onError).toHaveBeenCalledWith("disk full"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("asks before deleting, and names what it would delete", async () => {
    const { onDelete } = renderDrawer();
    await waitFor(() => expect(screen.getByLabelText("Recipe YAML")).toHaveValue(YAML));

    await userEvent.click(screen.getByRole("button", { name: /Delete/ }));
    expect(screen.getByText(/Delete "My Recipe"\? This cannot be undone\./)).toBeInTheDocument();
    expect(onDelete).not.toHaveBeenCalled();

    await userEvent.click(screen.getAllByRole("button", { name: "Delete" }).at(-1)!);
    await waitFor(() => expect(onDelete).toHaveBeenCalledWith(RECIPE.id));
  });

  it("reports a delete that failed", async () => {
    const onDelete = vi.fn().mockRejectedValue(new Error("file is read-only"));
    const { onError } = renderDrawer({ onDelete });
    await waitFor(() => expect(screen.getByLabelText("Recipe YAML")).toHaveValue(YAML));

    await userEvent.click(screen.getByRole("button", { name: /Delete/ }));
    await userEvent.click(screen.getAllByRole("button", { name: "Delete" }).at(-1)!);

    await waitFor(() => expect(onError).toHaveBeenCalledWith("file is read-only"));
  });

  /** A 1 MB cap is the difference between a recipe and someone's model
   *  weights; over it the drawer says so rather than pasting it in. */
  it("refuses a file larger than a recipe could plausibly be", async () => {
    const { onError } = renderDrawer();
    await waitFor(() => expect(screen.getByLabelText("Recipe YAML")).toHaveValue(YAML));

    const huge = new File(["x".repeat(2 * 1024 * 1024)], "big.yaml", { type: "text/yaml" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, huge);

    await waitFor(() => expect(onError).toHaveBeenCalledWith("File too large (max 1MB)"));
    expect(screen.getByLabelText("Recipe YAML")).toHaveValue(YAML);
  });

  it("replaces the editor's content with an uploaded file", async () => {
    renderDrawer();
    await waitFor(() => expect(screen.getByLabelText("Recipe YAML")).toHaveValue(YAML));

    const uploaded = new File(["name: From File\n"], "other.yaml", { type: "text/yaml" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, uploaded);

    await waitFor(() =>
      expect(screen.getByLabelText("Recipe YAML")).toHaveValue("name: From File\n"),
    );
  });
});
