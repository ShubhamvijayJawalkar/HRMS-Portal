import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';

import { Shell } from '@/components/Shell';
import { ErrorBoundaryState, Loading, NotFound, RedirectToLogin } from '@/components/states';
import { PreboardingPage } from '@/routes/PreboardingPage';
import { LoginPage } from '@/routes/LoginPage';
import { ResetPasswordPage } from '@/routes/ResetPasswordPage';
import { SessionProvider, useSession } from '@/lib/session';
import { AdminAssetsPage } from '@/routes/AdminAssetsPage';
import { AdminCandidatesPage } from '@/routes/AdminCandidatesPage';
import { AdminExpensesPage } from '@/routes/AdminExpensesPage';
import { AdminAnalyticsPage } from '@/routes/AdminAnalyticsPage';
import { AdminHolidaysPage } from '@/routes/AdminHolidaysPage';
import { AdminImportUsersPage } from '@/routes/AdminImportUsersPage';
import { AdminOnboardingPage } from '@/routes/AdminOnboardingPage';
import { AdminOffboardingPage } from '@/routes/AdminOffboardingPage';
import { AdminDocumentsPage } from '@/routes/AdminDocumentsPage';
import { AdminJobsPage } from '@/routes/AdminJobsPage';
import { AdminReviewsPage } from '@/routes/AdminReviewsPage';
import { AdminPayrollPage } from '@/routes/AdminPayrollPage';
import { AdminReportsPage } from '@/routes/AdminReportsPage';
import { AdminSalaryStructuresPage } from '@/routes/AdminSalaryStructuresPage';
import { AdminTicketsPage } from '@/routes/AdminTicketsPage';
import { AdminUsersPage } from '@/routes/AdminUsersPage';
import { AuditLogPage } from '@/routes/AuditLogPage';
import { Dashboard } from '@/routes/Dashboard';
import { DocumentsPage } from '@/routes/DocumentsPage';
import { LeaveApprovalsPage } from '@/routes/LeaveApprovalsPage';
import { ExpensesPage } from '@/routes/ExpensesPage';
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
        <Route path="/admin/assets" element={<AdminAssetsPage />} />
        <Route path="/admin/expenses" element={<AdminExpensesPage />} />
        <Route path="/admin/import-users" element={<AdminImportUsersPage />} />
        <Route path="/admin/onboarding" element={<AdminOnboardingPage />} />
        <Route path="/admin/offboarding" element={<AdminOffboardingPage />} />
        <Route path="/admin/holidays" element={<AdminHolidaysPage />} />
        <Route path="/admin/analytics" element={<AdminAnalyticsPage />} />
        <Route path="/admin/candidates" element={<AdminCandidatesPage />} />
        <Route path="/admin/audit" element={<AuditLogPage />} />
        <Route path="/admin/documents" element={<AdminDocumentsPage />} />
        <Route path="/admin/jobs" element={<AdminJobsPage />} />
        <Route path="/admin/reviews" element={<AdminReviewsPage />} />
        <Route path="/admin/salary-structures" element={<AdminSalaryStructuresPage />} />
        <Route path="/admin/payroll" element={<AdminPayrollPage />} />
        <Route path="/admin/reports" element={<AdminReportsPage />} />
        <Route path="/admin/tickets" element={<AdminTicketsPage />} />
        <Route path="/admin/users" element={<AdminUsersPage />} />
        <Route path="/admin/leaves" element={<LeaveApprovalsPage />} />
        <Route path="/documents" element={<DocumentsPage />} />
        <Route path="/expenses" element={<ExpensesPage />} />
        <Route path="/leaves" element={<LeavesPage />} />
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="/preboarding/:token" element={<PreboardingPage />} />
        <Route path="/tickets" element={<TicketsPage />} />
        <Route path="/regularization" element={<RegularizationPage />} />
        <Route path="/goals" element={<GoalsPage />} />

        <Route path="/login" element={<LoginPage />} />
        <Route path="/reset-password" element={<ResetPasswordPage />} />
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