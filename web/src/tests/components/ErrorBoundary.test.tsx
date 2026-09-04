/** ErrorBoundary: what an operator sees when a page throws.
 *
 * The e2e suite checks for "Something went wrong" as its crash detector, so
 * the wording here is load-bearing. The other property worth holding is that
 * "Try Again" actually re-renders the subtree rather than only clearing the
 * message — a boundary that cannot recover is a reload button with extra
 * steps.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { DefaultErrorFallback, ErrorBoundary } from "@/components/ErrorBoundary";

function Boom({ throws }: { throws: boolean }): React.ReactElement {
  if (throws) throw new Error("recipes exploded");
  return <p>the page</p>;
}

describe("ErrorBoundary", () => {
  beforeEach(() => {
    // React logs the caught error itself; the boundary logs it again. Neither
    // is a test failure, but both make the output unreadable.
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders its children while nothing is wrong", () => {
    render(
      <ErrorBoundary>
        <Boom throws={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("the page")).toBeInTheDocument();
  });

  it("shows the failure's message instead of a blank screen", () => {
    render(
      <ErrorBoundary>
        <Boom throws />
      </ErrorBoundary>,
    );

    expect(screen.getByRole("heading", { name: "Something went wrong" })).toBeInTheDocument();
    expect(screen.getByText("recipes exploded")).toBeInTheDocument();
  });

  it("falls back to a generic line when the failure carries no message", () => {
    function Empty(): React.ReactElement {
      throw new Error("");
    }
    render(
      <ErrorBoundary>
        <Empty />
      </ErrorBoundary>,
    );
    expect(screen.getByText("An unexpected error occurred")).toBeInTheDocument();
  });

  it("tells the caller what it caught, so a page can report it", () => {
    const onError = vi.fn();
    render(
      <ErrorBoundary onError={onError}>
        <Boom throws />
      </ErrorBoundary>,
    );

    expect(onError).toHaveBeenCalledTimes(1);
    expect((onError.mock.calls[0][0] as Error).message).toBe("recipes exploded");
  });

  it("renders a caller's own fallback in place of the built-in one", () => {
    render(
      <ErrorBoundary fallback={<p>this page is having a lie down</p>}>
        <Boom throws />
      </ErrorBoundary>,
    );

    expect(screen.getByText("this page is having a lie down")).toBeInTheDocument();
    expect(screen.queryByText("Something went wrong")).not.toBeInTheDocument();
  });

  /** Recovery, not just dismissal: after Try Again the subtree renders for
   *  real, which is only visible if the child has stopped throwing. */
  it("re-renders the subtree when Try Again is pressed", () => {
    const { rerender } = render(
      <ErrorBoundary>
        <Boom throws />
      </ErrorBoundary>,
    );
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();

    rerender(
      <ErrorBoundary>
        <Boom throws={false} />
      </ErrorBoundary>,
    );
    fireEvent.click(screen.getByRole("button", { name: /Try Again/ }));

    expect(screen.getByText("the page")).toBeInTheDocument();
    expect(screen.queryByText("Something went wrong")).not.toBeInTheDocument();
  });
});

describe("DefaultErrorFallback", () => {
  it("offers a reload as the last resort", () => {
    const reload = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, reload },
    });

    render(<DefaultErrorFallback />);
    expect(screen.getByRole("heading", { name: "Something went wrong" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Refresh Page/ }));
    expect(reload).toHaveBeenCalledTimes(1);
  });
});
