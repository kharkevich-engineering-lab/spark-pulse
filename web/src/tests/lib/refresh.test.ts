/** The global refresh registry behind the shell's refresh button.
 *
 * One slot, last writer wins: every page registers its own refetch on mount,
 * so the button always reloads the page the operator is looking at.
 */

import { describe, it, expect, vi } from "vitest";
import { doRefresh, setRefresh } from "@/lib/refresh";

describe("refresh registry", () => {
  it("calls whatever the current page registered", () => {
    const refetch = vi.fn();
    setRefresh(refetch);

    doRefresh();

    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it("replaces the previous page's refetch rather than calling both", () => {
    const first = vi.fn();
    const second = vi.fn();
    setRefresh(first);
    setRefresh(second);

    doRefresh();

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  // The button lives in the shell and is clickable before any page has
  // registered, so a refresh with nothing registered has to be a no-op.
  it("does nothing, rather than throwing, when no page has registered", () => {
    setRefresh(undefined as unknown as () => void);
    expect(() => doRefresh()).not.toThrow();
  });
});
