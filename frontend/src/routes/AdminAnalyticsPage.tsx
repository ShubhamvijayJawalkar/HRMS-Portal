/**
 * `/admin/analytics` — analytics snapshots (UI-09).
 */
import { useCallback, useState } from 'react';

import { PageHeader, Panel, RecordList } from '@/components/Panel';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';

function Snapshot({ data }: { data: unknown }) {
  if (Array.isArray(data)) {
    return (
      <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
        {JSON.stringify(data, null, 2)}
      </pre>
    );
  }
  if (data && typeof data === 'object') {
    return (
      <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
        {JSON.stringify(
          Object.keys(data).length ? data : {},
          null,
          2,
        )}
      </pre>
    );
  }
  return <p className="list__meta">{String(data ?? '')}</p>;
}

export function AdminAnalyticsPage() {
  const headcount = usePanel<unknown>('/api/analytics/headcount');
  const leaveTrends = usePanel<unknown>('/api/analytics/leave-trends');
  const attrition = usePanel<unknown[]>('/api/analytics/attrition-risk');
  const expenses = usePanel<unknown>('/api/analytics/expense-summary');
  const performance = usePanel<unknown>('/api/analytics/performance-summary');
  const { pending, run } = useMutation();
  const [error, setError] = useState<string | null>(null);

  // The `useState` above would be out of place; use local helper instead.
  const err = error;
  void err;

  const refresh = useCallback(async () => {
    setError(null);
    try {
      await run(async () => {
        await api.post('/api/analytics/refresh', {});
        return null;
      });
      headcount.reload();
      leaveTrends.reload();
      attrition.reload();
      expenses.reload();
      performance.reload();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not refresh analytics.');
    }
  }, [api, attrition, expenses, headcount, leaveTrends, performance, run]);

  return (
    <div className="page">
      <PageHeader title="Analytics" subtitle="Headcount, leave, attrition, expenses, and performance snapshots." />

      <div className="field__actions" style={{ marginBottom: '12px' }}>
        <button type="button" className="btn" disabled={pending} onClick={() => void refresh()}>
          {pending ? 'Refreshing…' : 'Refresh snapshots'}
        </button>
      </div>
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}

      <Panel
        title="Headcount"
        data={headcount.data}
        error={headcount.error}
        loading={headcount.loading}
        reload={headcount.reload}
      >
        {(data) => <Snapshot data={data} />}
      </Panel>

      <Panel
        title="Leave trends"
        data={leaveTrends.data}
        error={leaveTrends.error}
        loading={leaveTrends.loading}
        reload={leaveTrends.reload}
      >
        {(data) => <Snapshot data={data} />}
      </Panel>

      <Panel
        title="Attrition risk"
        data={attrition.data}
        error={attrition.error}
        loading={attrition.loading}
        reload={attrition.reload}
      >
        {(data) =>
          Array.isArray(data) ? (
            <RecordList
              items={data}
              keyOf={(row) => String((row as { emp_id?: string }).emp_id ?? '')}
              emptyTitle="No attrition risk records."
              render={(row) => {
                const item = row as Record<string, unknown>;
                return (
                  <div className="leave-row">
                    <strong>{String(item.name ?? '')}</strong>
                    <span className="list__meta">
                      {` · ${String(item.department ?? '')} · score ${String(item.risk_score ?? '')}`}
                    </span>
                  </div>
                );
              }}
            />
          ) : (
            <Snapshot data={data} />
          )
        }
      </Panel>

      <Panel
        title="Expense summary"
        data={expenses.data}
        error={expenses.error}
        loading={expenses.loading}
        reload={expenses.reload}
      >
        {(data) => <Snapshot data={data} />}
      </Panel>

      <Panel
        title="Performance summary"
        data={performance.data}
        error={performance.error}
        loading={performance.loading}
        reload={performance.reload}
      >
        {(data) => <Snapshot data={data} />}
      </Panel>
    </div>
  );
}

