/**
 * A single GET, with loading, error and manual-retry state.
 *
 * Each panel owns its own request rather than the page owning one bundle. That
 * is what keeps a dashboard usable when one endpoint fails: a slow or broken
 * `/api/user/calendar` shows as one stuck panel, not a blank screen.
 *
 * `pollMs` re-fetches on an interval and is *paused* while the tab is hidden —
 * a break panel polling every 10 s in a background tab is pure waste, and on a
 * shared laptop it is the difference between a quiet dashboard and a hot one.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, api } from './api';

export interface PanelState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
  /** Replace the data locally after a successful write, avoiding a refetch. */
  setData: (next: T | null) => void;
}

export function usePanel<T>(
  path: string,
  { enabled = true, pollMs = 0 }: { enabled?: boolean; pollMs?: number } = {},
): PanelState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [nonce, setNonce] = useState(0);

  // A poll that is still in flight must not be replaced by a newer one, or a
  // slow response lands out of order and overwrites fresher state.
  const inFlight = useRef(false);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    const controller = new AbortController();

    const run = () => {
      if (inFlight.current) return;
      inFlight.current = true;
      api
        .get<T>(path, undefined, controller.signal)
        .then((result) => {
          if (cancelled) return;
          setData(result);
          setError(null);
        })
        .catch((err: unknown) => {
          if (cancelled || controller.signal.aborted) return;
          setError(err instanceof ApiError ? err : new ApiError(0, err));
        })
        .finally(() => {
          inFlight.current = false;
          if (!cancelled) setLoading(false);
        });
    };

    setLoading(true);
    run();

    let timer: ReturnType<typeof setInterval> | undefined;
    if (pollMs > 0) {
      timer = setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return;
        run();
      }, pollMs);
    }

    return () => {
      cancelled = true;
      controller.abort();
      if (timer) clearInterval(timer);
    };
  }, [path, nonce, enabled, pollMs]);

  return { data, error, loading, reload, setData };
}

/**
 * Track a pending write, so a button cannot be pressed twice and its label can
 * say what is happening.
 *
 * The server is idempotent for neither of these operations — a double submit of
 * "start break" would auto-end the first break — so the disable is a real
 * guard, not decoration.
 */
export function useMutation() {
  const [pending, setPending] = useState(false);
  const busy = useRef(false);

  const run = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | null> => {
    if (busy.current) return null;
    busy.current = true;
    setPending(true);
    try {
      return await fn();
    } finally {
      busy.current = false;
      setPending(false);
    }
  }, []);

  return { pending, run };
}