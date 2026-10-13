import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';

import { Shell } from '@/components/Shell';
import { ErrorBoundaryState, Loading, NotFound, RedirectToLogin } from '@/components/states';
import { SessionProvider, useSession } from '@/lib/session';
import { Dashboard } from '@/routes/Dashboard';
import { GoalsPage } from '@/routes/GoalsPage';
import { LeavesPage } from '@/routes/LeavesPage';
import { ProfilePage } from '@/routes/ProfilePage';
import { RegularizationPage } from '@/routes/RegularizationPage';
import { TicketsPage } from '@/routes/TicketsPage';

import './styles.css';

/**
 * The application root.
 *
 * One decision lives here and is worth stating: a 401 does not render a React
 * login form. The sign-in flow is still a Jinja template -- it owns the deferred
 * MFA step and the session/attendance bookkeeping -- so an unauthenticated
 * visitor is sent there by the server, not by a client-side route. Migrating it
 * is UI-12; until then the two coexist without either pretending to be the other.
 *
 * Paths are **bare**: the router carries `basename="/app"` below, which prepends
 * the mount prefix itself. Writing `/app/dashboard` here as well would match
 * nothing and, worse, emit `/app/app/dashboard` hrefs.
 *
 * A route that exists in `policy.NAV_ENTRIES` but has no component yet renders
 * `NotFound` rather than redirecting to the dashboard. Redirecting would silently
 * make a nav link do nothing — which is exactly how a half-migrated app becomes
 * impossible to tell apart from a broken one.
 */
function AppRoutes() {
  const { loading, unauthenticated, error, reload } = useSession();

  if (loading) {
    return (
      <div className="boot">
        <Loading label="Starting HRMS…" />
      </div>
    );
  }

  if (unauthenticated) return <RedirectToLogin />;

  // A 403 here means the session exists but /api/navigation itself was refused,
  // which should not happen for a signed-in actor. Rendered rather than
  // swallowed so it is diagnosable in a browser test.
  if (error) {
    return (
      <div className="boot">
        <ErrorBoundaryState error={error} onRetry={reload} />
      </div>
    );
  }

  return (
    <Shell>
      <Routes>
        {/* Phase 2 pilot */}
        <Route path="/dashboard" element={<Dashboard />} />

        {/* Phase 3 — UI-08 self-service */}
        <Route path="/leaves" element={<LeavesPage />} />
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="/tickets" element={<TicketsPage />} />
        <Route path="/regularization" element={<RegularizationPage />} />
        <Route path="/goals" element={<GoalsPage />} />

        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Shell>
  );
}

const container = document.getElementById('root');
if (!container) {
  throw new Error('#root is missing from index.html');
}

createRoot(container).render(
  <StrictMode>
    <BrowserRouter basename="/app">
      <SessionProvider>
        <AppRoutes />
      </SessionProvider>
    </BrowserRouter>
  </StrictMode>,
);