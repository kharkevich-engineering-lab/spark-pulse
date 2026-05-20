import { useState, useEffect, useCallback, useRef } from "react";

export interface UseQueryResult<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

export function useQuery<T>(fetcher: () => Promise<T>, deps: unknown[] = []): UseQueryResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const refetch = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = new AbortController();
    const signal = abortRef.current.signal;
    setLoading(true);
    setError(null);
    fetcher().then((res) => { if (!signal.aborted) setData(res); })
      .catch((e) => { if (!signal.aborted && e.name !== "AbortError") setError(e.message); })
      .finally(() => { if (!signal.aborted) setLoading(false); });
  }, deps);

  useEffect(() => { refetch(); return () => abortRef.current?.abort(); }, [refetch]);

  return { data, loading, error, refetch };
}
