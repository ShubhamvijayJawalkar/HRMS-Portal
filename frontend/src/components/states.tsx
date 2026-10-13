/**
 * The shared states every migrated screen must have.
 *
 * The acceptance checklist requires loading, empty, validation, server-error and
 * permission-denied states to be *explicit and usable* -- so they are components
 * here rather than something each screen reinvents, and an omitted state is a
 * missing import rather than a missing feature.
 *
 * Accessibility is deliberate in this file, not incidental. A screen reader
 * user has no way to tell a spinner from a crash; `role="status"` with
 * `aria-live="polite"` announces the change, and the retry button is a real
 * focusable control.
 */
import type { ReactNode } from 'react';

import { ApiError } from '@/lib/api';

/** Something is in flight. Announced politely -- not an error. */
export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="state state--loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

/** A request succeeded and there is genuinely nothing to show. */
export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="state state--empty">
      <p className="state__title">{title}</p>
      {hint ? <p className="state__hint">{hint}</p> : null}
      {action ? <div className="state__action">{action}</div> : null}
    </div>
  );
}

/**
 * A failure the user can act on.
 *
 * Server messages are rendered as text, never as markup -- an error body comes
 * from the backend and may quote user input (an employee name, a ticket
 * subject), so React's default escaping is load-bearing here rather than
 * decorative.
 */
export function ErrorState({
  error,
  onRetry,
}: {
  error: ApiError | Error | null;
  onRetry?: () => void;
}) {
  const message =
    error instanceof ApiError
      ? error.message
      : error?.message || 'Something went wrong.';

  return (
    <div className="state state--error" role="alert">
      <p className="state__title">{message}</p>
      <p className="state__hint">
        {error instanceof ApiError && error.status === 0
          ? 'The server could not be reached.'
          : 'The server rejected the request or is unavailable.'}
      </p>
      {onRetry ? (
        <button type="button" className="btn" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

/**
 * The session exists but the server refused.
 *
 * This is deliberately *not* a link to "apply for access": whether a person can
 * see a module is decided by the server-side policy and its per-user overrides
 * (FR-USR-15), and there is no client-side action that grants it. Saying so is
 * more useful than a 403 with no explanation.
 */
export function PermissionDenied({ what }: { what?: string }) {
  return (
    <div className="state state--denied" role="alert">
      <p className="state__title">
        {what ? `You do not have access to ${what}.` : 'You do not have access to this page.'}
      </p>
      <p className="state__hint">
        Access is decided by your role and any per-user permissions an administrator has set.
        If you believe this is wrong, ask an administrator to review them.
      </p>
    </div>
  );
}

/**
 * Field-level validation errors, keyed by field name.
 *
 * Shown next to the form and summarised above it, because the backend rejects a
 * whole payload and an employee should not have to hunt through six inputs to
 * find which one the server objected to.
 */
export function ValidationErrors({
  errors,
  title = 'Please correct the following',
}: {
  errors: Record<string, string>;
  title?: string;
}) {
  const entries = Object.entries(errors).filter(([, message]) => Boolean(message));
  if (entries.length === 0) return null;

  return (
    <div className="state state--validation" role="alert" aria-live="assertive">
      <p className="state__title">{title}</p>
      <ul className="state__list">
        {entries.map(([field, message]) => (
          <li key={field}>
            <strong>{field}</strong>: {message}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Pick the right state for a failed request, so screens do not re-decide. */
export function ErrorBoundaryState({
  error,
  onRetry,
}: {
  error: ApiError | null;
  onRetry?: () => void;
}) {
  if (!error) return null;
  if (error.isForbidden) return <PermissionDenied />;
  if (error.isUnauthenticated) return <RedirectToLogin />;
  return <ErrorState error={error} {...(onRetry ? { onRetry } : {})} />;
}

/**
 * A no-session response sends the browser to the Flask login page.
 *
 * A full navigation, not a client-side route change: the login page is a Jinja
 * template that still owns the sign-in flow (including the deferred MFA step),
 * and migrating it is UI-12. A `<Navigate>` here would land on a React route
 * that has no login screen yet.
 */
export function RedirectToLogin() {
  if (typeof window !== 'undefined') {
    window.location.assign('/login');
  }
  return <Loading label="Redirecting to sign in…" />;
}

/**
 * A React route with no screen yet.
 *
 * **It does not redirect to the dashboard.** Redirecting makes a nav link look
 * present but do nothing, which is how a half-migrated app becomes impossible to
 * distinguish from a broken one — the operator cannot tell whether the page is
 * unbuilt or the link is wrong.
 *
 * The page still exists on the server, and that is the honest thing to offer: the
 * Jinja version is the supported one until this screen reaches parity. So the
 * link is a **full navigation** to the legacy route, not a client-side push,
 * because there is no React bundle at that path.
 */
export function NotFound() {
  if (typeof window !== 'undefined') {
    const target = window.location.pathname.replace(/^\/app/, '') || '/dashboard';
    return (
      <div className="page">
        <h1 className="page__title">Not yet migrated</h1>
        <div className="state state--empty">
          <p className="state__title">This screen has not been migrated to React yet.</p>
          <p className="state__hint">
            The existing page still works and is the supported version until this one reaches
            feature parity.
          </p>
          <a className="btn" href={target}>
            Open the current page
          </a>
        </div>
      </div>
    );
  }
  return <Loading />;
}