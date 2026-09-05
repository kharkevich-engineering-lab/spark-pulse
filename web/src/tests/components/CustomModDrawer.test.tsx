/** The custom-mod editor: several files behind one Save.
 *
 * A mod is a directory, not a file, so the drawer edits a map. What matters
 * is that switching files does not lose the edits made to the previous one —
 * one Save writes every file — and that `run.sh`, the file the runtime
 * actually executes, is the one opened first.
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CustomModDrawer from "@/components/CustomModDrawer";
import type { CustomModInfo, ModFileMap } from "@/lib/types";

const MOD: CustomModInfo = {
  id: "custom/my-mod",
  name: "My Mod",
  description: "Patches the attention kernel",
  filepath: "/home/spark/.config/spark-pulse/mods/my-mod",
  has_run_sh: true,
};

const FILES: ModFileMap = {
  "run.sh": "#!/bin/bash\necho hello\n",
  "patch.diff": "--- a\n+++ b\n",
};

function renderDrawer(props: Partial<React.ComponentProps<typeof CustomModDrawer>> = {}) {
  const onClose = vi.fn();
  const onSave = vi.fn().mockResolvedValue(undefined);
  const onDelete = vi.fn().mockResolvedValue(undefined);
  const onError = vi.fn();
  const result = render(
    <CustomModDrawer
      open
      mod={MOD}
      files={FILES}
      onClose={onClose}
      onSave={onSave}
      onDelete={onDelete}
      onError={onError}
      {...props}
    />,
  );
  return { ...result, onClose, onSave, onDelete, onError };
}

describe("CustomModDrawer", () => {
  it("renders nothing when closed or when there is no mod", () => {
    const { container, unmount } = renderDrawer({ open: false });
    expect(container).toBeEmptyDOMElement();
    unmount();

    const bare = renderDrawer({ mod: null });
    expect(bare.container).toBeEmptyDOMElement();
  });

  it("names the mod, its description and every file it holds", () => {
    renderDrawer();
    expect(screen.getByRole("heading", { name: "My Mod" })).toBeInTheDocument();
    expect(screen.getByText("Patches the attention kernel")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "run.sh" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "patch.diff" })).toBeInTheDocument();
  });

  /** `run.sh` is what the runtime executes, so it is the file the operator
   *  almost always came to edit — even though it sorts after `patch.diff`. */
  it("opens run.sh first whatever the alphabet says", () => {
    renderDrawer();
    expect(screen.getByRole("textbox")).toHaveValue("#!/bin/bash\necho hello\n");
  });

  it("falls back to the first file when there is no run.sh", () => {
    renderDrawer({ files: { "setup.py": "print(1)" } });
    expect(screen.getByRole("textbox")).toHaveValue("print(1)");
  });

  it("says there is nothing to edit rather than showing an empty editor", () => {
    renderDrawer({ files: {} });
    expect(screen.getByText("No files")).toBeInTheDocument();
    expect(screen.getByText("Select a file to edit")).toBeInTheDocument();
  });

  it("switches to the file the operator picks", async () => {
    renderDrawer();
    await userEvent.click(screen.getByRole("button", { name: "patch.diff" }));
    expect(screen.getByRole("textbox")).toHaveValue("--- a\n+++ b\n");
  });

  /** One Save writes the whole mod, so an edit made before switching files
   *  has to survive the switch — losing it silently is the bug this guards. */
  it("keeps edits to a file the operator navigated away from", async () => {
    const { onSave } = renderDrawer();

    const editor = screen.getByRole("textbox");
    await userEvent.clear(editor);
    await userEvent.type(editor, "#!/bin/sh");
    await userEvent.click(screen.getByRole("button", { name: "patch.diff" }));
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(MOD.id, {
        "run.sh": "#!/bin/sh",
        "patch.diff": "--- a\n+++ b\n",
      }),
    );
  });

  it("closes once the save lands", async () => {
    const { onClose } = renderDrawer();
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("stays open and reports a save that failed", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("read-only mount"));
    const { onError, onClose } = renderDrawer({ onSave });

    await userEvent.click(screen.getByRole("button", { name: /Save/ }));

    await waitFor(() => expect(onError).toHaveBeenCalledWith("read-only mount"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("asks before deleting the mod, and names it", async () => {
    const { onDelete } = renderDrawer();

    await userEvent.click(screen.getByRole("button", { name: /Delete/ }));
    expect(screen.getByText(/Delete "My Mod"\? This cannot be undone\./)).toBeInTheDocument();
    expect(onDelete).not.toHaveBeenCalled();

    await userEvent.click(screen.getAllByRole("button", { name: "Delete" }).at(-1)!);
    await waitFor(() => expect(onDelete).toHaveBeenCalledWith(MOD.id));
  });

  it("reports a delete that failed", async () => {
    const onDelete = vi.fn().mockRejectedValue(new Error("mod is in use"));
    const { onError } = renderDrawer({ onDelete });

    await userEvent.click(screen.getByRole("button", { name: /Delete/ }));
    await userEvent.click(screen.getAllByRole("button", { name: "Delete" }).at(-1)!);

    await waitFor(() => expect(onError).toHaveBeenCalledWith("mod is in use"));
  });
});
