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
  name: string;
  file_name?: string | null;
  doc_type?: string | null;
  category?: string | null;
  file_size?: number | null;
  uploaded_at?: string | null;
  emp_id?: string;
  employee?: string;
}

export interface LoginSession {
  session_id?: number;
  login_time?: string;
  logout_time?: string | null;
  total_hours?: number;
  [key: string]: unknown;
}

export interface AuditLogEntry {
  log_id?: number;
  created_at?: string | null;
  emp_id?: string | null;
  action?: string;
  details?: string | null;
  ip_address?: string | null;
  [key: string]: unknown;
}

export interface AuditLogPayload {
  data: AuditLogEntry[];
  total: number;
}

export interface AssetRecord {
  id: number;
  emp_id: string;
  employee?: string;
  type: string;
  tag?: string | null;
  brand?: string | null;
  model?: string | null;
  serial?: string | null;
  issued?: string | null;
  returned?: string | null;
  status: string;
  notes?: string | null;
}

export interface JobRecord {
  id: number;
  title: string;
  department?: string | null;
  location?: string | null;
  description?: string | null;
  status: string;
  created_at?: string | null;
}

export interface ReviewRecord {
  id: number;
  emp_id: string;
  employee: string;
  reviewer_id: string;
  reviewer: string;
  period: string;
  rating?: number | null;
  status: string;
  submitted_at?: string | null;
}

export interface CandidateRecord {
  id: number;
  name: string;
  email: string;
  phone?: string | null;
  job_id?: number | null;
  job_title?: string | null;
  status: string;
  applied_at?: string | null;
  [key: string]: unknown;
}

export interface ImportJob {
  job_id: number;
  filename?: string;
  status: string;
  total_rows?: number;
  processed_rows?: number;
  imported?: number;
  skipped?: number;
  errors?: string[] | null;
  failure_reason?: string | null;
  cancel_requested?: boolean | number;
}

export interface OnboardingWorkflow {
  workflow_id: number;
  emp_id: string;
  employee: string;
  current_step?: number;
  completed?: boolean;
  steps?: Record<string, string>;
  days_in_step?: number;
  checklist?: Array<{
    id: number;
    doc_type: string;
    status: string;
    uploaded_at?: string | null;
    reviewed_by?: string | null;
    review_note?: string | null;
    reviewed_at?: string | null;
  }>;
  tasks?: Array<{
    id: number;
    task: string;
    assigned_to?: string | null;
    status: string;
    due_date?: string | null;
    completed_at?: string | null;
    stage?: number;
  }>;
  [key: string]: unknown;
}

export interface PayrollRun {
  id: number;
  month: number;
  year: number;
  status: string;
  processed_at?: string | null;
  submitted_by?: string | null;
  approved_by?: string | null;
  finalized_at?: string | null;
  [key: string]: unknown;
}

export interface PayrollItem {
  id: number;
  emp_id: string;
  employee: string;
  gross: number;
  deductions: number;
  net: number;
  pf: number;
  esi: number;
  pt: number;
  payslip_generated?: boolean;
  [key: string]: unknown;
}

export interface SalaryStructureRecord {
  id: number;
  emp_id: string;
  employee?: string;
  basic: number;
  hra: number;
  allowances: number;
  deductions: number;
  effective_from?: string | null;
  effective_to?: string | null;
  [key: string]: unknown;
}

export interface UserRecord {
  emp_id: string;
  name?: string | null;
  email?: string | null;
  role?: string;
  status?: string;
  department?: string | null;
  first_login?: string | null;
  locked?: boolean;
  attempts?: number;
  allow_login?: number;
  allow_breaks?: number;
  [key: string]: unknown;
}

export interface HolidayRecord {
  id: number;
  name: string;
  date: string;
  type: string;
  location?: string | null;
  year?: number;
  [key: string]: unknown;
}

export interface HolidayOptIn {
  optin_id: number;
  emp_id: string;
  holiday_id: number;
  status: string;
  created_at?: string | null;
  holiday_name?: string | null;
  holiday_date?: string | null;
  type?: string | null;
  [key: string]: unknown;
}

export interface OffboardingTaskRecord {
  id: number;
  emp_id: string;
  employee: string;
  task: string;
  assigned_to?: string | null;
  status: string;
  due_date?: string | null;
  completed_at?: string | null;
  stage?: number;
  [key: string]: unknown;
}

export interface OnboardingTaskRecord {
  id: number;
  emp_id: string;
  employee: string;
  task: string;
  assigned_to?: string | null;
  status: string;
  due_date?: string | null;
  completed_at?: string | null;
  stage?: number;
  [key: string]: unknown;
}