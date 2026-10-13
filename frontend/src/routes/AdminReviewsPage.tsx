/**
 * `/admin/reviews` — open and track performance reviews (UI-09).
 */
import { useCallback, useEffect, useState } from 'react';

import { PageHeader, Panel, RecordList, StatusPill } from '@/components/Panel';
import { ValidationErrors } from '@/components/states';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { ReviewRecord } from '@/lib/types';

interface UserOption {
  emp_id: string;
  name?: string | null;
}
interface UserListPayload {
  data: UserOption[];
}

export function AdminReviewsPage() {
  const { actor } = useSession();
  const reviews = usePanel<ReviewRecord[]>('/api/performance-reviews');
  const users = usePanel<UserListPayload>('/api/users?active=1');
  const { run, pending } = useMutation();

  const [form, setForm] = useState({ emp_id: '', reviewer_id: '', period: '' });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const options = users.data?.data ?? [];
  useEffect(() => {
    if (options.length && !form.emp_id) {
      setForm((f) => ({ ...f, emp_id: options[0]?.emp_id ?? f.emp_id }));
    }
    if (options.length && !form.reviewer_id) {
      setForm((f) => ({ ...f, reviewer_id: options[0]?.emp_id ?? f.reviewer_id }));
    }
  }, [form.emp_id, form.reviewer_id, options]);

  const create = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setNotice(null);
      setErrors({});
      if (!form.emp_id || !form.reviewer_id || !form.period.trim()) {
        setErrors({ _: 'Employee, reviewer and period are required.' });
        return;
      }
      const result = await run(async () => {
        try {
          const created = await api.post<{ id?: number; message?: string }>('/api/performance-reviews', {
            emp_id: form.emp_id,
            reviewer_id: form.reviewer_id,
            review_period: form.period.trim(),
          });
          return { ok: true as const, message: created?.message ?? 'Review created.' };
        } catch (err) {
          const message = err instanceof ApiError ? err.message : 'Could not create the review.';
          return { ok: false as const, message };
        }
      });
      if (!result) return;
      if (result.ok) {
        setNotice(result.message);
        setForm((f) => ({ ...f, period: '' }));
        reviews.reload();
      } else {
        setErrors({ _: result.message });
      }
    },
    [form, reviews, run],
  );

  const rows = reviews.data ?? [];

  return (
    <div className="page">
      <PageHeader title="Reviews" subtitle="Open and track performance reviews." />

      {notice ? (
        <p className="notice notice--ok" role="status">
          {notice}
        </p>
      ) : null}

      <Panel
        title="New review"
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
                <label htmlFor="review-employee">Employee</label>
                <select
                  id="review-employee"
                  value={form.emp_id}
                  onChange={(e) => setForm((f) => ({ ...f, emp_id: e.target.value }))}
                >
                  {options.map((user) => (
                    <option key={user.emp_id} value={user.emp_id}>
                      {user.name || user.emp_id} ({user.emp_id})
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="review-reviewer">Reviewer</label>
                <select
                  id="review-reviewer"
                  value={form.reviewer_id}
                  onChange={(e) => setForm((f) => ({ ...f, reviewer_id: e.target.value }))}
                >
                  {options.map((user) => (
                    <option key={user.emp_id} value={user.emp_id}>
                      {user.name || user.emp_id} ({user.emp_id})
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="review-period">Period</label>
                <input
                  id="review-period"
                  value={form.period}
                  onChange={(e) => setForm((f) => ({ ...f, period: e.target.value }))}
                  placeholder="Q1 2026"
                />
              </div>
              <button type="submit" className="btn" disabled={pending || users.loading}>
                {pending ? 'Creating…' : 'Create'}
              </button>
            </div>
          </form>
        )}
      </Panel>

      <Panel
        title="All reviews"
        data={rows}
        error={reviews.error}
        loading={reviews.loading}
        reload={reviews.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(review) => review.id}
            emptyTitle="No reviews."
            emptyHint="Open a review above and it will appear here."
            render={(review) => (
              <div className="leave-row">
                <div>
                  <StatusPill status={review.status} />{' '}
                  <strong>{review.employee}</strong>
                  <span className="list__meta">
                    {' '}
                    · reviewer {review.reviewer}
                    {review.period ? ` · ${review.period}` : ''}
                    {review.rating != null ? ` · ${review.rating.toFixed(1)} / 5` : ''}
                    {review.submitted_at
                      ? ` · submitted ${new Date(review.submitted_at).toLocaleDateString()}`
                      : ''}
                  </span>
                </div>
              </div>
            )}
          />
        )}
      </Panel>

      {actor?.emp_id ? null : null}
    </div>
  );
}
