/**
 * `/admin/leaves` — the HR/admin leave approval queue (UI-09).
 *
 * Reaches parity with `templates/admin_leaves.html`: a status filter plus
 * approve/reject actions for pending requests. `GET /api/leaves` is scoped by
 * policy, so an admin sees the company's requests and a manager sees their own
 * approval queue; the React side does not second-guess which one.
 */
import { useCallback, useState } from 'react';

import { PageHeader, Panel, RecordList, StatusPill } from '@/components/Panel';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { LeaveRequest } from '@/lib/types';

const FILTERS = ['Pending', 'Approved', 'Rejected', ''];

export function LeaveApprovalsPage() {
  const { actor } = useSession();
  const { run, pending } = useMutation();
  const [filter, setFilter] = useState<string>('Pending');
  const [error, setError] = useState<string | null>(null);

  const path = filter ? `/api/leaves?status=${filter}` : '/api/leaves';
  const leaves = usePanel<LeaveRequest[]>(path);

  const review = useCallback(
    async (id: number, action: 'approve' | 'reject') => {
      setError(null);
      try {
        await run(async () => {
          if (action === 'approve') {
            await api.post(`/api/leaves/${id}/approve`);
          } else {
            await api.post(`/api/leaves/${id}/reject`);
          }
          return null;
        });
      } catch (err) {
        setError(err instanceof ApiError ? err.message : `Could not ${action} the leave.`);
      } finally {
        leaves.reload();
      }
    },
    [leaves, run],
  );

  const rows = leaves.data ?? [];

  return (
    <div className="page">
      <PageHeader
        title="Leave approvals"
        subtitle="Review and decide the leave queue."
      />

      <div className="tab-row" role="tablist" aria-label="Leave status">
        {FILTERS.map((value) => {
          const label = value || 'All';
          return (
            <button
              key={label}
              type="button"
              role="tab"
              aria-selected={filter === value}
              className={`tab ${filter === value ? 'is-active' : ''}`}
              onClick={() => setFilter(value)}
            >
              {label}
            </button>
          );
        })}
      </div>

      {error ? (
        <p className="notice notice--bad" role="alert">
          {error}
        </p>
      ) : null}

      <Panel
        title="Leave requests"
        data={rows}
        error={leaves.error}
        loading={leaves.loading}
        reload={leaves.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(leave) => leave.leave_id}
            emptyTitle="No leave requests."
            emptyHint="Nothing matches this filter."
            render={(leave) => (
              <div className="leave-row">
                <div>
                  <strong>{leave.emp_name || leave.emp_id}</strong>
                  <span className="list__meta">
                    {' '}
                    · {leave.leave_type} · {leave.start_date} → {leave.end_date}
                    {leave.reason ? ` · ${leave.reason}` : ''}
                  </span>
                </div>
                <div className="field__actions">
                  <StatusPill status={leave.status} />
                  {leave.status === 'Pending' ? (
                    <>
                      <button
                        type="button"
                        className="btn btn--small"
                        disabled={pending}
                        onClick={() => void review(leave.leave_id, 'approve')}
                      >
                        Approve
                      </button>
                      <button
                        type="button"
                        className="btn btn--small btn--danger"
                        disabled={pending}
                        onClick={() => void review(leave.leave_id, 'reject')}
                      >
                        Reject
                      </button>
                    </>
                  ) : null}
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
