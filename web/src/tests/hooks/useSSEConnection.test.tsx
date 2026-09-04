import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEventStream, useSSEConnection } from "@/hooks/useSSEConnection";
import { useSSEStore } from "@/lib/operationStore";
import { SSEConnectionState } from "@/lib/operations";

class MockEventSource {
  url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
  }

  addEventListener() {}
  removeEventListener() {}
  close() {
    this.readyState = 2;
  }
}

describe("useSSEConnection", () => {
  beforeEach(() => {
    useSSEStore.setState({ connections: new Map() } as never);
    vi.useRealTimers();
  });

  it("does not reconnect when the store updates after the initial connection", () => {
    const EventSourceMock = vi.fn(function (this: MockEventSource, url: string) {
      return new MockEventSource(url);
    });
    vi.stubGlobal("EventSource", EventSourceMock as never);

    const onMessage = vi.fn();
    const { unmount } = renderHook(() =>
      useSSEConnection("/sse/health", onMessage, {
        maxRetries: 3,
        retryDelayMs: 1000,
      })
    );

    expect(EventSourceMock).toHaveBeenCalledTimes(1);

    act(() => {
      useSSEStore.getState().updateConnection("/sse/health", {
        state: SSEConnectionState.CONNECTED,
      });
    });

    expect(EventSourceMock).toHaveBeenCalledTimes(1);
    unmount();
  });
});

/** The reconnect path: what an operator's browser does when the stream drops.
 *
 * A live page whose SSE connection dies silently is worse than one that never
 * connected — the deployment list simply stops changing and nothing says so.
 * These drive a fake EventSource through the states a real one goes through,
 * because the backoff, the retry budget and the unmount cleanup are the parts
 * that fail quietly.
 */

/** An EventSource a test can open, feed, drop and close by hand. */
class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static instances: FakeEventSource[] = [];

  url: string;
  readyState: number = FakeEventSource.CONNECTING;
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, ((event: MessageEvent) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  removeEventListener() {}

  close() {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }

  /** The server accepting the connection. */
  open() {
    this.readyState = FakeEventSource.OPEN;
    this.onopen?.();
  }

  /** A named `event:` frame arriving on the wire. */
  emit(type: string, data: string) {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data } as MessageEvent);
    }
  }

  /** An unnamed `data:` frame. */
  message(data: string) {
    this.onmessage?.({ data } as MessageEvent);
  }

  /** The stream dropping: the browser goes back to CONNECTING and fires error. */
  drop() {
    this.readyState = FakeEventSource.CONNECTING;
    this.onerror?.();
  }
}

const latest = () => FakeEventSource.instances[FakeEventSource.instances.length - 1];

describe("useSSEConnection, live", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    useSSEStore.setState({ connections: new Map() } as never);
    vi.stubGlobal("EventSource", FakeEventSource as never);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("opens the stream it was given, and says it is still connecting", () => {
    const { result } = renderHook(() => useSSEConnection("/sse/deployments", vi.fn()));

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(latest().url).toBe("/sse/deployments");
    expect(result.current.state).toBe(SSEConnectionState.RECONNECTING);
  });

  it("reports connected once the stream opens, and forgets the retry count", () => {
    const { result } = renderHook(() => useSSEConnection("/sse/deployments", vi.fn()));

    act(() => latest().open());

    expect(result.current.state).toBe(SSEConnectionState.CONNECTED);
    expect(result.current.reconnect_attempts).toBe(0);
    expect(result.current.last_connected_at).toBeTruthy();
    expect(result.current.error).toBeUndefined();
  });

  it("delivers each named event to the caller, parsed", () => {
    const onMessage = vi.fn();
    renderHook(() => useSSEConnection("/sse/deployments", onMessage));

    act(() => {
      latest().open();
      latest().emit("health", JSON.stringify({ status: "healthy" }));
      latest().emit("event", JSON.stringify({ type: "deployment_start" }));
      latest().emit("log", JSON.stringify({ text: "line" }));
      latest().message(JSON.stringify({ tick: 1 }));
    });

    expect(onMessage).toHaveBeenCalledWith("health", { status: "healthy" });
    expect(onMessage).toHaveBeenCalledWith("event", { type: "deployment_start" });
    expect(onMessage).toHaveBeenCalledWith("log", { text: "line" });
    expect(onMessage).toHaveBeenCalledWith("message", { tick: 1 });
  });

  /** A frame that is not JSON is still a frame. Dropping it would lose the
   *  one log line that explained a crash. */
  it("hands a non-JSON frame through as text rather than dropping it", () => {
    const onMessage = vi.fn();
    renderHook(() => useSSEConnection("/sse/logs/abc", onMessage));

    act(() => {
      latest().emit("health", "not json at all");
      latest().emit("event", "nor this");
      latest().emit("log", "vllm: CUDA error");
      latest().message("also not json");
    });

    expect(onMessage).toHaveBeenCalledWith("health", "not json at all");
    expect(onMessage).toHaveBeenCalledWith("event", "nor this");
    expect(onMessage).toHaveBeenCalledWith("log", "vllm: CUDA error");
    expect(onMessage).toHaveBeenCalledWith("message", "also not json");
  });

  /** The callback is held in a ref, so a page that rebuilds its handler on
   *  every render must not tear the connection down and start again. */
  it("keeps one connection when the caller's handler changes identity", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(
      ({ handler }: { handler: (event: string, data: unknown) => void }) =>
        useSSEConnection("/sse/deployments", handler),
      { initialProps: { handler: first as (event: string, data: unknown) => void } },
    );

    rerender({ handler: second as (event: string, data: unknown) => void });
    act(() => latest().emit("event", JSON.stringify({ type: "x" })));

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledWith("event", { type: "x" });
  });

  it("waits out a backoff before reconnecting, and doubles it each attempt", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() =>
      useSSEConnection("/sse/deployments", vi.fn(), { maxRetries: 5, retryDelayMs: 1000 }),
    );

    act(() => {
      latest().open();
      latest().drop();
    });
    expect(result.current.state).toBe(SSEConnectionState.RECONNECTING);
    expect(result.current.reconnect_attempts).toBe(1);
    expect(result.current.error).toContain("1/5");

    // The first attempt waits a full delay rather than retrying instantly:
    // hammering a backend that is restarting is how one drop becomes many.
    act(() => void vi.advanceTimersByTime(999));
    expect(FakeEventSource.instances).toHaveLength(1);
    act(() => void vi.advanceTimersByTime(1));
    expect(FakeEventSource.instances).toHaveLength(2);

    // The second failure waits twice as long.
    act(() => latest().drop());
    expect(result.current.reconnect_attempts).toBe(2);
    act(() => void vi.advanceTimersByTime(1999));
    expect(FakeEventSource.instances).toHaveLength(2);
    act(() => void vi.advanceTimersByTime(1));
    expect(FakeEventSource.instances).toHaveLength(3);
  });

  it("gives up after the retry budget and says when the stream last worked", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() =>
      useSSEConnection("/sse/deployments", vi.fn(), { maxRetries: 2, retryDelayMs: 1000 }),
    );

    act(() => latest().open());
    act(() => {
      latest().drop();
      vi.advanceTimersByTime(1000);
    });
    act(() => {
      latest().drop();
      vi.advanceTimersByTime(2000);
    });
    act(() => latest().drop());

    expect(result.current.state).toBe(SSEConnectionState.DISCONNECTED);
    expect(result.current.error).toContain("Connection lost");
    // Naming the last good update is the point: "disconnected" on its own
    // leaves an operator unable to tell a stale page from an idle cluster.
    expect(result.current.error).not.toContain("unknown");
  });

  it("says the last update is unknown when the stream never opened at all", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() =>
      useSSEConnection("/sse/deployments", vi.fn(), { maxRetries: 1, retryDelayMs: 1000 }),
    );

    act(() => {
      latest().drop();
      vi.advanceTimersByTime(1000);
    });
    act(() => latest().drop());

    expect(result.current.state).toBe(SSEConnectionState.DISCONNECTED);
    expect(result.current.error).toContain("unknown");
  });

  /** A stream we closed ourselves reports an error on the way out. Treating
   *  that as a drop would reopen a connection the page just abandoned. */
  it("does not reconnect a stream that was closed on purpose", () => {
    vi.useFakeTimers();
    renderHook(() => useSSEConnection("/sse/deployments", vi.fn(), { retryDelayMs: 1000 }));

    act(() => {
      const es = latest();
      es.close();
      es.onerror?.();
      vi.advanceTimersByTime(10_000);
    });

    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("closes the stream on unmount and stops delivering events", () => {
    const onMessage = vi.fn();
    const { unmount } = renderHook(() => useSSEConnection("/sse/deployments", onMessage));
    const es = latest();

    act(() => es.open());
    unmount();

    expect(es.closed).toBe(true);
    es.emit("event", JSON.stringify({ type: "ignored" }));
    expect(onMessage).not.toHaveBeenCalled();
  });

  /** Leaving a reconnect timer armed on an unmounted page opens a connection
   *  nobody is reading, on a route the operator has already left. */
  it("cancels a pending reconnect when the page is left", () => {
    vi.useFakeTimers();
    const { unmount } = renderHook(() =>
      useSSEConnection("/sse/deployments", vi.fn(), { retryDelayMs: 1000 }),
    );

    act(() => latest().drop());
    unmount();
    act(() => void vi.advanceTimersByTime(30_000));

    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("reopens against the new url when the page switches streams", () => {
    const { rerender } = renderHook(
      ({ url }: { url: string }) => useSSEConnection(url, vi.fn()),
      { initialProps: { url: "/sse/logs/one" } },
    );
    const first = latest();

    rerender({ url: "/sse/logs/two" });

    expect(first.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(latest().url).toBe("/sse/logs/two");
  });

  /** The heartbeat exists to notice a browser that slept through a drop. It
   *  must not promote a stream that never opened to "seen just now". */
  it("does not claim a fresh update when the heartbeat fires on a dead stream", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() =>
      useSSEConnection("/sse/deployments", vi.fn(), { heartbeatIntervalMs: 15_000 }),
    );

    act(() => void vi.advanceTimersByTime(15_000));

    expect(result.current.last_connected_at).toBeUndefined();
    expect(result.current.state).toBe(SSEConnectionState.RECONNECTING);
  });
});

describe("the named stream hooks", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    useSSEStore.setState({ connections: new Map() } as never);
    vi.stubGlobal("EventSource", FakeEventSource as never);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("useEventStream subscribes per resource, so two deployments do not share a stream", () => {
    const onEvent = vi.fn();
    renderHook(() => useEventStream("dep-abc", onEvent));

    expect(latest().url).toBe("/sse/events/dep-abc");
    act(() => latest().emit("event", JSON.stringify({ type: "deployment_stop" })));
    expect(onEvent).toHaveBeenCalledWith("event", { type: "deployment_stop" });
  });
});
