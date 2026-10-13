/**
 * Pilot screen: the employee dashboard (Phase 2 exit criterion).
 *
 * Chosen as the pilot because it exercises a broad slice of the read APIs while
 * touching only two write contracts, so it proves the shell, the typed API
 * client and the CSRF path end to end at the lowest possible risk.
 *
 * Every read is independent and non-critical, so each panel owns its own state.
 * A dashboard that waited for all of them before rendering would show one slow
 * endpoint as a blank page; a panel that fails shows a failed panel.
 */
import { useEffect } from 'react';

import { EmptyState } from '@/components/states';
import { Panel } from '@/components/Panel';
import { api } from '@/lib/api';
import { usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type {
  DashboardStats,
  HolidaysPayload,
  LoginSession,
  NotificationsPayload,
  Profile,
  ShiftSummary,
} from '@/lib/types';

import { BreakPanel } from './BreakPanel';
import { LeavePanel } from './LeavePanel';

function formatHours(value: unknown): string {
  const hours = Number(value);
  if (!Number.isFinite(hours)) return '—';
  return `${hours.toFixed(2)} h`;
}

function StatTile({
  value,
  label,
  note,
}: {
  value: string;
  label: string;
  note?: string;
}) {
  return (
    <div className="stat">
      <p className="metric">{value}</p>
      <p className="metric__note">{label}</p>
      {note ? <p className="metric__note">{note}</p> : null}
    </div>
  );
}

export function Dashboard() {
  const { actor, seesAdminSurface } = useSession();

  const profile = usePanel<Profile>('/api/profile');
  const shift = usePanel<ShiftSummary>('/api/user/shift-summary');
  const notifications = usePanel<NotificationsPayload>('/api/notifications');
  const holidays = usePanel<HolidaysPayload | Holiday[]>('/api/holidays');
  const sessions = usePanel<LoginSession[]>('/api/login-hours');
  // The admin stats endpoint is gated on the `users` module; requesting it for
  // an employee would just produce a 403 the panel then has to explain.
  const stats = usePanel<DashboardStats>('/api/dashboard-stats', {
    enabled: seesAdminSurface,
  });

  /*
   * Mark notifications read once they have been seen.
   *
   * Deliberately *not* fired immediately on mount, the way the Jinja page did.
   * That marked them read the instant the dashboard loaded, including
   * notifications the employee never scrolled to — so a badge on another page
   * had already been cleared by a visit they did not read anything on. Here it
   * fires only when there is at least one unread and the panel has rendered.
   */
  const unread = notifications.data?.unread ?? 0;
  useEffect(() => {
    if (unread <= 0) return;
    const timer = setTimeout(() => {
      void api.post('/api/notifications/read').catch(() => {
        /* Best-effort: a failure here must not disturb the panel. The badge
           simply stays, which is the honest state. */
      });
    }, 1500);
    return () => clearTimeout(timer);
  }, [unread]);

  const greeting = actor?.name ? `Welcome back, ${actor.name}` : 'Welcome back';

  return (
    <div className="page">
      <h1 className="page__title">{greeting}</h1>
      <p className="page__subtitle">
        Signed in as <strong>{actor?.emp_id}</strong>
        {actor?.role ? ` · ${actor.role}` : ''}
      </p>

      {/*
        `estimated` and `capped` are surfaced rather than swallowed. The
        shift-length rule (FR-ATT-09) caps an open shift at the scheduled length
        plus 25% and flags it, so an employee whose session is still open has a
        number the UI itself says is provisional — hiding the flag would make a
        corrected figure look like a mistake.
      */}
      <div className="stats">
        <StatTile
          value={shift.data ? formatHours(shift.data.shift_hours) : '—'}
          label="Shift hours"
          note={
            shift.data
              ? `${shift.data.estimated ? 'Estimated — your shift is still open' : 'Confirmed'}${
                  shift.data.capped ? ', capped at scheduled + 25%' : ''
                }`
              : undefined
          }
        />
        <StatTile
          value={
            shift.data ? `${shift.data.total_break_minutes ?? 0} min` : '—'
          }
          label="Break time"
          note={
            shift.data?.scheduled_hours != null
              ? `Scheduled ${formatHours(shift.data.scheduled_hours)}${
                  shift.data.shift_configured === false ? ' (none configured)' : ''
                }`
              : undefined
          }
        />
        <StatTile
          value={shift.data?.efficiency != null ? `${shift.data.efficiency}%` : '—'}
          label="Efficiency"
        />
      </div>

      <div className="grid">
        <Panel
          title="Your profile"
          data={profile.data}
          error={profile.error}
          loading={profile.loading}
          reload={profile.reload}
        >
          {(data) => (
            <dl className="kv">
              <dt>Name</dt>
              <dd>{String(data.name ?? '—')}</dd>
              <dt>Email</dt>
              <dd>{String(data.email ?? '—')}</dd>
              <dt>Department</dt>
              <dd>{String(data.department ?? '—')}</dd>
            </dl>
          )}
        </Panel>

        {seesAdminSurface ? (
          <Panel
            title="Organisation"
            data={stats.data}
            error={stats.error}
            loading={stats.loading}
            reload={stats.reload}
          >
            {(data) => (
              <dl className="kv">
                {Object.entries(data)
                  .filter(([, value]) => typeof value !== 'object')
                  .slice(0, 8)
                  .map(([key, value]) => (
                    <div key={key} className="kv__row">
                      <dt>{key.replace(/_/g, ' ')}</dt>
                      <dd>{String(value)}</dd>
                    </div>
                  ))}
              </dl>
            )}
          </Panel>
        ) : null}

        <Panel
          title="Notifications"
          data={notifications.data}
          error={notifications.error}
          loading={notifications.loading}
          reload={notifications.reload}
        >
          {(data) => {
            const items = data.data ?? [];
            if (items.length === 0) {
              return <p className="metric__note">You have no notifications.</p>;
            }
            return (
              <>
                {data.unread > 0 ? (
                  <p className="badge-inline">
                    {data.unread} unread
                  </p>
                ) : null}
                <ul className="list">
                  {items.slice(0, 10).map((item) => (
                    <li
                      key={item.id ?? item.message}
                      className={`list__item ${item.is_read ? '' : 'list__item--unread'}`}
                    >
                      {item.type ? (
                        <span className="list__meta">{String(item.type)}</span>
                      ) : null}
                      {String(item.message ?? '')}
                    </li>
                  ))}
                </ul>
              </>
            );
          }}
        </Panel>

        <Panel
          title="Upcoming holidays"
          data={holidays.data}
          error={holidays.error}
          loading={holidays.loading}
          reload={holidays.reload}
        >
          {(data) => {
            // The endpoint returns `{holidays, filters}` on the current build and
            // a bare array on older ones. Accept both rather than assuming the
            // newer shape and rendering an empty calendar if it regresses.
            const items = Array.isArray(data) ? data : (data.holidays ?? []);
            const today = new Date().toISOString().slice(0, 10);
            const upcoming = items
              .filter((holiday) => String(holiday.date ?? '') >= today)
              .sort((a, b) => String(a.date ?? '').localeCompare(String(b.date ?? '')))
              .slice(0, 5);
            if (upcoming.length === 0) {
              return <EmptyState title="No upcoming holidays." />;
            }
            return (
              <ul className="list">
                {upcoming.map((holiday) => (
                  <li key={holiday.id ?? holiday.name} className="list__item">
                    <strong>{String(holiday.name ?? '')}</strong>
                    <span className="list__meta">
                      {String(holiday.date ?? '')}
                      {holiday.type ? ` · ${holiday.type}` : ''}
                    </span>
                  </li>
                ))}
              </ul>
            );
          }}
        </Panel>

        <Panel
          title="Login sessions"
          data={sessions.data}
          error={sessions.error}
          loading={sessions.loading}
          reload={sessions.reload}
        >
          {(data) => {
            if (data.length === 0) {
              return <EmptyState title="No sessions recorded." />;
            }
            return (
              <table className="table">
                <caption className="sr-only">Sign-in and sign-out times with duration</caption>
                <thead>
                  <tr>
                    <th scope="col">Login</th>
                    <th scope="col">Logout</th>
                    <th scope="col">Duration</th>
                  </tr>
                </thead>
                <tbody>
                  {data.slice(0, 10).map((session, index) => (
                    <tr key={session.session_id ?? index}>
                      <td>{String(session.login_time ?? '—')}</td>
                      <td>{session.logout_time ? String(session.logout_time) : '—'}</td>
                      <td>
                        {Number(session.total_hours ?? 0).toFixed(2)} h
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            );
          }}
        </Panel>
      </div>

      {/*
        The two write workflows. They sit below the read panels because they are
        the ones an employee *acts* on, and on a phone that means scrolling to
        them — not because they are secondary.
      */}
      <BreakPanel />
      <LeavePanel />
    </div>
  );
}

/** Local alias so the holidays union stays readable at the call site. */
type Holiday = HolidaysPayload['holidays'][number];