/**
 * `/admin/payroll` — payroll runs (UI-09).
 */
import { useCallback, useState } from 'react';

import { PageHeader, Panel, RecordList, StatusPill } from '@/components/Panel';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import type { PayrollItem, PayrollRun } from '@/lib/types';

export function AdminPayrollPage() {
  const runs = usePanel<PayrollRun[]>('/api/payroll-runs');
  const [selectedRun, setSelectedRun] = useState<number | null>(null);
  const items = usePanel<PayrollItem[]>(
    selectedRun ? `/api/payroll-runs/${selectedRun}/items` : '',
    { enabled: selectedRun !== null },
  );
  const { pending } = useMutation();
  const [month, setMonth] = useState(() => String(new Date().getMonth() + 1));
  const [year, setYear] = useState(() => String(new Date().getFullYear()));
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const createRun = useCallback(async () => {
    setError(null);
    try {
      const result = await api.post<{ id?: number } | { message?: string; job_id?: number }>('/api/payroll-runs', {
        month: Number(month),
        year: Number(year),
      });
      setMessage('Payroll run created.');
      runs.reload();
      const newId = (result as { id?: number }).id;
      if (newId) setSelectedRun(newId);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not create the run.');
    }
  }, [month, runs, year]);

  const transition = useCallback(
    async (id: number, path: 'submit' | 'approve' | 'finalize') => {
      setError(null);
      try {
        await api.post(`/api/payroll-runs/${id}/${path}`, {});
        setMessage(`Run ${path}ed.`);
        runs.reload();
        if (selectedRun === id) items.reload();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : `Could not ${path} the run.`);
      }
    },
    [items, runs, selectedRun],
  );

  const money = (v: number): string => `₹${Number(v).toFixed(2)}`;

  return (
    <div className="page">
      <PageHeader title="Payroll" subtitle="Create, review, and finalize payroll runs." />

      {message ? <p className="notice notice--ok" role="status">{message}</p> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}

      <Panel
        title="Create run"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <div className="form">
            <div className="form__row">
              <div className="field">
                <label htmlFor="p-month">Month</label>
                <input
                  id="p-month"
                  value={month}
                  onChange={(e) => setMonth(e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="p-year">Year</label>
                <input
                  id="p-year"
                  value={year}
                  onChange={(e) => setYear(e.target.value)}
                />
              </div>
              <button type="button" className="btn" disabled={pending} onClick={() => void createRun()}>
                {pending ? 'Creating…' : 'Create'}
              </button>
            </div>
          </div>
        )}
      </Panel>

      <Panel
        title="Runs"
        data={runs.data ?? []}
        error={runs.error}
        loading={runs.loading}
        reload={runs.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(run) => run.id}
            emptyTitle="No payroll runs."
            emptyHint="Create a run above."
            render={(run) => (
              <div className="leave-row">
                <div>
                  <StatusPill status={run.status} />{' '}
                  <strong>{run.month}/{run.year}</strong>
                  <span className="list__meta">
                    {run.submitted_by ? ` · submitted by ${run.submitted_by}` : ''}
                    {run.approved_by ? ` · approved by ${run.approved_by}` : ''}
                  </span>
                  {selectedRun === run.id ? (
                    <span className="list__meta"> (selected)</span>
                  ) : null}
                </div>
                <div className="field__actions">
                  <button
                    type="button"
                    className="btn btn--small"
                    onClick={() => setSelectedRun(run.id)}
                  >
                    View items
                  </button>
                  {run.status === 'Draft' ? (
                    <button
                      type="button"
                      className="btn btn--small"
                      disabled={pending}
                      onClick={() => void transition(run.id, 'submit')}
                    >
                      Submit
                    </button>
                  ) : null}
                  {run.status === 'Submitted' ? (
                    <button
                      type="button"
                      className="btn btn--small"
                      disabled={pending}
                      onClick={() => void transition(run.id, 'approve')}
                    >
                      Approve
                    </button>
                  ) : null}
                  {run.status === 'Approved' ? (
                    <button
                      type="button"
                      className="btn btn--small btn--primary"
                      disabled={pending}
                      onClick={() => void transition(run.id, 'finalize')}
                    >
                      Finalize
                    </button>
                  ) : null}
                  {run.status === 'Finalized' ? (
                    <>
                      <a className="btn btn--small" href={`/api/payroll-runs/${run.id}/bank-file`} target="_blank">
                        Bank file
                      </a>
                      <a className="btn btn--small" href={`/api/payroll-runs/${run.id}/tds-report`} target="_blank">
                        TDS report
                      </a>
                    </>
                  ) : null}
                </div>
              </div>
            )}
          />
        )}
      </Panel>

      {selectedRun ? (
        <Panel
          title={`Payroll items for run ${selectedRun}`}
          data={items.data ?? []}
          error={items.error}
          loading={items.loading}
          reload={items.reload}
        >
          {(values) => (
            <table className="table table--compact">
              <thead>
                <tr>
                  <th>Employee</th>
                  <th>Gross</th>
                  <th>Deductions</th>
                  <th>Net</th>
                  <th>PF</th>
                  <th>ESI</th>
                  <th>PT</th>
                </tr>
              </thead>
              <tbody>
                {values.map((item) => (
                  <tr key={item.id}>
                    <td>{item.employee || item.emp_id}</td>
                    <td>{money(item.gross)}</td>
                    <td>{money(item.deductions)}</td>
                    <td>{money(item.net)}</td>
                    <td>{money(item.pf)}</td>
                    <td>{money(item.esi)}</td>
                    <td>{money(item.pt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      ) : null}
    </div>
  );
}