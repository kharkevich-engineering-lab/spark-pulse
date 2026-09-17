/** "Which machines?" — the one dialog three used to ask separately.
 *
 * The preselection is the part that carries the meaning, and it is the part
 * the three copies disagreed on. **Delete** starts on the nodes that hold a
 * copy, because a node without one has no disk to reclaim. **Replicate**
 * starts on the nodes that do not, because a node that already has it has
 * nothing to gain from a transfer. And once the operator has touched a box,
 * an answer arriving late must not move it back.
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NodeScopedDialog } from "@/ui";

const PRESENCE = {
  nodes: [
    { node: "spark-02", present: true },
    { node: "spark-03", present: false },
  ],
};

const base = {
  title: "Remove the model",
  body: "acme/qwen3-8b",
  legend: "Also remove from",
  nodes: ["spark-02", "spark-03"],
  confirmLabel: "Remove",
  onClose: vi.fn(),
};

describe("NodeScopedDialog", () => {
  it("preselects the nodes that hold a copy when deleting", async () => {
    const onConfirm = vi.fn();
    render(
      <NodeScopedDialog
        {...base}
        verb="delete"
        fetchPresence={() => Promise.resolve(PRESENCE)}
        onConfirm={onConfirm}
      />,
    );

    await waitFor(() => expect(screen.getByLabelText(/spark-02/)).toBeChecked());
    expect(screen.getByLabelText(/spark-03/)).not.toBeChecked();

    await userEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(onConfirm).toHaveBeenCalledWith(["spark-02"], { force: false });
  });

  it("preselects the nodes that do not, when replicating", async () => {
    const onConfirm = vi.fn();
    render(
      <NodeScopedDialog
        {...base}
        verb="replicate"
        confirmLabel="Replicate"
        fetchPresence={() => Promise.resolve(PRESENCE)}
        onConfirm={onConfirm}
      />,
    );

    await waitFor(() => expect(screen.getByLabelText(/spark-03/)).toBeChecked());
    expect(screen.getByLabelText(/spark-02/)).not.toBeChecked();
  });

  /** A late answer must not undo a decision the operator has already made. */
  it("leaves a box the operator has touched alone", async () => {
    let settle: (value: typeof PRESENCE) => void = () => {};
    const pending = new Promise<typeof PRESENCE>((resolve) => {
      settle = resolve;
    });
    render(
      <NodeScopedDialog {...base} verb="delete" fetchPresence={() => pending} onConfirm={vi.fn()} />,
    );

    expect(screen.getByText("Loading…")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText(/spark-03/));
    settle(PRESENCE);

    await waitFor(() => expect(screen.queryByText("Loading…")).toBeNull());
    expect(screen.getByLabelText(/spark-03/)).toBeChecked();
    expect(screen.getByLabelText(/spark-02/)).not.toBeChecked();
  });

  /** Engines already holds the answer and must not send a second request for
   *  it, and until a node has answered it offers every peer. */
  it("takes a presence the caller already has, and a fallback selection", () => {
    render(
      <NodeScopedDialog
        {...base}
        verb="delete"
        presence="loading"
        selectionWhenUnknown={base.nodes}
        onConfirm={vi.fn()}
      />,
    );

    expect(screen.getByLabelText(/spark-02/)).toBeChecked();
    expect(screen.getByLabelText(/spark-03/)).toBeChecked();
  });

  it("notes what each node holds", async () => {
    render(
      <NodeScopedDialog
        {...base}
        verb="delete"
        fetchPresence={() => Promise.resolve(PRESENCE)}
        noteFor={(_node, holds) => (holds === false ? <span>not there</span> : null)}
        onConfirm={vi.fn()}
      />,
    );

    expect(await screen.findByText("not there")).toBeInTheDocument();
  });

  it("offers force only when the caller asks, and passes it on", async () => {
    const onConfirm = vi.fn();
    render(
      <NodeScopedDialog
        {...base}
        verb="replicate"
        confirmLabel="Replicate"
        forceLabel="Re-transfer even where presence says it is there"
        fetchPresence={() => Promise.resolve(PRESENCE)}
        onConfirm={onConfirm}
      />,
    );

    await screen.findByLabelText(/spark-03/);
    await userEvent.click(screen.getByLabelText(/Re-transfer/));
    await userEvent.click(screen.getByRole("button", { name: "Replicate" }));

    expect(onConfirm).toHaveBeenCalledWith(["spark-03"], { force: true });
  });

  it("re-asks presence once the action has resolved", async () => {
    const fetchPresence = vi.fn().mockResolvedValue(PRESENCE);
    render(
      <NodeScopedDialog
        {...base}
        verb="delete"
        fetchPresence={fetchPresence}
        onConfirm={() => Promise.resolve()}
      />,
    );

    await waitFor(() => expect(fetchPresence).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(fetchPresence).toHaveBeenCalledTimes(2));
  });

  /** Removing from this machine alone is "Delete"; removing from four is
   *  something the operator should be made to read. */
  it("lets the confirm word depend on what is selected", async () => {
    render(
      <NodeScopedDialog
        {...base}
        verb="delete"
        confirmLabel={(selected) => (selected.length > 0 ? "Delete everywhere" : "Delete")}
        fetchPresence={() => Promise.resolve(PRESENCE)}
        onConfirm={vi.fn()}
      />,
    );

    expect(await screen.findByRole("button", { name: "Delete everywhere" })).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText(/spark-02/));
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });

  it("refuses an empty selection where one is required", async () => {
    render(
      <NodeScopedDialog
        {...base}
        verb="replicate"
        confirmLabel="Replicate"
        requireSelection
        presence={PRESENCE}
        onConfirm={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByLabelText(/spark-03/));
    expect(screen.getByRole("button", { name: "Replicate" })).toBeDisabled();
  });

  it("shows one red line when the action failed, not a banner", () => {
    render(
      <NodeScopedDialog {...base} verb="replicate" error="rsync exited 23" onConfirm={vi.fn()} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("rsync exited 23");
  });

  /** A single-machine install has nothing to ask, and asking anyway is how a
   *  confirm turns into a form with no fields. */
  it("asks nothing of a single-machine install", () => {
    render(<NodeScopedDialog {...base} verb="delete" nodes={[]} onConfirm={vi.fn()} />);
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByText("Also remove from")).toBeNull();
  });

  it("survives a presence query that fails", async () => {
    render(
      <NodeScopedDialog
        {...base}
        verb="delete"
        fetchPresence={() => Promise.reject(new Error("no route to host"))}
        onConfirm={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.queryByText("Loading…")).toBeNull());
    expect(screen.getByLabelText(/spark-02/)).not.toBeChecked();
  });

  it("closes without acting", async () => {
    const onClose = vi.fn();
    const onConfirm = vi.fn();
    render(
      <NodeScopedDialog
        {...base}
        verb="delete"
        onClose={onClose}
        presence={PRESENCE}
        onConfirm={onConfirm}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
