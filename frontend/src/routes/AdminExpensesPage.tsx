/**
 * `/admin/expenses` — review and move expense claims (UI-09).
 *
 * The list is `/api/expenses` (policy-scoped), and `allowed_actions` tells us
 * which transitions the backend will actually accept for that claim and actor.
 */
import { useCallback } from 'react';

import { PageHeader, Panel, RecordList, StatusPill } from '@/components/Panel';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { Expense } from '@/lib/types';

export function AdminExpensesPage() {
  const { actor } = useSession();
  const expenses = usePanel<Expense[]>('/api/expenses');
  const { run, pending } = useMutation();

  const move = useCallback(
    async (id: number, status: string) => {
      let reason: string | undefined;
      if (status === 'Rejected') {
        const input = window.prompt('Reason for rejection');
        if (input === null) return;
        reason = input;
      }
      try {
        await run(async () => {
          await api.put(`/api/expenses/${id}/status`, {
            status,
            ...(reason ? { reason } : {}),
          });
          return null;
        });
        expenses.reload();
      } catch (err) {
        window.alert(err instanceof ApiError ? err.message : 'Could not update the claim.');
      }
    },
    [expenses, run],
  );

  const formatMoney = (value: unknown): string =>
    typeof value === 'number' ? `₹${value.toFixed(2)}` : String(value ?? '');

  const rows = expenses.data ?? [];

  return (
    <div className="page">
      <PageHeader title="Expenses" subtitle="Review expense claims." />

      <Panel
        title="All claims"
        data={rows}
        error={expenses.error}
        loading={expenses.loading}
        reload={expenses.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(claim) => claim.id}
            emptyTitle="No claims."
            emptyHint="Filed claims will appear here."
            render={(claim) => (
              <div className="leave-row">
                <div>
                  <StatusPill status={claim.status} />{' '}
                  <strong>{claim.employee || claim.emp_id}</strong>
                  <span className="list__meta">
                    {' '}
                    · {claim.category || 'General'}
                    {claim.amount != null ? ` · ${formatMoney(claim.amount)}` : ''}
                    {claim.description ? ` · ${claim.description}` : ''}
                    {claim.created_at
                      ? ` · ${new Date(claim.created_at).toLocaleDateString()}`
                      : ''}
                    {claim.has_receipt ? ' · receipt attached' : ''}
                  </span>
                </div>
                <div className="field__actions">
                  {(claim.allowed_actions ?? []).map((action) => (
                    <button
                      key={action}
                      type="button"
                      className={`btn btn--small ${
                        action === 'Rejected' ? 'btn--danger' : 'btn--primary'
                      }`}
                      disabled={pending}
                      onClick={() => void move(claim.id, action)}
                    >
                      {action === 'Paid'
                        ? 'Mark Paid'
                        : action === 'Approved'
                        ? 'Approve'
                        : action}
                    </button>
                  ))}
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
