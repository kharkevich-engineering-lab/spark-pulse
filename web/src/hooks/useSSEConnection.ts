import { useEffect, useRef, useCallback, useMemo } from "react";
import {
  SSEConnectionState,
  type SSEConnectionStatus,
} from "@/lib/operations";
import { useSSEStore } from "@/lib/operationStore";

interface UseSSEConnectionOptions {
  maxRetries?: number;
  retryDelayMs?: number;
  heartbeatIntervalMs?: number;
}

const DEFAULT_OPTIONS: Required<UseSSEConnectionOptions> = {
  maxRetries: 5,
  retryDelayMs: 1000,
  heartbeatIntervalMs: 15000,
};

/**
 * SSE Connection Hook (AF-3)
 *
 * Manages EventSource connections with automatic reconnection,
 * exponential backoff, and browser sleep/wake handling.
 *
 * Usage:
 * ```ts
 * const handleHealthEvent = useCallback((event: string, data: unknown) => {
 *   console.log(event, data);
 * }, []);
 *
 * const status = useSSEConnection("/sse/health", handleHealthEvent);
 *
 * if (status.state === SSEConnectionState.CONNECTED) {
 *   // Real-time updates active
 * }
 * ```
 */
export function useSSEConnection(
  url: string,
  onMessage: (event: string, data: unknown) => void,
  options: UseSSEConnectionOptions = {}
): SSEConnectionStatus {
  const mergedOptions = useMemo(
    () => ({ ...DEFAULT_OPTIONS, ...options }),
    [options.maxRetries, options.retryDelayMs, options.heartbeatIntervalMs]
  );
  const esRef = useRef<EventSource | null>(null);
  const retryCountRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null);
  /** When the heartbeat interval last actually fired. Not "when the server
   *  last spoke" — a quiet stream is not a dead one — but "when this timer
   *  was last given a turn on the event loop at all". */
  const lastHeartbeatTickRef = useRef<number>(Date.now());
  /** Whether the most recent close was ours rather than the server's. */
  const intentionalCloseRef = useRef(false);
  const isMountedRef = useRef(true);
  const lastConnectedRef = useRef<string | undefined>(undefined);
  /** `connect`, kept current for the heartbeat's own closure so starting it
   *  does not have to depend on — and thereby be redefined by — `connect`. */
  const connectRef = useRef<() => void>(() => {});

  const connectionStatus = useSSEStore((s) => s.getConnection(url));
  const updateConnection = useSSEStore((s) => s.updateConnection);

  const stableOnMessage = useRef(onMessage);
  useEffect(() => {
    stableOnMessage.current = onMessage;
  }, [onMessage]);

  const clearHeartbeat = useCallback(() => {
    if (heartbeatRef.current) {
      clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }
  }, []);

  /** Starts once the stream is actually open, and stops as soon as it is not.
   *
   *  The check is not "has a message arrived lately" — a quiet deployment
   *  stream can go minutes between real events — it is "did this timer fire
   *  on schedule". Suspending a laptop pauses its whole JS event loop, timers
   *  included; on wake, a repeating timer fires far later than the interval
   *  it was given, which a machine that never slept does not do. That gap is
   *  treated as a sleep/wake, and a socket the browser still calls OPEN is
   *  forced to reconnect, because the wake can leave it dead on the far end
   *  without ever raising `onerror`.
   */
  const startHeartbeat = useCallback(() => {
    clearHeartbeat();
    lastHeartbeatTickRef.current = Date.now();
    heartbeatRef.current = setInterval(() => {
      if (!isMountedRef.current) return;
      const now = Date.now();
      const elapsed = now - lastHeartbeatTickRef.current;
      lastHeartbeatTickRef.current = now;
      if (
        elapsed > mergedOptions.heartbeatIntervalMs * 2 &&
        esRef.current?.readyState === EventSource.OPEN
      ) {
        connectRef.current();
      }
    }, mergedOptions.heartbeatIntervalMs);
  }, [clearHeartbeat, mergedOptions.heartbeatIntervalMs]);

  const scheduleReconnect = useCallback(
    (attempt: number) => {
      if (!isMountedRef.current) return;

      const delay = Math.min(
        mergedOptions.retryDelayMs * Math.pow(2, attempt - 1),
        30000 // Cap at 30s
      );

      updateConnection(url, {
        state: SSEConnectionState.RECONNECTING,
        reconnect_attempts: attempt,
        error: `Reconnecting... (${attempt}/${mergedOptions.maxRetries})`,
      });

      reconnectTimerRef.current = setTimeout(() => {
        retryCountRef.current = attempt;
        connect();
      }, delay);
    },
    [url, mergedOptions, updateConnection]
  );

  const connect = useCallback(() => {
    if (!isMountedRef.current) return;

    // A reconnect leaves the previous connection's heartbeat with nothing to
    // watch; `onopen` below starts a fresh one once this attempt succeeds.
    clearHeartbeat();

    // Close existing connection. Flagged, so the `onerror` this provokes is
    // not mistaken for the server hanging up on us.
    if (esRef.current) {
      intentionalCloseRef.current = true;
      esRef.current.close();
    }

    const es = new EventSource(url);
    esRef.current = es;
    intentionalCloseRef.current = false;

    updateConnection(url, {
      state: SSEConnectionState.RECONNECTING,
      reconnect_attempts: retryCountRef.current,
      error: `Connecting... (${retryCountRef.current}/${mergedOptions.maxRetries})`,
    });

    es.onopen = () => {
      if (!isMountedRef.current) return;

      lastConnectedRef.current = new Date().toISOString();
      updateConnection(url, {
        state: SSEConnectionState.CONNECTED,
        reconnect_attempts: 0,
        last_connected_at: lastConnectedRef.current,
        error: undefined,
      });
      retryCountRef.current = 0;
      startHeartbeat();
    };

    es.onmessage = (event: MessageEvent) => {
      if (!isMountedRef.current) return;
      try {
        const data = JSON.parse(event.data);
        stableOnMessage.current("message", data);
      } catch {
        stableOnMessage.current("message", event.data);
      }
    };

    // Listen for specific event types
    es.addEventListener("health", (e: MessageEvent) => {
      if (!isMountedRef.current) return;
      try {
        const data = JSON.parse(e.data);
        stableOnMessage.current("health", data);
      } catch {
        stableOnMessage.current("health", e.data);
      }
    });

    es.addEventListener("event", (e: MessageEvent) => {
      if (!isMountedRef.current) return;
      try {
        const data = JSON.parse(e.data);
        stableOnMessage.current("event", data);
      } catch {
        stableOnMessage.current("event", e.data);
      }
    });

    es.addEventListener("log", (e: MessageEvent) => {
      if (!isMountedRef.current) return;
      try {
        const data = JSON.parse(e.data);
        stableOnMessage.current("log", data);
      } catch {
        stableOnMessage.current("log", e.data);
      }
    });

    es.onerror = () => {
      if (!isMountedRef.current) return;

      // Whatever this stream was doing, it is not OPEN any more — nothing
      // left for the wake heartbeat to watch until a future `onopen` starts
      // it again.
      clearHeartbeat();

      if (es.readyState === EventSource.CLOSED) {
        if (intentionalCloseRef.current) return;
        // The browser closed the stream itself and will not retry. That is
        // what it does for a response it cannot use — a 401 once the session
        // expires, a proxy error page, the wrong content-type — as opposed to
        // a dropped connection, which leaves it CONNECTING and retrying.
        //
        // Returning here treated the two as the same thing, so the indicator
        // sat on "Connecting..." for the rest of the page's life and the
        // operator watched a spinner instead of being told the stream was
        // dead.
        updateConnection(url, {
          state: SSEConnectionState.DISCONNECTED,
          error: lastConnectedRef.current
            ? `Connection closed by the server. Last update: ${new Date(lastConnectedRef.current).toLocaleTimeString()}`
            : "Connection refused by the server.",
        });
        return;
      }

      // Connection error - attempt reconnect
      if (retryCountRef.current < mergedOptions.maxRetries) {
        scheduleReconnect(retryCountRef.current + 1);
      } else {
        updateConnection(url, {
          state: SSEConnectionState.DISCONNECTED,
          error: `Connection lost. Last update: ${lastConnectedRef.current ? new Date(lastConnectedRef.current).toLocaleTimeString() : "unknown"}`,
        });
      }
    };
  }, [url, mergedOptions, updateConnection, clearHeartbeat, startHeartbeat]);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  useEffect(() => {
    isMountedRef.current = true;
    connect();

    return () => {
      isMountedRef.current = false;
      if (esRef.current) {
        intentionalCloseRef.current = true;
        esRef.current.close();
        esRef.current = null;
      }
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
      }
      clearHeartbeat();
    };
  }, [url, connect]);

  // Return current status or defaults
  return (
    connectionStatus ?? {
      state: SSEConnectionState.DISCONNECTED,
      reconnect_attempts: 0,
    }
  );
}

/**
 * Hook for connecting to a deployment event SSE stream.
 *
 * Usage:
 * ```ts
 * const handleEvent = useCallback((event: string, data: unknown) => {
 *   if (event === "event") {
 *     addEvent(data as DeploymentEvent);
 *   }
 * }, []);
 *
 * const eventStatus = useEventStream("my-cluster", handleEvent);
 * ```
 */
export function useEventStream(
  resource: string,
  onEventUpdate: (event: string, data: unknown) => void
): SSEConnectionStatus {
  return useSSEConnection(
    `/sse/events/${resource}`,
    onEventUpdate,
    { maxRetries: 5, retryDelayMs: 1000 }
  );
}
