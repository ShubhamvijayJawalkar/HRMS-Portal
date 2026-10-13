/** Shapes returned by the backend. Kept close to the API, not aspirational. */

export interface Actor {
  emp_id: string;
  name: string;
  role: string | null;
  department: string | null;
}

export interface NavChild {
  href: string;
  label: string;
  icon: string;
  module: string;
}

export interface NavEntry {
  href: string;
  label: string;
  icon: string;
  always?: boolean;
  group?: string;
  children?: NavChild[];
}

/** `GET /api/navigation` */
export interface NavigationPayload {
  actor: Actor;
  navigation: NavEntry[];
  sees_admin_surface: boolean;
}

export interface DashboardStats {
  [key: string]: unknown;
}

export interface ShiftSummary {
  shift_hours?: number;
  estimated?: boolean;
  capped?: boolean;
  scheduled_hours?: number;
  shift_configured?: boolean;
  total_break_minutes?: number;
  efficiency?: number;
  date?: string;
  [key: string]: unknown;
}

/**
 * A break type as the summary reports it.
 *
 * `daily_limit_minutes: null` means **no cap**, not zero — `remaining_minutes`
 * is null in that case too, and a UI that treats it as 0 disables every uncapped
 * break type permanently.
 */
export interface BreakType {
  break_type: string;
  description?: string | null;
  daily_limit_minutes: number | null;
  used_minutes: number;
  recorded_minutes?: number;
  active_minutes?: number;
  remaining_minutes: number | null;
  requires_approval: boolean;
  approval_status?: string | null;
}

export interface BreakRecord {
  break_id: number;
  break_type: string;
  status: 'Active' | 'Completed' | (string & {});
  start_time: string;
  end_time?: string | null;
  duration_minutes?: number | null;
}

export interface BreakSummaryPayload {
  break_types: BreakType[];
  breaks: BreakRecord[];
  /** The *shift* date, not the calendar date — they differ across midnight. */
  date?: string;
}

export interface AppNotification {
  id?: number;
  type?: string;
  category?: string | null;
  message?: string;
  is_read?: boolean | number;
  link?: string | null;
  created_at?: string;
  [key: string]: unknown;
}

/**
 * `GET /api/notifications` returns an **object**, not an array:
 * `{unread: number, data: [...]}`.
 *
 * This was modelled as a bare array first and the dashboard panel rendered
 * "You have no notifications" beside a live unread notification, because
 * `Array.isArray(payload)` was false and the fallback branch quietly produced
 * an empty list. A wrong shape does not throw here — it renders an empty state,
 * which is indistinguishable from a genuine one. Verified against the live
 * endpoint before it was written down here.
 */
export interface NotificationsPayload {
  unread: number;
  data: AppNotification[];
}

export interface LeaveBalanceRow {
  leave_type: string;
  total_days: number;
  used_days: number;
  reserved_days: number;
  remaining: number;
  /** `default` (published matrix), `policy`, or `policy+grants`. */
  source?: string;
}

export interface LeaveRequest {
  leave_id: number;
  emp_id: string;
  emp_name?: string;
  leave_type: string;
  start_date: string;
  end_date: string;
  reason?: string | null;
  status: string;
  approved_by?: string | null;
  created_at?: string | null;
  session?: string;
}

export interface Holiday {
  id?: number;
  name?: string;
  date?: string;
  holiday_date?: string;
  type?: string;
  [key: string]: unknown;
}

/** `GET /api/holidays` — `{holidays, filters}` on the current build. */
export interface HolidaysPayload {
  holidays: Holiday[];
  filters?: Record<string, unknown>;
}

export interface LeaveApplyResult {
  message?: string;
  leave_id: number;
  /** Working days charged. FR-LEA-09: Fri-to-Mon costs 2, not 4. */
  days_requested: number;
  /** Raw calendar span, so the employee can see what the two figures mean. */
  calendar_days: number;
  session?: string;
}

/** The fields `page.current_actor()` already exposes to every template. */
export interface Context {
  current_emp_id: string | null;
}

export interface CancelResult {
  message?: string;
  status?: string;
  /**
   * `release` returns a Pending reservation; `unconsume` takes approved days
   * back out of usage. Surfaced because "how much of my leave did this give
   * back" is the question an employee actually has after cancelling.
   */
  ledger?: string;
}

/**
 * `GET /api/profile` for one's own record.
 *
 * `allow_login` / `allow_breaks` are `1`/`0` integers on the compatibility
 * schema and real booleans on the v2.0 target. They are typed `boolean | number`
 * because a truthiness test that assumes either would break on the other, and
 * both shapes are live in this deployment.
 */
export interface Profile {
  emp_id: string;
  name?: string | null;
  email?: string | null;
  phone?: string | null;
  role?: string | null;
  department?: string | null;
  designation?: string | null;
  address?: string | null;
  date_of_birth?: string | null;
  date_of_joining?: string | null;
  emergency_contact_name?: string | null;
  emergency_contact_phone?: string | null;
  manager_emp_id?: string | null;
  allow_login?: boolean | number;
  allow_breaks?: boolean | number;
  [key: string]: unknown;
}

export interface Dependent {
  id: number;
  name: string;
  relationship: string;
  date_of_birth?: string | null;
}

/** `GET /api/mfa/status`. MFA is deferred by the migration plan; read-only here. */
export interface MfaStatus {
  emp_id: string;
  enrolled: boolean;
  enabled: boolean;
  enforced: boolean;
  mandatory: boolean;
  role?: string;
  stored?: boolean;
}

export interface Ticket {
  id: number;
  subject: string;
  description?: string | null;
  category?: string | null;
  priority?: string | null;
  status: string;
  emp_id?: string;
  employee?: string;
  assigned_to?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface Regularization {
  id: number;
  date: string;
  reason: string;
  status: string;
  emp_id?: string;
  approved_by?: string | null;
  created_at?: string | null;
}

export interface Goal {
  id: number;
  title: string;
  description?: string | null;
  target_date?: string | null;
  weight?: number | null;
  status: string;
  rating?: number | null;
  emp_id?: string;
  employee?: string;
  created_at?: string | null;
}

export interface ExpenseCategory {
  id: number;
  name: string;
  description?: string | null;
}

export interface Expense {
  id: number;
  cat_id: number;
  category?: string | null;
  amount: number;
  description?: string | null;
  status: string;
  has_receipt?: boolean;
  emp_id?: string;
  employee?: string;
  created_at?: string | null;
  /** Which transitions the *server* permits from this state for this actor. */
  allowed_actions?: string[];
}

export interface DocumentRecord {
  id: number;
  file_name: string;
  doc_type?: string | null;
  uploaded_at?: string | null;
}

export interface LoginSession {
  session_id?: number;
  login_time?: string;
  logout_time?: string | null;
  total_hours?: number;
  [key: string]: unknown;
}