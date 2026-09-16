import { useState, useEffect, useCallback, useRef } from "react";

export interface UseQueryResult<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

/** A fetcher may ignore the signal entirely — most just resolve a promise —
 *  but one that forwards it to `fetch` gets a request that is actually
 *  cancelled over the network, not merely a response the hook throws away. */
export function useQuery<T>(fetcher: (signal?: AbortSignal) => Promise<T>): UseQueryResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const hasDataRef = useRef(false);

  const refetch = useCallback(() => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const signal = controller.signal;
    if (!hasDataRef.current) setLoading(true);
    setError(null);
    fetcher(signal).then((res) => {
      if (!signal.aborted) {
        setData(res);
        hasDataRef.current = true;
        setLoading(false);
      }
    }).catch((e) => {
      if (!signal.aborted && e.name !== "AbortError") {
        setError(e.message);
        setLoading(false);
      }
    });
  }, [fetcher]);

  useEffect(() => {
    refetch();
    // Abandon an in-flight request rather than let it land — and try to set
    // state — on a hook nothing is reading any more.
    return () => abortRef.current?.abort();
  }, [refetch]);

  return { data, loading, error, refetch };
}
