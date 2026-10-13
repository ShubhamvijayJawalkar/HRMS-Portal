/**
 * `/regularization` — request a correction to a missing/incorrect attendance
 * record (UI-08).
 *
 * The one thing worth knowing here is the **field name**: the create payload key
 * is `date`, not `attendance_date`. `POST /api/regularization` answers
 * `"date and reason required"` (400) for the latter, so inferring the contract
 * from the legacy template's variable naming silently costs a whole form. The
 * shape was read off a live request.
 */
import { useCallback, useState } from 'react';

import { PageHeader, Panel, RecordList, RefreshButton, StatusPill } from '@/components/Panel';
import { ValidationErrors } from '@/components/states';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { Regularization } from '@/lib/types';

function todayIso(): string {
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60_000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 10);
}

export function RegularizationPage() {
  const { actor } = useSession();
  const requests = usePanel<Regularization[]>('/api/regularization');
  const { run, pending } = useMutation();

  const [form, setForm] = useState({ date: todayIso(), reason: '' });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const viewer = actor?.emp_id ?? '';
  const rows = requests.data ?? [];
  const showOwner = rows.some((r) => r.emp_id !== viewer);

  const create = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setNotice(null);
      setErrors({});

      if (!form.date) {
        setErrors({ date: 'Pick the date to correct.' });
        return;
      }
      if (!form.reason.trim()) {
        setErrors({ reason: 'A reason is required — it is what the approver reads.' });
        return;
      }

      const result = await run(async () => {
        try {
          // `date`, not `attendance_date`. Verified against the live endpoint.
          const created = await api.post<{ id: number; message?: string }>(
            '/api/regularization',
            { date: form.date, reason: form.reason.trim() },
          );
          return { ok: true as const, message: created.message };
        } catch (err) {
          return {
            ok: false as const,
            errors:
              err instanceof ApiError && err.payload && typeof err.payload === 'object'
                ? (err.payload as Record<string, string>)
                : { _: err instanceof ApiError ? err.message : 'Could not submit.' },
          };
        }
      });

      if (!result) return;
      if (result.ok) {
        setForm({ date: todayIso(), reason: '' });
        setNotice(result.message ?? 'Regularization request submitted.');
        requests.reload();
      } else {
        setErrors(result.errors);
      }
    },
    [form, run, requests],
  );

  return (
    <div className="page">
      <PageHeader
        title="Regularization"
        subtitle="Correct a missing or incorrect attendance record."
        actions={<RefreshButton onClick={requests.reload} pending={pending} />}
      />

      {notice ? (
        <p className="notice notice--ok" role="status">
          {notice}
        </p>
      ) : null}

      <Panel
        title="Request a correction"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <form className="form" onSubmit={(event) => void create(event)} noValidate>
            <ValidationErrors errors={errors} />
            <div className="form__row">
              <div className="field">
                <label htmlFor="r-date">Attendance date</label>
                <input
                  id="r-date"
                  type="date"
                  value={form.date}
                  aria-invalid={Boolean(errors.date) || undefined}
                  onChange={(e) => setForm((f) => ({ ...f, date: e.target.value }))}
                />
                {errors.date ? <p className="field__error">{errors.date}</p> : null}
              </div>
              <div className="field">
                <label htmlFor="r-reason">Reason</label>
                <input
                  id="r-reason"
                  value={form.reason}
                  placeholder="e.g. Forgot to punch in"
                  aria-invalid={Boolean(errors.reason) || undefined}
                  onChange={(e) => setForm((f) => ({ ...f, reason: e.target.value }))}
                />
                {errors.reason ? <p className="field__error">{errors.reason}</p> : null}
              </div>
            </div>
            <button type="submit" className="btn" disabled={pending}>
              {pending ? 'Submitting…' : 'Submit request'}
            </button>
          </form>
        )}
      </Panel>

      <Panel
        title={showOwner ? 'Regularization requests' : 'Your requests'}
        data={rows}
        error={requests.error}
        loading={requests.loading}
        reload={requests.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(r) => r.id}
            emptyTitle="No regularization requests."
            emptyHint="Requests you submit will appear here with their decision."
            render={(item) => (
              <div className="leave-row">
                <div>
                  <StatusPill status={item.status} />
                  {showOwner && item.emp_id !== viewer ? (
                    <span className="list__meta list__meta--owner">{item.emp_id}</span>
                  ) : null}{' '}
                  <strong>{item.date}</strong>
                  <span className="list__meta">
                    {item.reason}
                    {item.approved_by ? ` · decided by ${item.approved_by}` : ''}
                  </span>
                </div>
              </div>
            )}
          />
        )}
      </Panel>
    </div>
  );
}