import { NavBar } from '@/components/NavBar';
import { useSession } from '@/lib/session';

function initials(name: string | null | undefined): string {
  const trimmed = (name ?? '').trim();
  return trimmed ? trimmed[0]!.toUpperCase() : '?';
}

/**
 * The authenticated shell: navbar, actor identity, sign-out, content.
 *
 * The identity block reads from the `/api/navigation` payload, which is built
 * from the **database** actor rather than the session copy. That is the same
 * rule the Jinja context processor follows, and for the same reason: the session
 * caches `role` at login time, so a role change would otherwise leave the UI
 * describing an identity the server has already stopped honouring.
 */
export function Shell({ children }: { children: React.ReactNode }) {
  const { actor, navigation } = useSession();

  return (
    <div className="shell">
      <NavBar entries={navigation} />

      <header className="shell__header">
        <div className="shell__identity">
          <span className="shell__avatar" aria-hidden="true">
            {initials(actor?.name)}
          </span>
          <span className="shell__meta">
            <span className="shell__name">{actor?.name || 'User'}</span>
            <span className="shell__role">
              {actor?.role || 'Employee'}
              {actor?.department ? <span className="shell__dept">{actor.department}</span> : null}
            </span>
          </span>
        </div>
        {/*
          Sign-out is a real navigation to the Flask logout route, not a fetch.
          It has to clear the session cookie server-side and close the
          `user_sessions` row that feeds attendance and payroll; doing it from
          the client would leave the hours unaccounted.
        */}
        <a className="btn btn--ghost" href="/logout">
          Sign out
        </a>
      </header>

      <main className="shell__main" id="main">
        {children}
      </main>
    </div>
  );
}