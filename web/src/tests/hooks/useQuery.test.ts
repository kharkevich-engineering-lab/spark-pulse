/** useQuery: the three states every page renders from, and the rules that keep
 * a poll from flickering or from overwriting itself.
 *
 * Nearly every page in the app is `const { data, loading, error, refetch } =
 * useQuery(fetchSomething)` on a 10–15s interval, so two of these properties
 * are the ones an operator actually feels: a refresh must not blank the list
 * back to a spinner, and a slow response must not land on top of a newer one.
 *
 * Every fetcher below is a stable reference, because `refetch` is memoised on
 * the fetcher's identity and the effect re-runs whenever it changes — which is
 * what every real call site does by passing an imported function.
 */

import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { useQuery } from "@/hooks/useQuery";

/** A promise plus the handles to settle it, so a test can control ordering. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("useQuery", () => {
  it("starts loading with nothing to show", () => {
    const never = () => new Promise<string[]>(() => {});
    const { result } = renderHook(() => useQuery(never));

    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it("hands back what the fetcher resolved and stops loading", async () => {
    const fetcher = async () => ["a", "b"];
    const { result } = renderHook(() => useQuery(fetcher));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual(["a", "b"]);
    expect(result.current.error).toBeNull();
  });

  it("surfaces the failure's message rather than the failure object", async () => {
    const fetcher = async () => {
      throw new Error("API 500: internal server error");
    };
    const { result } = renderHook(() => useQuery(fetcher));

    await waitFor(() => expect(result.current.error).toBe("API 500: internal server error"));
    expect(result.current.loading).toBe(false);
    expect(result.current.data).toBeNull();
  });

  it("fetches once on mount, not once per render", async () => {
    const fetcher = vi.fn(async () => "once");
    const { result, rerender } = renderHook(() => useQuery(fetcher));

    await waitFor(() => expect(result.current.data).toBe("once"));
    rerender();
    rerender();

    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("re-runs the fetcher on refetch", async () => {
    let calls = 0;
    const fetcher = vi.fn(async () => ++calls);
    const { result } = renderHook(() => useQuery(fetcher));

    await waitFor(() => expect(result.current.data).toBe(1));

    await act(async () => {
      result.current.refetch();
    });

    await waitFor(() => expect(result.current.data).toBe(2));
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  /** The polling property. Once a page has a list, a background refresh must
   *  not swap it for a spinner every ten seconds — the list stays on screen
   *  and is replaced in place. */
  it("does not go back to loading once it has data", async () => {
    const pending = deferred<string[]>();
    const fetcher = vi
      .fn<() => Promise<string[]>>()
      .mockResolvedValueOnce(["a"])
      .mockImplementation(() => pending.promise);
    const { result } = renderHook(() => useQuery(fetcher));

    await waitFor(() => expect(result.current.data).toEqual(["a"]));

    await act(async () => {
      result.current.refetch();
    });

    expect(result.current.loading).toBe(false);
    expect(result.current.data).toEqual(["a"]);

    await act(async () => {
      pending.resolve(["a", "b"]);
      await pending.promise;
    });
    expect(result.current.data).toEqual(["a", "b"]);
  });

  /** A refetch that overtakes a slow one must win. Otherwise a stop that
   *  finished fast is undone on screen by the list request it raced. */
  it("discards a superseded response that arrives late", async () => {
    const slow = deferred<string[]>();
    const fetcher = vi
      .fn<() => Promise<string[]>>()
      .mockImplementationOnce(() => slow.promise)
      .mockResolvedValue(["fresh"]);
    const { result } = renderHook(() => useQuery(fetcher));

    await act(async () => {
      result.current.refetch();
    });
    await waitFor(() => expect(result.current.data).toEqual(["fresh"]));

    await act(async () => {
      slow.resolve(["stale"]);
      await slow.promise;
    });

    expect(result.current.data).toEqual(["fresh"]);
  });

  /** Same rule for a failure: the aborted request's rejection is not the
   *  operator's problem, so it must not paint an error over good data. */
  it("does not report an error from a request that was superseded", async () => {
    const slow = deferred<string[]>();
    const fetcher = vi
      .fn<() => Promise<string[]>>()
      .mockImplementationOnce(() => slow.promise)
      .mockResolvedValue(["fresh"]);
    const { result } = renderHook(() => useQuery(fetcher));

    await act(async () => {
      result.current.refetch();
    });
    await waitFor(() => expect(result.current.data).toEqual(["fresh"]));

    await act(async () => {
      slow.reject(new Error("connection reset"));
      await slow.promise.catch(() => undefined);
    });

    expect(result.current.error).toBeNull();
    expect(result.current.data).toEqual(["fresh"]);
  });

  /** An abort is the app's own doing, not a fault to show an operator. */
  it("stays silent when a fetch rejects with an AbortError", async () => {
    const abort = new Error("aborted");
    abort.name = "AbortError";
    const fetcher = () => Promise.reject(abort);
    const { result } = renderHook(() => useQuery(fetcher));

    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.error).toBeNull();
  });

  it("clears a previous error when a refetch succeeds", async () => {
    const fetcher = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce(new Error("nope"))
      .mockResolvedValue("ok");
    const { result } = renderHook(() => useQuery(fetcher));

    await waitFor(() => expect(result.current.error).toBe("nope"));

    await act(async () => {
      result.current.refetch();
    });

    await waitFor(() => expect(result.current.data).toBe("ok"));
    expect(result.current.error).toBeNull();
  });
});
