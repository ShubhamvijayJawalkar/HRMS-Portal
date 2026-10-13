/**
 * `/goals` — personal performance goals (UI-08).
 *
 * **No employee picker, and that is a decision, not an omission.** The legacy
 * page fetched `/api/users?per_page=200` to populate a dropdown, but that
 * endpoint is `admin_required`: a plain Employee gets 403. So an employee
 * viewing the legacy page got a 403 on the dropdown and a form that silently
 * could not be submitted. `POST /api/goals` takes `emp_id` from the session
 * (CC-10), so there is nothing to pick — and the screen says so rather than
 * rendering a control that cannot work.
 *
 * Rating is also absent: `goals.check_rating` allows the owner's manager or
 * HR/Admin and **refuses the owner** (FR-PERF-01). A self-service screen with a
 * "Rate" button next to your own goal would offer an action the server
 * permanently rejects. The rating is displayed when it exists.
 */
import { useCallback, useState } from 'react';

import { PageHeader, Panel, RecordList, RefreshButton, StatusPill } from '@/components/Panel';
import { ValidationErrors } from '@/components/states';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { Goal } from '@/lib/types';

function todayIso(): string {
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60_000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 10);
}

export function GoalsPage() {
  const { actor } = useSession();
  const goals = usePanel<Goal[]>('/api/goals');
  const { run, pending } = useMutation();

  const [form, setForm] = useState({
    title: '',
    description: '',
    target_date: todayIso(),
    weight: 5,
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const viewer = actor?.emp_id ?? '';
  const rows = goals.data ?? [];
  const showOwner = rows.some((g) => g.emp_id !== viewer);

  const create = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setNotice(null);
      setErrors({});

      if (!form.title.trim()) {
        setErrors({ title: 'Give the goal a title.' });
        return;
      }
      if (!form.target_date) {
        setErrors({ target_date: 'A target date is required.' });
        return;
      }

      const result = await run(async () => {
        try {
          // `weight` is bounded 1-10 by the server (CC-12 validation).
          const created = await api.post<{ id: number; message?: string }>('/api/goals', {
            title: form.title.trim(),
            description: form.description.trim(),
            target_date: form.target_date,
            weight: Number(form.weight),
          });
          return { ok: true as const, message: created.message };
        } catch (err) {
          return {
            ok: false as const,
            errors:
              err instanceof ApiError && err.payload && typeof err.payload === 'object'
                ? (err.payload as Record<string, string>)
                : { _: err instanceof ApiError ? err.message : 'Could not create the goal.' },
          };
        }
      });

      if (!result) return;
      if (result.ok) {
        setForm({ title: '', description: '', target_date: todayIso(), weight: 5 });
        setNotice(result.message ?? 'Goal created.');
        goals.reload();
      } else {
        setErrors(result.errors);
      }
    },
    [form, run, goals],
  );

  return (
    <div className="page">
      <PageHeader
        title="Goals"
        subtitle="Set personal performance goals. Your manager rates them, not you."
        actions={<RefreshButton onClick={goals.reload} pending={pending} />}
      />

      {notice ? (
        <p className="notice notice--ok" role="status">
          {notice}
        </p>
      ) : null}

      <Panel
        title="Set a goal"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <form className="form" onSubmit={(event) => void create(event)} noValidate>
            <ValidationErrors errors={errors} />
            <div className="field">
              <label htmlFor="g-title">Title</label>
              <input
                id="g-title"
                value={form.title}
                aria-invalid={Boolean(errors.title) || undefined}
                onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))}
              />
              {errors.title ? <p className="field__error">{errors.title}</p> : null}
            </div>
            <div className="form__row">
              <div className="field">
                <label htmlFor="g-target">Target date</label>
                <input
                  id="g-target"
                  type="date"
                  value={form.target_date}
                  aria-invalid={Boolean(errors.target_date) || undefined}
                  onChange={(e) => setForm((f) => ({ ...f, target_date: e.target.value }))}
                />
                {errors.target_date ? (
                  <p className="field__error">{errors.target_date}</p>
                ) : null}
              </div>
              <div className="field">
                <label htmlFor="g-weight">Weight (1–10)</label>
                <input
                  id="g-weight"
                  type="number"
                  min={1}
                  max={10}
                  step={1}
                  value={form.weight}
                  aria-invalid={Boolean(errors.weight) || undefined}
                  onChange={(e) => setForm((f) => ({ ...f, weight: Number(e.target.value) }))}
                />
                {errors.weight ? <p className="field__error">{errors.weight}</p> : null}
              </div>
            </div>
            <div className="field">
              <label htmlFor="g-description">Description</label>
              <textarea
                id="g-description"
                rows={3}
                value={form.description}
                onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
              />
            </div>
            <button type="submit" className="btn" disabled={pending}>
              {pending ? 'Saving…' : 'Create goal'}
            </button>
          </form>
        )}
      </Panel>

      <Panel
        title={showOwner ? 'Goals' : 'Your goals'}
        data={rows}
        error={goals.error}
        loading={goals.loading}
        reload={goals.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(g) => g.id}
            emptyTitle="No goals yet."
            emptyHint="Set one above; your manager rates it at review time."
            render={(goal) => (
              <div className="leave-row">
                <div>
                  <StatusPill status={goal.status} />{' '}
                  <strong>{goal.title}</strong>
                  {showOwner && goal.emp_id !== viewer ? (
                    <span className="list__meta list__meta--owner">
                      {goal.employee || goal.emp_id}
                    </span>
                  ) : null}
                  <span className="list__meta">
                    {goal.description ? `${goal.description} · ` : ''}
                    target {goal.target_date ?? '—'}
                    {goal.weight != null ? ` · weight ${goal.weight}` : ''}
                    {goal.rating != null ? ` · rated ${goal.rating}/5` : ' · not yet rated'}
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