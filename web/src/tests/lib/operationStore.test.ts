import { describe, it, expect } from "vitest";
import { act } from "@testing-library/react";
import { useSSEStore } from "@/lib/operationStore";
import { SSEConnectionState } from "@/lib/operations";

describe("useSSEStore", () => {
  it("starts with empty connections", () => {
    const { connections } = useSSEStore.getState();
    expect(connections.size).toBe(0);
  });

  it("updates connection status", () => {
    act(() => {
      useSSEStore.getState().updateConnection("/sse/health", {
        state: SSEConnectionState.CONNECTED,
        reconnect_attempts: 0,
      });
    });

    const conn = useSSEStore.getState().getConnection("/sse/health");
    expect(conn).toBeDefined();
    expect(conn!.state).toBe(SSEConnectionState.CONNECTED);
  });

  it("removes connection", () => {
    act(() => {
      useSSEStore.getState().updateConnection("/sse/health", {
        state: SSEConnectionState.CONNECTED,
      });
      useSSEStore.getState().removeConnection("/sse/health");
    });

    expect(useSSEStore.getState().getConnection("/sse/health")).toBeUndefined();
  });

  it("merges partial updates", () => {
    act(() => {
      useSSEStore.getState().updateConnection("/sse/health", {
        state: SSEConnectionState.CONNECTED,
        reconnect_attempts: 2,
      });
      useSSEStore.getState().updateConnection("/sse/health", {
        last_connected_at: new Date().toISOString(),
      });
    });

    const conn = useSSEStore.getState().getConnection("/sse/health");
    expect(conn!.state).toBe(SSEConnectionState.CONNECTED);
    expect(conn!.reconnect_attempts).toBe(2);
    expect(conn!.last_connected_at).toBeDefined();
  });

  // A stream that has never connected still has to have a status to render,
  // or the indicator is blank rather than saying "disconnected".
  it("starts an unknown stream disconnected with no attempts made", () => {
    act(() => {
      useSSEStore.getState().updateConnection("/sse/deployments", { error: "refused" });
    });

    const conn = useSSEStore.getState().getConnection("/sse/deployments")!;
    expect(conn.state).toBe(SSEConnectionState.DISCONNECTED);
    expect(conn.reconnect_attempts).toBe(0);
    expect(conn.error).toBe("refused");
  });
});
