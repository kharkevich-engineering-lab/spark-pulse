import { create } from "zustand";
import { type SSEConnectionStatus, SSEConnectionState } from "@/lib/operations";

// This file used to also hold an operation store, an event stream store, a
// lock store, an audit trail store and a dry-run store — none of them wired
// to any component, kept "covered" only by their own dedicated tests. They
// are deleted rather than kept as an aspiration; `useSSEStore` below is what
// `useSSEConnection` actually reads and writes.

// ── SSE Connection Store (AF-3) ──────────────────────────────────────────────

interface SSEStore {
  connections: Map<string, SSEConnectionStatus>;
  updateConnection: (url: string, status: Partial<SSEConnectionStatus>) => void;
  removeConnection: (url: string) => void;
  getConnection: (url: string) => SSEConnectionStatus | undefined;
}

export const useSSEStore = create<SSEStore>((set, get) => ({
  connections: new Map(),

  updateConnection: (url, status) =>
    set((state) => {
      const current = state.connections.get(url) ?? {
        state: SSEConnectionState.DISCONNECTED,
        reconnect_attempts: 0,
      };
      const next = new Map(state.connections);
      next.set(url, { ...current, ...status });
      return { connections: next };
    }),

  removeConnection: (url) =>
    set((state) => {
      const next = new Map(state.connections);
      next.delete(url);
      return { connections: next };
    }),

  getConnection: (url) => get().connections.get(url),
}));
