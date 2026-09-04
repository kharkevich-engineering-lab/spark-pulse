import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSSEConnection } from "@/hooks/useSSEConnection";
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
