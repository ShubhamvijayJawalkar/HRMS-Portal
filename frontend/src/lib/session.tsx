/**
 * Session context.
 *
 * The server owns the session. This holds only what the server told us, and
 * it re-reads on mount rather than caching in storage -- a stale `role` in
 * localStorage is exactly the bug FR-USR-15 exists to prevent, so the SPA
 * deliberately has no way to hold one.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import { ApiError, api, setCurrentEmpId } from './api';
import type { NavigationPayload } from './types';

interface SessionState {
  actor: NavigationPayload['actor'] | null;
  navigation: NavigationPayload['navigation'];
  seesAdminSurface: boolean;
  /** True until the first /api/navigation call settles. */
  loading: boolean;
  /** Set when the caller has no session (401), as opposed to a real failure. */
  unauthenticated: boolean;
  error: ApiError | null;
  reload: () => void;
}

const SessionContext = createContext<SessionState | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [payload, setPayload] = useState<NavigationPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [unauthenticated, setUnauthenticated] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .get<NavigationPayload>('/api/navigation')
      .then((data) => {
        if (cancelled) return;
        setPayload(data);
        // Registered once the identity is known, so the API client can label
        // whose records a policy-scoped list is holding. See `setCurrentEmpId`.
        setCurrentEmpId(data.actor?.emp_id ?? null);
        setUnauthenticated(false);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setPayload(null);
        if (err instanceof ApiError && err.isUnauthenticated) {
          setUnauthenticated(true);
          setError(null);
        } else {
          setError(err instanceof ApiError ? err : new ApiError(0, err));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  const value = useMemo<SessionState>(
    () => ({
      actor: payload?.actor ?? null,
      navigation: payload?.navigation ?? [],
      seesAdminSurface: payload?.sees_admin_surface ?? false,
      loading,
      unauthenticated,
      error,
      reload,
    }),
    [payload, loading, unauthenticated, error, reload],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionState {
  const context = useContext(SessionContext);
  if (!context) throw new Error('useSession must be used inside <SessionProvider>');
  return context;
}