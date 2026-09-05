/** ConfirmModal and AlertModal — the dialogs that stand between an operator
 * and every destructive action in the app.
 *
 * Stopping a deployment, forgetting a node, deleting a custom recipe and
 * cleaning the cache all end at ConfirmModal, so the properties worth holding
 * are the ones that decide whether the right thing happened: cancel must not
 * confirm, a second click while the first is still running must not fire
 * twice, and the dialog must not close underneath an action that is mid-flight.
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AlertModal, ConfirmModal } from "@/components/Modal";

/** A promise plus the handle to settle it, so a test can hold a confirm open. */
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("ConfirmModal", () => {
  it("renders nothing at all while closed", () => {
    render(
      <ConfirmModal
        open={false}
        onClose={vi.fn()}
        onConfirm={vi.fn()}
        title="Stop Deployment"
        message="Stop it?"
      />,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText("Stop it?")).not.toBeInTheDocument();
  });

  it("shows the question and the labelled action when open", () => {
    render(
      <ConfirmModal
        open
        onClose={vi.fn()}
        onConfirm={vi.fn()}
        title="Stop Deployment"
        message='Stop "qwen3-8b"? This will terminate the running process.'
        confirmLabel="Stop"
        confirmVariant="danger"
      />,
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Stop Deployment" })).toBeInTheDocument();
    expect(screen.getByText(/terminate the running process/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it("confirms only when the confirm button is pressed", () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    render(
      <ConfirmModal
        open
        onClose={onClose}
        onConfirm={onConfirm}
        title="Forget node"
        message="Forget it?"
        confirmLabel="Forget"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Forget" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("cancels without confirming", () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    render(
      <ConfirmModal
        open
        onClose={onClose}
        onConfirm={onConfirm}
        title="Forget node"
        message="Forget it?"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("closes from the X in the corner", () => {
    const onClose = vi.fn();
    render(
      <ConfirmModal open onClose={onClose} onConfirm={vi.fn()} title="Stop" message="Stop it?" />,
    );

    fireEvent.click(screen.getByTitle("Close"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes on Escape", () => {
    const onClose = vi.fn();
    render(
      <ConfirmModal open onClose={onClose} onConfirm={vi.fn()} title="Stop" message="Stop it?" />,
    );

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  /** Enter is the shortcut for the action, which means it must not fire while
   *  the operator is typing into a field the dialog is hosting. */
  it("confirms on Enter", () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmModal open onClose={vi.fn()} onConfirm={onConfirm} title="Stop" message="Stop it?" />,
    );

    fireEvent.keyDown(window, { key: "Enter" });
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("does not confirm on Enter pressed inside a text field", () => {
    const onConfirm = vi.fn();
    render(
      <>
        <input aria-label="name" />
        <ConfirmModal
          open
          onClose={vi.fn()}
          onConfirm={onConfirm}
          title="Stop"
          message="Stop it?"
        />
      </>,
    );

    fireEvent.keyDown(screen.getByLabelText("name"), { key: "Enter" });
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("ignores keys while closed", () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    render(
      <ConfirmModal
        open={false}
        onClose={onClose}
        onConfirm={onConfirm}
        title="Stop"
        message="Stop it?"
      />,
    );

    fireEvent.keyDown(window, { key: "Enter" });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  /** Stopping a deployment takes seconds. A second click during that window
   *  used to send a second stop; both buttons go dead until it settles. */
  it("disables both buttons while the confirmed action is still running", async () => {
    const pending = deferred();
    const onConfirm = vi.fn(() => pending.promise);
    const onClose = vi.fn();
    render(
      <ConfirmModal
        open
        onClose={onClose}
        onConfirm={onConfirm}
        title="Stop"
        message="Stop it?"
        confirmLabel="Stop"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled(),
    );
    const confirming = screen.getByRole("button", { name: "..." });
    expect(confirming).toBeDisabled();

    fireEvent.click(confirming);
    fireEvent.keyDown(window, { key: "Enter" });
    expect(onConfirm).toHaveBeenCalledTimes(1);

    pending.resolve();
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled());
  });

  it("cannot be dismissed by the backdrop while the action is running", async () => {
    const pending = deferred();
    const onClose = vi.fn();
    const { container } = render(
      <ConfirmModal
        open
        onClose={onClose}
        onConfirm={() => pending.promise}
        title="Stop"
        message="Stop it?"
        confirmLabel="Stop"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "..." })).toBeDisabled());

    const backdrop = container.querySelector(".absolute.inset-0") as HTMLElement;
    fireEvent.click(backdrop);
    expect(onClose).not.toHaveBeenCalled();

    pending.resolve();
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled());
  });

  it("dismisses on a backdrop click when nothing is running", () => {
    const onClose = vi.fn();
    const { container } = render(
      <ConfirmModal open onClose={onClose} onConfirm={vi.fn()} title="Stop" message="Stop it?" />,
    );

    fireEvent.click(container.querySelector(".absolute.inset-0") as HTMLElement);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  /** Keyboard-first operators should not have to reach for the mouse to
   *  answer a dialog that appeared under their hands. */
  it("puts focus inside the dialog when it opens", () => {
    render(
      <ConfirmModal open onClose={vi.fn()} onConfirm={vi.fn()} title="Stop" message="Stop it?" />,
    );
    expect(screen.getByRole("dialog").contains(document.activeElement)).toBe(true);
  });

  /** A caller that handles its own failure (every current one does — they
   *  catch and raise an AlertModal) must find the dialog re-armed rather than
   *  stuck on "..." forever, so a failed stop can be retried without a
   *  reload. */
  it("re-arms after an action that reported a failure of its own", async () => {
    const onConfirm = vi.fn(async () => {
      // What the pages actually do: catch, then show the message elsewhere.
      try {
        throw new Error("stop failed");
      } catch {
        /* reported by the caller */
      }
    });
    render(
      <ConfirmModal
        open
        onClose={vi.fn()}
        onConfirm={onConfirm}
        title="Stop"
        message="Stop it?"
        confirmLabel="Stop"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled());
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});

describe("AlertModal", () => {
  it("renders nothing while closed", () => {
    render(<AlertModal open={false} onClose={vi.fn()} title="Error" message="boom" />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows the title and the message it was handed", () => {
    render(
      <AlertModal
        open
        onClose={vi.fn()}
        title="Error"
        message="API 400: node(s) 10.99.99.99 are not in the node registry"
      />,
    );
    expect(screen.getByRole("heading", { name: "Error" })).toBeInTheDocument();
    expect(screen.getByText(/not in the node registry/)).toBeInTheDocument();
  });

  it("dismisses from OK, from the X and from Escape", () => {
    const onClose = vi.fn();
    const { unmount } = render(
      <AlertModal open onClose={onClose} title="Error" message="boom" />,
    );
    fireEvent.click(screen.getByRole("button", { name: "OK" }));
    fireEvent.click(screen.getByTitle("Close"));
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(3);
    unmount();
  });

  /** Backend errors arrive with newlines in them — a stack tail, a list of
   *  nodes. Collapsing them makes the message unreadable at the moment it
   *  matters most. */
  it("keeps the line breaks of a multi-line backend error", () => {
    render(<AlertModal open onClose={vi.fn()} title="Error" message={"line one\nline two"} />);
    expect(screen.getByText(/line one/)).toHaveClass("whitespace-pre-wrap");
  });
});
