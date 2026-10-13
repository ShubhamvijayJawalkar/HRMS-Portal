/**
 * The one place this app talks to the backend.
 *
 * Everything that is easy to get subtly wrong in a SPA -- credentials, CSRF,
 * error shape, 401 vs 403 -- is handled here once, so a screen cannot get it
 * wrong by accident. The plan makes this a Phase 1 exit criterion, and it is
 * the right one: the legacy templates solve CSRF with a `window.fetch` wrapper
 * that `security.init_csrf` injects into every HTML response, which an SPA does
 * not have. If each React screen fetched on its own, half of them would be
 * missing the token.
 */

/** A non-2xx response, carrying the status so screens can branch on it. */
export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown, message?: string) {
    super(message ?? errorMessage(payload, status));
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }

  /** 401 means "no session"; 403 means "session, but not allowed". */
  get isUnauthenticated(): boolean {
    return this.status === 401;
  }
  get isForbidden(): boolean {
    return this.status === 403;
  }
}

function errorMessage(payload: unknown, status: number): string {
  if (payload && typeof payload === 'object' && 'error' in payload) {
    const detail = (payload as { error: unknown }).error;
    if (typeof detail === 'string' && detail) return detail;
  }
  return `Request failed (${status})`;
}

/**
 * CSRF token, held in memory only.
 *
 * Not in localStorage, not in a cookie, not in the URL. The plan forbids
 * putting session identifiers or credentials in browser storage, and the token
 * is neither of those -- but keeping it in memory costs nothing and means a
 * stale token cannot survive across sessions in a shared machine.
 */
let csrfToken: string | null = null;
let csrfInFlight: Promise<string> | null = null;

async function fetchCsrfToken(): Promise<string> {
  const response = await fetch('/api/csrf-token', {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) {
    throw new ApiError(response.status, null, 'Could not obtain a CSRF token');
  }
  const body = (await response.json()) as { csrf_token?: string };
  if (!body.csrf_token) {
    throw new ApiError(response.status, body, 'Malformed CSRF token response');
  }
  csrfToken = body.csrf_token;
  return csrfToken;
}

/** Memoised so N components mounting at once cause one request, not N. */
function csrf(): Promise<string> {
  if (csrfToken) return Promise.resolve(csrfToken);
  csrfInFlight ??= fetchCsrfToken().finally(() => {
    csrfInFlight = null;
  });
  return csrfInFlight;
}

/** Forget the cached token. Called on logout and when the session rotates. */
export function resetCsrfToken(): void {
  csrfToken = null;
  csrfInFlight = null;
}

const UNSAFE = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

type Query = Record<string, string | number | boolean | undefined | null>;

/**
 * The signed-in employee's id, used only to label whose records a list holds.
 *
 * Set once at start-up from `/api/navigation`; see the header note below for why
 * it exists and why it must never gate anything.
 */
let currentEmpId: string | null = null;

export function setCurrentEmpId(empId: string | null): void {
  currentEmpId = empId;
}

export interface RequestOptions {
  method?: string;
  /** JSON-serialisable body. Sent as application/json. */
  body?: unknown;
  query?: Query;
  signal?: AbortSignal;
  /**
   * Internal: set once the client has already refreshed the token for this
   * call, so a persistent 403 cannot become a refresh loop.
   */
  _retried?: boolean;
}

function withQuery(path: string, query?: Query): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') {
      params.set(key, String(value));
    }
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

async function readBody(response: Response): Promise<unknown> {
  const type = response.headers.get('content-type') ?? '';
  if (response.status === 204) return null;
  if (type.includes('application/json')) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
  const text = await response.text();
  return text || null;
}

export async function request<T = unknown>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const method = (options.method ?? 'GET').toUpperCase();
  const headers: Record<string, string> = { Accept: 'application/json' };

  /*
   * Who the caller is.
   *
   * `page.current_actor()` already exposes `current_emp_id` to every Jinja
   * template, and the SPA needs it for the same reason the navbar does: to know
   * *whose* records a list belongs to.
   *
   * This matters because `GET /api/leaves` is scoped by policy — an employee
   * gets their own requests, an Admin gets the whole company's. Labelling that
   * list "Your requests" unconditionally is a statement the client cannot make
   * honestly, and it happened here: the heading claimed the list was the
   * employee's while the rows were four colleagues'.
   *
   * It is a *hint for labelling*, never an authorization input — it is entirely
   * client-supplied and the server ignores it. Nothing may branch on it for
   * access; the endpoints re-check every rule.
   */
  if (currentEmpId) {
    headers['X-Current-Emp-Id'] = currentEmpId;
  }

  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  if (UNSAFE.has(method)) {
    headers['X-CSRF-Token'] = await csrf();
  }

  const response = await fetch(withQuery(path, options.query), {
    method,
    headers,
    // Same-origin only: this is what carries the existing secure session
    // cookie. `credentials: 'include'` would also attach the cookie to a
    // cross-origin request, which we never want.
    credentials: 'same-origin',
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  });

  if (response.ok) return (await readBody(response)) as T;

  const payload = await readBody(response);

  /**
   * One retry on a CSRF rejection, after refreshing the token.
   *
   * The session cookie is the authority and it rotates on logout, so a token
   * cached before a sign-in can be stale by the time a screen writes. Without
   * this, the first write after a fresh login fails with an opaque 403 that
   * looks like a permissions problem. Retrying exactly once means a genuine
   * CSRF rejection still surfaces instead of looping.
   */
  const isCsrfRejection =
    response.status === 403 &&
    payload !== null &&
    typeof payload === 'object' &&
    'error' in payload &&
    /csrf/i.test(String((payload as { error: unknown }).error));

  if (isCsrfRejection && UNSAFE.has(method) && !options._retried) {
    resetCsrfToken();
    return request<T>(path, { ...options, _retried: true });
  }

  throw new ApiError(response.status, payload);
}

export async function upload<T = unknown>(path: string, formData: FormData, _retried = false): Promise<T> {
  const method = 'POST';
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (currentEmpId) headers['X-Current-Emp-Id'] = currentEmpId;
  if (UNSAFE.has(method)) headers['X-CSRF-Token'] = await csrf();

  const response = await fetch(path, {
    method,
    headers,
    credentials: 'same-origin',
    body: formData,
  });

  if (response.ok) return (await readBody(response)) as T;

  const payload = await readBody(response);
  const isCsrfRejection =
    response.status === 403 &&
    payload !== null &&
    typeof payload === 'object' &&
    'error' in payload &&
    /csrf/i.test(String((payload as { error: unknown }).error));

  if (isCsrfRejection && !_retried) {
    resetCsrfToken();
    return upload<T>(path, formData, true);
  }

  throw new ApiError(response.status, payload);
}

export const api = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) =>
    request<T>(path, { method: 'GET', ...(query ? { query } : {}), ...(signal ? { signal } : {}) }),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  upload: <T>(path: string, formData: FormData) => upload<T>(path, formData, false),
};