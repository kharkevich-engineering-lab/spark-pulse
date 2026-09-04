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
  const heartbeatRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isMountedRef = useRef(true);
  const lastConnectedRef = useRef<string | undefined>(undefined);

  const connectionStatus = useSSEStore((s) => s.getConnection(url));
  const updateConnection = useSSEStore((s) => s.updateConnection);

  const stableOnMessage = useRef(onMessage);
  useEffect(() => {
    stableOnMessage.current = onMessage;
  }, [onMessage]);

  const clearHeartbeat = useCallback(() => {
    if (heartbeatRef.current) {
      clearTimeout(heartbeatRef.current);
      heartbeatRef.current = null;
    }
  }, []);

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

    // Close existing connection
    if (esRef.current) {
      esRef.current.close();
    }

    const es = new EventSource(url);
    esRef.current = es;

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
      clearHeartbeat();
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

      if (es.readyState === EventSource.CLOSED) {
        // Explicitly closed
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

    // Heartbeat: detect browser sleep/wake
    heartbeatRef.current = setTimeout(() => {
      if (es.readyState === EventSource.OPEN) {
        // Send a ping by checking connection state
        updateConnection(url, {
          last_connected_at: new Date().toISOString(),
        });
      }
    }, mergedOptions.heartbeatIntervalMs);
  }, [url, mergedOptions, updateConnection, clearHeartbeat]);

  useEffect(() => {
    isMountedRef.current = true;
    connect();

    return () => {
      isMountedRef.current = false;
      if (esRef.current) {
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
