/**
 * Break panel — the pilot's first write workflow.
 *
 * Reaches parity with the Jinja panel: the break-type buttons with their usage,
 * the active break with a live elapsed timer and an End button, today's history,
 * and the 10-second refresh.
 *
 * **One deliberate difference: no `localStorage`.** The Jinja panel stored
 * `activeBreakId` in localStorage and needed a self-heal — `endBreak()` had to
 * re-fetch the summary whenever the id was missing, because a stale or cleared
 * value left End Break pointing at a break that was over. Here the active break
 * is derived from the same summary payload that renders the panel, so the two
 * cannot disagree and the self-heal is not needed at all. The server stays the
 * only source of truth for what is running.
 *
 * **Every refusal the server can issue is rendered, not swallowed.** Starting a
 * break can be refused because breaks are not allowed for the employee (403), the
 * type is invalid (400), the daily limit is spent (400), the type needs manager
 * approval (403), or another break is still active (409). Each carries a distinct
 * message from the server and each is worth showing verbatim: "you have used your
 * 15 minutes of Tea" and "Lunch needs manager approval" are different problems
 * with different next steps.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { EmptyState, ErrorState, Loading } from '@/components/states';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import type { BreakRecord, BreakSummaryPayload, BreakType } from '@/lib/types';

/** How often the panel re-reads the summary. Matches the legacy 10s cadence. */
const POLL_MS = 10_000;

function elapsedMinutes(startTime: string, now: number): number {
  const started = Date.parse(startTime);
  if (Number.isNaN(started)) return 0;
  const diff = now - started;
  // A start_time in the future means the two clocks disagree; showing a negative
  // timer is worse than showing zero, and the server's own row is authoritative.
  return diff <= 0 ? 0 : Math.floor(diff / 60_000);
}

function formatClock(iso: string | null | undefined): string {
  if (!iso) return '—';
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return '—';
  return new Date(parsed).toLocaleTimeString('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function usageLabel(type: BreakType): string {
  const used = type.used_minutes ?? 0;
  // `daily_limit_minutes` is null for an uncapped type. Rendering "0/undefined
  // min used" here would tell the employee their uncapped break has a zero
  // allowance.
  return type.daily_limit_minutes == null
    ? `${used} min used · no daily cap`
    : `${used}/${type.daily_limit_minutes} min used`;
}

export function BreakPanel() {
  const summary = usePanel<BreakSummaryPayload>('/api/user/break-summary', {
    pollMs: POLL_MS,
  });
  const { run, pending } = useMutation();

  const [notice, setNotice] = useState<{ tone: 'ok' | 'bad'; text: string } | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const endButtonRef = useRef<HTMLButtonElement>(null);

  const types = summary.data?.break_types ?? [];
  const breaks = summary.data?.breaks ?? [];
  const active = useMemo<BreakRecord | undefined>(
    () => breaks.find((b) => b.status === 'Active'),
    [breaks],
  );

  // A local tick so the elapsed timer advances between polls. Without it the
  // figure would only refresh every 10 s, which reads as a broken timer.
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);

  const start = useCallback(
    async (breakType: string) => {
      const result = await run(async () => {
        try {
          const created = await api.post<{ break_id: number; break_type: string }>(
            '/api/start-break',
            { break_type: breakType },
          );
          return { ok: true as const, text: `${created.break_type} break started.` };
        } catch (err) {
          // The server's message is the useful one — it distinguishes an
          // exhausted allowance from a missing approval.
          const text =
            err instanceof ApiError ? err.message : 'Could not start the break.';
          return { ok: false as const, text };
        }
      });
      if (result) setNotice({ tone: result.ok ? 'ok' : 'bad', text: result.text });
      summary.reload();
    },
    [run, summary],
  );

  const end = useCallback(async () => {
    if (!active) return;
    const result = await run(async () => {
      try {
        const ended = await api.post<{ duration_minutes: number }>(
          `/api/end-break/${active.break_id}`,
        );
        return {
          ok: true as const,
          text: `Break ended after ${ended.duration_minutes ?? 0} min.`,
        };
      } catch (err) {
        const text = err instanceof ApiError ? err.message : 'Could not end the break.';
        return { ok: false as const, text };
      }
    });
    if (result) setNotice({ tone: result.ok ? 'ok' : 'bad', text: result.text });
    summary.reload();
  }, [active, run, summary]);

  /*
   * Focus moves to End Break once a break becomes active. Starting a break
   * replaces this panel's content, so without this a keyboard or screen-reader
   * user is left focused on a button that no longer exists — their next Tab goes
   * somewhere unpredictable.
   */
  useEffect(() => {
    if (active) endButtonRef.current?.focus();
  }, [active?.break_id]);

  return (
    <section className="panel panel--wide" aria-label="Breaks">
      <header className="panel__header">
        <h2 className="panel__title">Breaks</h2>
        <button type="button" className="btn btn--small" onClick={summary.reload}>
          Refresh
        </button>
      </header>

      <div className="panel__body">
        {summary.loading ? <Loading label="Loading breaks…" /> : null}

        {summary.error ? (
          <ErrorState error={summary.error} onRetry={summary.reload} />
        ) : null}

        {!summary.loading && !summary.error && summary.data ? (
          <>
            {/*
              `role="status"` on the success and `role="alert"` on the failure:
              both are announced, and the failure interrupts because a refused
              break is the thing the employee needs to know about immediately.
            */}
            <div aria-live="polite">
              {notice?.tone === 'ok' ? (
                <p className="notice notice--ok" role="status">
                  {notice.text}
                </p>
              ) : null}
              {notice?.tone === 'bad' ? (
                <p className="notice notice--bad" role="alert">
                  {notice.text}
                </p>
              ) : null}
            </div>

            <div className="break-layout">
              <div>
                <h3 className="panel__subhead" id="break-start-heading">
                  Start a break
                </h3>
                {types.length === 0 ? (
                  <EmptyState
                    title="No break types are available to you."
                    hint="An administrator configures these."
                  />
                ) : (
                  <div
                    className="break-buttons"
                    role="group"
                    aria-labelledby="break-start-heading"
                  >
                    {types.map((type) => {
                      // null means uncapped, so it is NOT exhausted. Only a
                      // literal 0 is.
                      const exhausted = type.remaining_minutes === 0;
                      const needsApproval = type.requires_approval;
                      const description = [
                        usageLabel(type),
                        needsApproval
                          ? type.approval_status
                            ? `Approval ${String(type.approval_status).toLowerCase()}`
                            : 'Requires manager approval'
                          : 'No approval required',
                      ].join(' · ');

                      return (
                        <button
                          key={type.break_type}
                          type="button"
                          className="btn break-btn"
                          disabled={exhausted || pending}
                          aria-describedby={`break-desc-${type.break_type}`}
                          title={description}
                          onClick={() => void start(type.break_type)}
                        >
                          {type.break_type}
                        </button>
                      );
                    })}
                  </div>
                )}
                <ul className="sr-only" aria-live="polite">
                  {types.map((type) => (
                    <li key={type.break_type} id={`break-desc-${type.break_type}`}>
                      {`${type.break_type}: ${usageLabel(type)}. ${
                        type.requires_approval
                          ? `Approval ${String(type.approval_status ?? 'pending').toLowerCase()}.`
                          : 'No approval required.'
                      }${type.remaining_minutes === 0 ? ' Allowance spent for today.' : ''}`}
                    </li>
                  ))}
                </ul>
              </div>

              <div>
                <h3 className="panel__subhead">Active break</h3>
                {active ? (
                  <div className="break-active">
                    <p className="break-active__name">{active.break_type}</p>
                    <p className="break-active__meta">
                      Started {formatClock(active.start_time)}
                    </p>
                    <p className="break-active__elapsed">
                      {elapsedMinutes(active.start_time, now)} min
                    </p>
                    <button
                      ref={endButtonRef}
                      type="button"
                      className="btn btn--danger"
                      disabled={pending}
                      onClick={() => void end()}
                    >
                      End break
                    </button>
                  </div>
                ) : (
                  <p className="metric__note">No active break.</p>
                )}
              </div>
            </div>

            <h3 className="panel__subhead">Break types</h3>
            <ul className="list">
              {types.map((type) => (
                <li key={type.break_type} className="list__item">
                  <strong>{type.break_type}</strong>
                  <span className="list__meta">
                    {type.description ? `${type.description} · ` : ''}
                    {usageLabel(type)} ·{' '}
                    {type.requires_approval
                      ? `Approval required${
                          type.approval_status
                            ? ` (${String(type.approval_status).toLowerCase()})`
                            : ''
                        }`
                      : 'No approval required'}
                  </span>
                </li>
              ))}
            </ul>

            <h3 className="panel__subhead">
              {summary.data.date ? `Breaks on ${summary.data.date}` : "Today's breaks"}
            </h3>
            {breaks.length === 0 ? (
              <p className="metric__note">No breaks this shift.</p>
            ) : (
              <ul className="list">
                {breaks.map((record) => (
                  <li key={record.break_id} className="list__item">
                    <span className={`status status--${record.status.toLowerCase()}`}>
                      {record.status}
                    </span>{' '}
                    <strong>{record.break_type}</strong>
                    <span className="list__meta">
                      {formatClock(record.start_time)} –{' '}
                      {record.end_time ? formatClock(record.end_time) : 'now'}
                      {record.duration_minutes != null ? ` · ${record.duration_minutes} min` : ''}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : null}
      </div>
    </section>
  );
}