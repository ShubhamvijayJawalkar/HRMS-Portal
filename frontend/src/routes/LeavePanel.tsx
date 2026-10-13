/**
 * Leave panel — balance, request, cancel.
 *
 * Three things here are load-bearing and are easy to get wrong:
 *
 * 1. **The balance shows `reserved` as well as `used`.** FR-LEA-06 splits the
 *    ledger three ways, and `reserved` is the part an employee cannot otherwise
 *    see: while a request is Pending its days are held, not spent. Omitting the
 *    column makes the remaining figure look like it dropped for no reason.
 *
 * 2. **`source` is displayed.** `GET /api/leave-balance` reports whether a
 *    number came from the published matrix, a policy assignment, or a policy
 *    plus an HR grant. An employee asking "why is my annual leave 18 not 20"
 *    deserves the answer on the screen rather than in a database.
 *
 * 3. **Both day counts are shown after applying.** The response carries
 *    `days_requested` (working days charged) and `calendar_days` (raw span).
 *    FR-LEA-09 changed leave to count working days, so Friday-to-Monday now
 *    costs 2 days rather than 4 — and someone who used to pay 4 deserves to be
 *    told why it is 2 now. Showing only the working-day figure would look like a
 *    bug.
 */
import { useCallback, useMemo, useState } from 'react';

import { EmptyState, ErrorState, Loading, ValidationErrors } from '@/components/states';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type {
  CancelResult,
  LeaveApplyResult,
  LeaveBalanceRow,
  LeaveRequest,
} from '@/lib/types';

/** Mirrors `working_days.LEAVE_SESSIONS` on the server. */
const SESSIONS = ['Full', 'First-half', 'Second-half'] as const;

function todayIso(): string {
  const now = new Date();
  // Local, not UTC: `toISOString()` would roll back a day for anyone east of
  // Greenwich after 19:00, and the default start date would then be in the past.
  const offset = now.getTimezoneOffset() * 60_000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 10);
}

function addDays(iso: string, days: number): string {
  const parsed = new Date(`${iso}T00:00:00`);
  parsed.setDate(parsed.getDate() + days);
  const offset = parsed.getTimezoneOffset() * 60_000;
  return new Date(parsed.getTime() - offset).toISOString().slice(0, 10);
}

/** Server error bodies can carry field-level detail; surface what is there. */
function fieldErrors(error: ApiError): Record<string, string> {
  const payload = error.payload;
  if (!payload || typeof payload !== 'object') return { _: error.message };
  const errors: Record<string, string> = {};
  if ('start_date' in payload || 'hint' in payload) {
    errors.start_date = String((payload as { hint?: unknown }).hint ?? error.message);
  } else {
    errors._ = error.message;
  }
  return errors;
}

export function LeavePanel() {
  const { actor } = useSession();
  const balance = usePanel<LeaveBalanceRow[]>('/api/leave-balance');
  const mine = usePanel<LeaveRequest[]>('/api/leaves');
  const { run, pending } = useMutation();

  const [leaveType, setLeaveType] = useState('Casual');
  const [startDate, setStartDate] = useState(todayIso);
  const [endDate, setEndDate] = useState(() => addDays(todayIso(), 1));
  const [session, setSession] = useState<string>('Full');
  const [reason, setReason] = useState('');

  const [errors, setErrors] = useState<Record<string, string>>({});
  const [outcome, setOutcome] = useState<LeaveApplyResult | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [cancelError, setCancelError] = useState<string | null>(null);

  const balanceForType = useMemo(
    () => balance.data?.find((row) => row.leave_type === leaveType) ?? null,
    [balance.data, leaveType],
  );

  const refreshAll = useCallback(() => {
    balance.reload();
    mine.reload();
  }, [balance, mine]);

  const apply = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setErrors({});
      setOutcome(null);

      // Client-side checks exist to give an instant answer and to mark the
      // field; the server re-checks all of them, because it is the authority.
      const local: Record<string, string> = {};
      if (!startDate) local.start_date = 'Start date is required.';
      if (!endDate) local.end_date = 'End date is required.';
      if (startDate && endDate && endDate < startDate) {
        local.end_date = 'End date cannot be before the start date.';
      }
      if (Object.keys(local).length > 0) {
        setErrors(local);
        return;
      }

      const result = await run(async () => {
        try {
          const created = await api.post<LeaveApplyResult>('/api/leaves', {
            leave_type: leaveType,
            start_date: startDate,
            end_date: endDate,
            session,
            reason,
          });
          return { ok: true as const, created };
        } catch (err) {
          return {
            ok: false as const,
            errors:
              err instanceof ApiError
                ? fieldErrors(err)
                : { _: 'Could not submit the request.' },
          };
        }
      });

      if (!result) return;
      if (result.ok) {
        setOutcome(result.created);
        setReason('');
        refreshAll();
      } else {
        setErrors(result.errors);
      }
    },
    [leaveType, startDate, endDate, session, reason, run, refreshAll],
  );

  const cancel = useCallback(
    async (leave: LeaveRequest) => {
      setCancelError(null);
      const result = await run(async () => {
        try {
          const cancelled = await api.post<CancelResult>(
            `/api/leaves/${leave.leave_id}/cancel`,
          );
          return { ok: true as const, cancelled };
        } catch (err) {
          return {
            ok: false as const,
            text: err instanceof ApiError ? err.message : 'Could not cancel the request.',
          };
        }
      });

      if (!result) return;
      if (result.ok) {
        const howMuch =
          result.cancelled.ledger === 'release'
            ? 'The reserved days are back in your balance.'
            : result.cancelled.ledger === 'unconsume'
              ? 'The approved days have been taken back off your usage.'
              : '';
        setNotice(`Leave cancelled. ${howMuch}`.trim());
        // The submission confirmation is now stale: it describes a request
        // that no longer exists in that state. Leaving it up meant the panel
        // asserted "3 working days charged" beside a row reading Cancelled.
        setOutcome(null);
        refreshAll();
      } else {
        setCancelError(result.text);
      }
    },
    [run, refreshAll],
  );

  /*
   * Cancel is offered only where the server will accept it: Pending, or
   * Approved with a start date still in the future. The server refuses anything
   * else with a 409, and a button that always fails is worse than its absence —
   * but this is a *hint*, not the rule. The server decides; this only avoids
   * inviting a request that is certain to be refused.
   */
  const canCancel = (leave: LeaveRequest): boolean =>
    leave.status === 'Pending' ||
    (leave.status === 'Approved' && leave.start_date > todayIso());

  /*
   * The list heading has to be true.
   *
   * `GET /api/leaves` is scoped by policy: an employee receives their own
   * requests, an Admin receives the company's. Heading that list "Your requests"
   * regardless is a claim the client cannot make — and it was making it, so an
   * Admin reading their own dashboard saw four colleagues' leave under a
   * heading that said otherwise.
   *
   * The scope is *observed* from the payload rather than guessed from a role the
   * client would have to re-derive: if any row belongs to somebody else, the
   * list is company-wide and says so. That cannot be mistaken for an
   * authorization decision — nothing here gates an action on it.
   */
  const rows = mine.data ?? [];
  const viewerEmpId = actor?.emp_id ?? '';
  const showOwner = rows.some((leave) => leave.emp_id !== viewerEmpId);
  const requestsHeading = showOwner ? 'Leave requests' : 'Your requests';

  return (
    <section className="panel panel--wide" aria-label="Leave">
      <header className="panel__header">
        <h2 className="panel__title">Leave</h2>
        <button type="button" className="btn btn--small" onClick={refreshAll}>
          Refresh
        </button>
      </header>

      <div className="panel__body">
        <h3 className="panel__subhead">Your balance</h3>
        {balance.loading ? <Loading label="Loading balance…" /> : null}
        {balance.error ? <ErrorState error={balance.error} onRetry={balance.reload} /> : null}
        {!balance.loading && !balance.error ? (
          (balance.data?.length ?? 0) === 0 ? (
            <EmptyState title="No leave types are configured." />
          ) : (
            <table className="table">
              <caption className="sr-only">
                Leave entitlement, usage, reservation and remaining days by type
              </caption>
              <thead>
                <tr>
                  <th scope="col">Type</th>
                  <th scope="col">Total</th>
                  <th scope="col">Used</th>
                  <th scope="col">Reserved</th>
                  <th scope="col">Remaining</th>
                </tr>
              </thead>
              <tbody>
                {balance.data?.map((row) => (
                  <tr key={row.leave_type}>
                    <th scope="row">{row.leave_type}</th>
                    <td>{row.total_days}</td>
                    <td>{row.used_days}</td>
                    <td>{row.reserved_days}</td>
                    <td>
                      <strong>{row.remaining}</strong>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        ) : null}

        {balanceForType ? (
          <p className="metric__note">
            {balanceForType.leave_type} is {balanceForType.remaining} days remaining (
            {balanceForType.source === 'default'
              ? 'standard entitlement'
              : `from your ${balanceForType.source}`}
            ).
          </p>
        ) : null}

        <h3 className="panel__subhead">Request leave</h3>
        <form className="form" onSubmit={(event) => void apply(event)} noValidate>
          <ValidationErrors errors={errors} />

          {outcome ? (
            /*
             * `notice--applied` rather than a plain `notice--ok`, because the
             * panel also shows a cancellation confirmation further down. Two
             * identically-styled messages in one panel, describing different
             * actions, made it ambiguous which one a reader was looking at —
             * and made a browser assertion read the wrong one.
             */
            <p className="notice notice--ok notice--applied" role="status">
              {`Submitted: ${outcome.days_requested} working day${
                outcome.days_requested === 1 ? '' : 's'
              } charged${
                outcome.calendar_days !== outcome.days_requested
                  ? ` (the request spans ${outcome.calendar_days} calendar days; weekends and holidays are not charged)`
                  : ''
              }.`}
            </p>
          ) : null}

          <div className="form__row">
            <div className="field">
              <label htmlFor="leave-type">Leave type</label>
              <select
                id="leave-type"
                value={leaveType}
                onChange={(event) => setLeaveType(event.target.value)}
              >
                {(balance.data ?? []).map((row) => (
                  <option key={row.leave_type} value={row.leave_type}>
                    {row.leave_type}
                  </option>
                ))}
                {/* A type with no entitlement row still has to be requestable;
                    the server validates it, and an employee with no policy
                    assigned falls back to the published matrix. */}
                {(balance.data ?? []).some((row) => row.leave_type === leaveType) ? null : (
                  <option value={leaveType}>{leaveType}</option>
                )}
              </select>
            </div>

            <div className="field">
              <label htmlFor="leave-session">Session</label>
              <select
                id="leave-session"
                value={session}
                onChange={(event) => setSession(event.target.value)}
              >
                {SESSIONS.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="form__row">
            <div className="field">
              <label htmlFor="leave-start">Start date</label>
              <input
                id="leave-start"
                type="date"
                value={startDate}
                onChange={(event) => setStartDate(event.target.value)}
                aria-invalid={Boolean(errors.start_date) || undefined}
                aria-describedby={errors.start_date ? 'leave-start-error' : undefined}
              />
              {errors.start_date ? (
                <p className="field__error" id="leave-start-error">
                  {errors.start_date}
                </p>
              ) : null}
            </div>

            <div className="field">
              <label htmlFor="leave-end">End date</label>
              <input
                id="leave-end"
                type="date"
                value={endDate}
                onChange={(event) => setEndDate(event.target.value)}
                aria-invalid={Boolean(errors.end_date) || undefined}
                aria-describedby={errors.end_date ? 'leave-end-error' : undefined}
              />
              {errors.end_date ? (
                <p className="field__error" id="leave-end-error">
                  {errors.end_date}
                </p>
              ) : null}
            </div>
          </div>

          <div className="field">
            <label htmlFor="leave-reason">Reason</label>
            <textarea
              id="leave-reason"
              rows={2}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </div>

          <button type="submit" className="btn" disabled={pending}>
            {pending ? 'Submitting…' : 'Submit request'}
          </button>
        </form>

        <h3 className="panel__subhead">{requestsHeading}</h3>
        {mine.loading ? <Loading label="Loading requests…" /> : null}
        {mine.error ? <ErrorState error={mine.error} onRetry={mine.reload} /> : null}

        {notice ? (
          <p className="notice notice--ok notice--cancelled" role="status">
            {notice}
          </p>
        ) : null}
        {cancelError ? (
          <p className="notice notice--bad" role="alert">
            {cancelError}
          </p>
        ) : null}

        {!mine.loading && !mine.error && (mine.data?.length ?? 0) === 0 ? (
          <EmptyState title="No leave requests to show." />
        ) : null}

        {mine.data && mine.data.length > 0 ? (
          <ul className="list">
            {mine.data.map((leave) => {
              /*
               * Whose record this is. An Admin's `/api/leaves` is company-wide,
               * so the panel names the employee on every row rather than
               * implying the list is the caller's own — and `can_view_all` is
               * not consulted for the decision, only to avoid repeating the
               * employee id on rows that already carry it.
               */
              const isOwn = leave.emp_id === viewerEmpId;
              return (
                <li
                  key={leave.leave_id}
                  className="list__item leave-row"
                  data-emp-id={leave.emp_id}
                  data-own={isOwn ? 'true' : 'false'}
                >
                  <div>
                    <span className={`status status--${leave.status.toLowerCase()}`}>
                      {leave.status}
                    </span>{' '}
                    <strong>{leave.leave_type}</strong>
                    {showOwner && !isOwn ? (
                      <span className="list__meta list__meta--owner">
                        {leave.emp_name || leave.emp_id}
                      </span>
                    ) : null}
                    <span className="list__meta">
                      {leave.start_date} → {leave.end_date}
                      {leave.session ? ` · ${leave.session}` : ''}
                      {leave.reason ? ` · ${leave.reason}` : ''}
                      {leave.approved_by ? ` · approved by ${leave.approved_by}` : ''}
                    </span>
                  </div>
                  {canCancel(leave) ? (
                    <button
                      type="button"
                      className="btn btn--small"
                      disabled={pending}
                      onClick={() => void cancel(leave)}
                    >
                      Cancel
                    </button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : null}
      </div>
    </section>
  );
}