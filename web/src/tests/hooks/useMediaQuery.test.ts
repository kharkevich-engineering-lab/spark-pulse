/** Which layout the viewport is in.
 *
 * A table and a list of cards are different markup, not two skins of one
 * layout, so the component renders one or the other rather than hiding a
 * second copy with CSS — which is what would put two buttons named "Remove
 * acme/plain-7b" in the DOM. The hook is what decides, so the two properties
 * worth pinning are that it answers before the first paint and that it
 * changes its mind when the window does.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useMediaQuery, useWideLayout } from "@/hooks/useMediaQuery";

const original = window.matchMedia;

afterEach(() => {
  window.matchMedia = original;
});

/** A `MediaQueryList` whose answer this test controls. */
function stub(initial: boolean) {
  const listeners = new Set<() => void>();
  const mql = {
    matches: initial,
    media: "",
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  };
  window.matchMedia = vi.fn(() => mql) as unknown as typeof window.matchMedia;
  return {
    set(next: boolean) {
      mql.matches = next;
      listeners.forEach((fn) => fn());
    },
    get listenerCount() {
      return listeners.size;
    },
  };
}

describe("useMediaQuery", () => {
  it("answers from the first render, not after an effect", () => {
    stub(true);
    const { result } = renderHook(() => useMediaQuery("(min-width: 900px)"));

    expect(result.current).toBe(true);
  });

  it("follows the window as it is resized", () => {
    const media = stub(false);
    const { result } = renderHook(() => useMediaQuery("(min-width: 900px)"));
    expect(result.current).toBe(false);

    act(() => media.set(true));

    expect(result.current).toBe(true);
  });

  it("stops listening when the component goes", () => {
    const media = stub(true);
    const { unmount } = renderHook(() => useWideLayout());
    expect(media.listenerCount).toBe(1);

    unmount();

    expect(media.listenerCount).toBe(0);
  });

  /** A test environment without the API is not a phone: assuming the narrow
   *  layout there would hide the table from every renderer that has no
   *  `matchMedia`, so the wide one is what an unanswered question gets. */
  it("assumes the wide layout where nothing can be asked", () => {
    // @ts-expect-error — deliberately removing the API the hook guards for.
    window.matchMedia = undefined;
    const { result } = renderHook(() => useWideLayout());

    expect(result.current).toBe(true);
  });
});
