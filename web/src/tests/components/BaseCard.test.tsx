/** BaseCard: the clickable tile every catalogue page is built out of.
 *
 * It is a `div` pretending to be a button, so the properties that matter are
 * the ones a real button would give for free — it is reachable by keyboard,
 * it announces itself as a button, and when it is disabled it does none of
 * those things rather than looking dimmed and still firing.
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import BaseCard from "@/components/BaseCard";

describe("BaseCard", () => {
  it("shows the title, subtitle, description and badges it is given", () => {
    render(
      <BaseCard
        icon={<span data-testid="icon" />}
        title="Qwen3-8B"
        subtitle="bundled/qwen3-8b"
        description="A small instruct model."
        badges={<span>vllm</span>}
        onClick={vi.fn()}
      />,
    );

    expect(screen.getByText("Qwen3-8B")).toBeInTheDocument();
    expect(screen.getByText("bundled/qwen3-8b")).toBeInTheDocument();
    expect(screen.getByText("A small instruct model.")).toBeInTheDocument();
    expect(screen.getByText("vllm")).toBeInTheDocument();
    expect(screen.getByTestId("icon")).toBeInTheDocument();
  });

  it("omits the optional parts rather than leaving empty rows", () => {
    render(<BaseCard icon={null} title="Bare" onClick={vi.fn()} />);
    expect(screen.getByText("Bare")).toBeInTheDocument();
    expect(screen.getByRole("button")).toBeInTheDocument();
  });

  it("is a button that opens what it names", () => {
    const onClick = vi.fn();
    render(<BaseCard icon={null} title="Qwen3-8B" onClick={onClick} />);

    fireEvent.click(screen.getByRole("button", { name: /Qwen3-8B/ }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it.each(["Enter", " "])("opens from the keyboard with %s", (key) => {
    const onClick = vi.fn();
    render(<BaseCard icon={null} title="Qwen3-8B" onClick={onClick} />);

    fireEvent.keyDown(screen.getByRole("button"), { key });
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("ignores keys that are not the activation keys", () => {
    const onClick = vi.fn();
    render(<BaseCard icon={null} title="Qwen3-8B" onClick={onClick} />);

    fireEvent.keyDown(screen.getByRole("button"), { key: "a" });
    expect(onClick).not.toHaveBeenCalled();
  });

  /** A cluster-only recipe on a one-node install is shown but not openable.
   *  It must not be a button at all, or a keyboard user tabs into a dead end. */
  it("is not a button, not focusable and not clickable when disabled", () => {
    const onClick = vi.fn();
    const { container } = render(
      <BaseCard icon={null} title="Cluster only" onClick={onClick} disabled />,
    );

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    const card = container.firstChild as HTMLElement;
    expect(card).toHaveAttribute("aria-disabled", "true");
    expect(card).not.toHaveAttribute("tabindex");

    fireEvent.click(card);
    fireEvent.keyDown(card, { key: "Enter" });
    expect(onClick).not.toHaveBeenCalled();
  });

  it("takes an extra className from the page that placed it", () => {
    const { container } = render(
      <BaseCard icon={null} title="x" onClick={vi.fn()} className="col-span-2" />,
    );
    expect(container.firstChild).toHaveClass("col-span-2");
  });
});
