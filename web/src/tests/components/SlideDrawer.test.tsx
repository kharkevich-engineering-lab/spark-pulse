/** SlideDrawer: the panel the deploy form and every editor live in.
 *
 * The interesting property is the one about nesting. A confirm dialog opened
 * from inside the drawer handles its own Escape; if the drawer also closed on
 * that key, cancelling "discard your changes?" would discard them anyway by
 * taking the whole form away.
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import SlideDrawer from "@/components/SlideDrawer";

function renderDrawer(props: Partial<React.ComponentProps<typeof SlideDrawer>> = {}) {
  return render(
    <SlideDrawer
      open
      onClose={vi.fn()}
      header={<h3>Qwen3-8B</h3>}
      actions={<button>Deploy</button>}
      {...props}
    >
      <p>drawer body</p>
    </SlideDrawer>,
  );
}

describe("SlideDrawer", () => {
  it("renders nothing while closed", () => {
    renderDrawer({ open: false });
    expect(screen.queryByText("drawer body")).not.toBeInTheDocument();
  });

  it("shows its header, actions and content when open", () => {
    renderDrawer();
    expect(screen.getByRole("heading", { name: "Qwen3-8B" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deploy" })).toBeInTheDocument();
    expect(screen.getByText("drawer body")).toBeInTheDocument();
  });

  it("closes on Escape", () => {
    const onClose = vi.fn();
    renderDrawer({ onClose });

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("ignores other keys", () => {
    const onClose = vi.fn();
    renderDrawer({ onClose });

    fireEvent.keyDown(window, { key: "a" });
    expect(onClose).not.toHaveBeenCalled();
  });

  /** The nesting rule: a confirm modal owns Escape while it is up. */
  it("leaves Escape to a confirm dialog opened inside it", () => {
    const onClose = vi.fn();
    render(
      <SlideDrawer open onClose={onClose} header={<h3>Recipe</h3>} actions={null}>
        <div data-confirm-modal="true">Discard changes?</div>
      </SlideDrawer>,
    );

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes when the dimmed area beside the panel is clicked", () => {
    const onClose = vi.fn();
    const { container } = renderDrawer({ onClose });

    fireEvent.click(container.firstChild as HTMLElement);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  /** Clicking a field in the form must not close the form. */
  it("stays open when the panel itself is clicked", () => {
    const onClose = vi.fn();
    renderDrawer({ onClose });

    fireEvent.click(screen.getByText("drawer body"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("takes focus when it opens, so Escape reaches it", () => {
    renderDrawer();
    expect(document.activeElement).toBe(screen.getByText("drawer body").closest(".outline-none"));
  });

  it("stops listening for Escape once it is closed", () => {
    const onClose = vi.fn();
    const { rerender } = renderDrawer({ onClose });

    rerender(
      <SlideDrawer open={false} onClose={onClose} header={<h3>x</h3>} actions={null}>
        <p>drawer body</p>
      </SlideDrawer>,
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });
});
