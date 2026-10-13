/**
 * The read/write panel used by every migrated screen.
 *
 * Extracted from the dashboard pilot rather than copied into each page. The
 * dashboard had its own copy; four more screens needing the same five states
 * (loading / empty / error / permission-denied / retry) is four more chances to
 * forget one, and the accessibiliy roles live with it.
 */
import type { ReactNode } from 'react';

import { ErrorState, Loading, PermissionDenied } from './states';
import type { ApiError } from '@/lib/api';

export function Panel<T>({
  title,
  data,
  error,
  loading,
  reload,
  denied,
  deniedLabel,
  children,
  actions,
}: {
  title: string;
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
  /** The server refused this resource for this actor (403). */
  denied?: boolean;
  deniedLabel?: string;
  children: (data: T) => ReactNode;
  /** Rendered in the header — usually a Refresh button. */
  actions?: ReactNode;
}) {
  return (
    <section className="panel" aria-label={title}>
      <header className="panel__header">
        <h2 className="panel__title">{title}</h2>
        <div className="panel__actions">
          {error || denied ? (
            <button type="button" className="btn btn--small" onClick={reload}>
              Retry
            </button>
          ) : null}
          {actions}
        </div>
      </header>
      <div className="panel__body">
        {denied ? <PermissionDenied {...(deniedLabel ? { what: deniedLabel } : {})} /> : null}
        {!denied && loading ? <Loading label={`Loading ${title.toLowerCase()}…`} /> : null}
        {!denied && !loading && error ? (
          <ErrorState error={error} onRetry={reload} />
        ) : null}
        {!denied && !loading && !error && data ? children(data) : null}
      </div>
    </section>
  );
}

/** A page heading, so every migrated screen reads the same way. */
export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div>
        <h1 className="page__title">{title}</h1>
        {subtitle ? <p className="page__subtitle">{subtitle}</p> : null}
      </div>
      {actions ? <div className="page-header__actions">{actions}</div> : null}
    </div>
  );
}

/** A refresh button for a panel or page header. */
export function RefreshButton({
  onClick,
  pending,
  label = 'Refresh',
}: {
  onClick: () => void;
  pending?: boolean;
  label?: string;
}) {
  return (
    <button type="button" className="btn btn--small" onClick={onClick} disabled={pending}>
      {label}
    </button>
  );
}

/**
 * A list of records, or an explicit empty state.
 *
 * The empty state is a component rather than a falsy render because "no rows"
 * and "the request failed" must never look the same — which is precisely how the
 * notifications panel hid a live unread notification behind "you have none".
 */
export function RecordList<T>({
  items,
  keyOf,
  emptyTitle,
  emptyHint,
  render,
}: {
  items: readonly T[];
  keyOf: (item: T, index: number) => string | number;
  emptyTitle: string;
  emptyHint?: string;
  render: (item: T, index: number) => ReactNode;
}) {
  if (items.length === 0) {
    return (
      <div className="state state--empty">
        <p className="state__title">{emptyTitle}</p>
        {emptyHint ? <p className="state__hint">{emptyHint}</p> : null}
      </div>
    );
  }
  return (
    <ul className="list">
      {items.map((item, index) => (
        <li key={keyOf(item, index)} className="list__item">
          {render(item, index)}
        </li>
      ))}
    </ul>
  );
}

/** A status pill. `tone` maps to the CSS modifier classes. */
export function StatusPill({ status }: { status: string }) {
  return <span className={`status status--${status.toLowerCase()}`}>{status}</span>;
}