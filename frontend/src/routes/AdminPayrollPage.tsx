/**
 * `/admin/payroll` — payroll runs (UI-09).
 */
import { useCallback, useState } from 'react';

import { PageHeader, Panel, RecordList, StatusPill } from '@/components/Panel';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { PayrollItem, PayrollRatesVersion, PayrollRun, PayrollRunJob } from '@/lib/types';

const POLL_ATTEMPTS = 60;
const POLL_DELAY_MS = 500;

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function AdminPayrollPage() {
  const { actor } = useSession();
  const canPayrollWrite =
    actor?.role === 'Finance' || actor?.role === 'Admin' || actor?.role === 'Super Admin';
  const runs = usePanel<PayrollRun[]>('/api/payroll-runs');
  const rates = usePanel<PayrollRatesVersion[]>('/api/payroll-rates');
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

  const waitForJob = useCallback(async (jobId: number): Promise<void> => {
    for (let attempt = 0; attempt < POLL_ATTEMPTS; attempt += 1) {
      const job = await api.get<PayrollRunJob>(`/api/payroll-run-jobs/${jobId}`);
      if (job.status === 'completed') return;
      if (job.status === 'failed') {
        throw new ApiError(0, job.failure_reason ?? 'Payroll job failed');
      }
      await wait(POLL_DELAY_MS);
    }
    throw new ApiError(0, 'Payroll is still running — check the list again shortly.');
  }, []);

  const createRun = useCallback(async () => {
    setError(null);
    setMessage(null);
    try {
      const result = await api.post<{
        message?: string;
        run_id: number;
        job_id: number;
        status: string;
      }>('/api/payroll-runs', {
        month: Number(month),
        year: Number(year),
      });
      setMessage('Payroll run queued — processing…');
      runs.reload();
      await waitForJob(result.job_id);
      setMessage('Payroll run ready for review.');
      runs.reload();
      setSelectedRun(result.run_id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not create the run.');
      runs.reload();
    }
  }, [month, runs, waitForJob, year]);

  const processNow = useCallback(
    async (id: number) => {
      setError(null);
      try {
        await api.post(`/api/payroll-runs/${id}/run`, {});
        setMessage('Payroll run processed.');
        runs.reload();
        if (selectedRun === id) items.reload();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Could not process the run.');
      }
    },
    [items, runs, selectedRun],
  );

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

  const [ratesMessage, setRatesMessage] = useState<string | null>(null);
  const [ratesError, setRatesError] = useState<string | null>(null);
  const [effectiveFrom, setEffectiveFrom] = useState(() => new Date().toISOString().slice(0, 10));
  const [effectiveTo, setEffectiveTo] = useState('');
  const [pfRate, setPfRate] = useState('0.12');
  const [pfMax, setPfMax] = useState('1800');
  const [esiRate, setEsiRate] = useState('0.0075');
  const [esiMax, setEsiMax] = useState('21000');
  const [ptAmount, setPtAmount] = useState('200');
  const [ptThreshold, setPtThreshold] = useState('10000');
  const [note, setNote] = useState('');
  const [slabs, setSlabs] = useState<Array<{ from: string; rate: string }>>([
    { from: '0', rate: '0' },
    { from: '300000', rate: '0.05' },
    { from: '600000', rate: '0.1' },
    { from: '900000', rate: '0.15' },
    { from: '1200000', rate: '0.2' },
    { from: '1500000', rate: '0.3' },
  ]);

  const submitRates = useCallback(async () => {
    setRatesMessage(null);
    setRatesError(null);
    try {
      await api.post('/api/payroll-rates', {
        effective_from: effectiveFrom,
        effective_to: effectiveTo || null,
        pf_rate: Number(pfRate),
        pf_max: Number(pfMax),
        esi_rate: Number(esiRate),
        esi_max: Number(esiMax),
        pt_amount: Number(ptAmount),
        pt_threshold: Number(ptThreshold),
        tds_slabs: slabs.map((slab) => ({
          from_amount: Number(slab.from),
          rate: Number(slab.rate),
        })),
        note: note.trim() || null,
      });
      setRatesMessage('Payroll rates saved as a new version.');
      rates.reload();
    } catch (err) {
      setRatesError(err instanceof ApiError ? err.message : 'Could not save rates.');
    }
  }, [effectiveFrom, effectiveTo, esiMax, esiRate, note, pfMax, pfRate, ptAmount, ptThreshold, rates, slabs]);

  const money = (v: number): string => `₹${Number(v).toFixed(2)}`;

  return (
    <div className="page">
      <PageHeader title="Payroll" subtitle="Create, review, and finalize payroll runs." />

      {message ? <p className="notice notice--ok" role="status">{message}</p> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      {!canPayrollWrite ? (
        <p className="notice notice--ok" role="status">
          HR has read-only access to payroll. Creating and approving runs requires Finance or an administrator.
        </p>
      ) : null}

      {canPayrollWrite ? (
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
      ) : null}

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
                  {canPayrollWrite && run.status === 'Queued' ? (
                    <button
                      type="button"
                      className="btn btn--small btn--primary"
                      disabled={pending}
                      onClick={() => void processNow(run.id)}
                    >
                      Process now
                    </button>
                  ) : null}
                  {canPayrollWrite && run.status === 'Draft' ? (
                    <button
                      type="button"
                      className="btn btn--small"
                      disabled={pending}
                      onClick={() => void transition(run.id, 'submit')}
                    >
                      Submit
                    </button>
                  ) : null}
                  {canPayrollWrite && run.status === 'Submitted' ? (
                    <button
                      type="button"
                      className="btn btn--small"
                      disabled={pending}
                      onClick={() => void transition(run.id, 'approve')}
                    >
                      Approve
                    </button>
                  ) : null}
                  {canPayrollWrite && run.status === 'Approved' ? (
                    <button
                      type="button"
                      className="btn btn--small btn--primary"
                      disabled={pending}
                      onClick={() => void transition(run.id, 'finalize')}
                    >
                      Finalize
                    </button>
                  ) : null}
                  {canPayrollWrite && run.status === 'Finalized' ? (
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
                  <th>TDS</th>
                  <th>LOP</th>
                  <th>Reimb.</th>
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
                    <td>{money(item.tds ?? 0)}</td>
                    <td>{money(item.lop_amount ?? 0)}</td>
                    <td>{money(item.reimbursements ?? 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      ) : null}

      <Panel
        title="Payroll rates"
        data={rates.data ?? []}
        error={rates.error}
        loading={rates.loading}
        reload={rates.reload}
      >
        {(versions) => (
          <table className="table table--compact">
            <thead>
              <tr>
                <th>Effective from</th>
                <th>Effective to</th>
                <th>PF</th>
                <th>ESI</th>
                <th>PT</th>
                <th>TDS slabs</th>
                <th>Note</th>
              </tr>
            </thead>
            <tbody>
              {versions.map((version) => (
                <tr key={version.version_id}>
                  <td>{version.effective_from}</td>
                  <td>{version.effective_to ?? 'open'}</td>
                  <td>{(version.pf_rate * 100).toFixed(1)}% (cap {money(version.pf_max)})</td>
                  <td>{(version.esi_rate * 100).toFixed(2)}% (to {money(version.esi_max)})</td>
                  <td>{money(version.pt_amount)} above {money(version.pt_threshold)}</td>
                  <td>{version.tds_slabs.length}</td>
                  <td>{version.note ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      {canPayrollWrite ? (
        <Panel title="New rate version" data={{ ok: true }} error={null} loading={false} reload={() => {}}>
          {() => (
            <div className="form">
              {ratesMessage ? (
                <p className="notice notice--ok" role="status">{ratesMessage}</p>
              ) : null}
              {ratesError ? (
                <p className="notice notice--bad" role="alert">{ratesError}</p>
              ) : null}
              <div className="form__row">
                <div className="field">
                  <label htmlFor="rates-from">Effective from</label>
                  <input id="rates-from" type="date" value={effectiveFrom} onChange={(e) => setEffectiveFrom(e.target.value)} />
                </div>
                <div className="field">
                  <label htmlFor="rates-to">Effective to</label>
                  <input id="rates-to" type="date" value={effectiveTo} onChange={(e) => setEffectiveTo(e.target.value)} />
                </div>
              </div>
              <div className="form__row">
                <div className="field">
                  <label htmlFor="rates-pf">PF rate</label>
                  <input id="rates-pf" type="number" step="0.001" value={pfRate} onChange={(e) => setPfRate(e.target.value)} />
                </div>
                <div className="field">
                  <label htmlFor="rates-pf-max">PF cap / month</label>
                  <input id="rates-pf-max" type="number" step="1" value={pfMax} onChange={(e) => setPfMax(e.target.value)} />
                </div>
                <div className="field">
                  <label htmlFor="rates-esi">ESI rate</label>
                  <input id="rates-esi" type="number" step="0.0001" value={esiRate} onChange={(e) => setEsiRate(e.target.value)} />
                </div>
                <div className="field">
                  <label htmlFor="rates-esi-max">ESI gross ceiling</label>
                  <input id="rates-esi-max" type="number" step="1" value={esiMax} onChange={(e) => setEsiMax(e.target.value)} />
                </div>
              </div>
              <div className="form__row">
                <div className="field">
                  <label htmlFor="rates-pt">Professional tax</label>
                  <input id="rates-pt" type="number" step="1" value={ptAmount} onChange={(e) => setPtAmount(e.target.value)} />
                </div>
                <div className="field">
                  <label htmlFor="rates-pt-threshold">PT gross threshold</label>
                  <input id="rates-pt-threshold" type="number" step="1" value={ptThreshold} onChange={(e) => setPtThreshold(e.target.value)} />
                </div>
              </div>
              <fieldset className="fieldset">
                <legend>TDS slabs (cumulative; first must start at 0)</legend>
                {slabs.map((slab, index) => (
                  <div className="form__row" key={index}>
                    <div className="field">
                      <label htmlFor={`slab-from-${index}`}>From amount</label>
                      <input id={`slab-from-${index}`} type="number" value={slab.from}
                        onChange={(e) => setSlabs((rows) => rows.map((row, i) => (i === index ? { ...row, from: e.target.value } : row)))} />
                    </div>
                    <div className="field">
                      <label htmlFor={`slab-rate-${index}`}>Rate</label>
                      <input id={`slab-rate-${index}`} type="number" step="0.01" value={slab.rate}
                        onChange={(e) => setSlabs((rows) => rows.map((row, i) => (i === index ? { ...row, rate: e.target.value } : row)))} />
                    </div>
                    <button type="button" className="btn btn--small" aria-label="Remove slab"
                      onClick={() => setSlabs((rows) => rows.filter((_, i) => i !== index))}>
                      Remove
                    </button>
                  </div>
                ))}
                <button type="button" className="btn btn--small"
                  onClick={() => setSlabs((rows) => [...rows, { from: '', rate: '0' }])}>
                  Add slab
                </button>
              </fieldset>
              <div className="field">
                <label htmlFor="rates-note">Note</label>
                <input id="rates-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="What changed and why" />
              </div>
              <button type="button" className="btn btn--primary" disabled={pending}
                onClick={() => void submitRates()}>
                {pending ? 'Saving…' : 'Save new version'}
              </button>
            </div>
          )}
        </Panel>
      ) : null}
    </div>
  );
}