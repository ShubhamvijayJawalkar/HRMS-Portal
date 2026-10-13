/**
 * `/admin/candidates` — candidate tracking and status progression (UI-09).
 *
 * Reaches parity with the candidate management aspect of `templates/candidates.html`:
 * add candidates, advance status, and create offers for interviewed candidates.
 */
import { useCallback, useState } from 'react';

import { PageHeader, Panel, RecordList } from '@/components/Panel';
import { ValidationErrors } from '@/components/states';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { CandidateRecord, JobRecord } from '@/lib/types';

const STATUSES = ['Applied', 'Screened', 'Interviewed', 'Offered', 'Hired', 'Rejected', 'Withdrawn'];

export function AdminCandidatesPage() {
  const { actor } = useSession();
  const candidates = usePanel<CandidateRecord[]>('/api/candidates');
  const jobs = usePanel<JobRecord[]>('/api/jobs');
  const { run, pending } = useMutation();

  const [form, setForm] = useState({ name: '', email: '', phone: '', job_id: '' });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const jobOptions = (jobs.data ?? []).filter((job) => job.status === 'Open' || !job.status);

  const createCandidate = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setNotice(null);
      setErrors({});
      if (!form.name.trim() || !form.email.trim()) {
        setErrors({ _: 'Name and email are required.' });
        return;
      }
      const result = await run(async () => {
        try {
          const created = await api.post<{ id?: number; message?: string }>('/api/candidates', {
            name: form.name.trim(),
            email: form.email.trim(),
            phone: form.phone.trim(),
            job_id: form.job_id ? Number(form.job_id) : null,
          });
          return { ok: true as const, message: created?.message ?? 'Candidate added.' };
        } catch (err) {
          const message = err instanceof ApiError ? err.message : 'Could not add the candidate.';
          return { ok: false as const, message };
        }
      });
      if (!result) return;
      if (result.ok) {
        setNotice(result.message);
        setForm({ name: '', email: '', phone: '', job_id: '' });
        candidates.reload();
      } else {
        setErrors({ _: result.message });
      }
    },
    [candidates, form, run],
  );

  const updateStatus = useCallback(
    async (id: number, status: string) => {
      try {
        await api.put(`/api/candidates/${id}/status`, { status });
        candidates.reload();
      } catch (err) {
        setErrors({
          _: err instanceof ApiError ? err.message : 'Could not update the status.',
        });
      }
    },
    [candidates],
  );

  const createOffer = useCallback(
    async (candidateId: number) => {
      const salaryText = window.prompt('Annual offered salary');
      if (!salaryText) return;
      const basicText = window.prompt('Basic percentage', '50');
      const hraText = window.prompt('HRA percentage', '30');
      const allowancesText = window.prompt('Allowances percentage', '20');
      if ([basicText, hraText, allowancesText].some((value) => value === null)) return;
      try {
        await api.post('/api/offers', {
          candidate_id: candidateId,
          offered_salary: Number(salaryText),
          basic_pct: Number(basicText),
          hra_pct: Number(hraText),
          allowances_pct: Number(allowancesText),
        });
        candidates.reload();
      } catch (err) {
        setErrors({
          _: err instanceof ApiError ? err.message : 'Could not create the offer.',
        });
      }
    },
    [candidates],
  );

  const rows = candidates.data ?? [];

  return (
    <div className="page">
      <PageHeader title="Candidates" subtitle="Track applicants and progress hiring." />

      {notice ? (
        <p className="notice notice--ok" role="status">
          {notice}
        </p>
      ) : null}

      <Panel
        title="Add candidate"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <form className="form" onSubmit={(event) => void createCandidate(event)} noValidate>
            <ValidationErrors errors={errors} />
            <div className="form__row">
              <div className="field">
                <label htmlFor="candidate-name">Name</label>
                <input
                  id="candidate-name"
                  value={form.name}
                  onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="candidate-email">Email</label>
                <input
                  id="candidate-email"
                  type="email"
                  value={form.email}
                  onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="candidate-phone">Phone</label>
                <input
                  id="candidate-phone"
                  value={form.phone}
                  onChange={(e) => setForm((f) => ({ ...f, phone: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="candidate-job">Job</label>
                <select
                  id="candidate-job"
                  value={form.job_id}
                  onChange={(e) => setForm((f) => ({ ...f, job_id: e.target.value }))}
                >
                  <option value="">-- None --</option>
                  {jobOptions.map((job) => (
                    <option key={job.id} value={job.id}>
                      {job.title}
                    </option>
                  ))}
                </select>
              </div>
              <button type="submit" className="btn" disabled={pending || jobs.loading}>
                {pending ? 'Adding…' : 'Add'}
              </button>
            </div>
          </form>
        )}
      </Panel>

      <Panel
        title="Candidates"
        data={rows}
        error={candidates.error}
        loading={candidates.loading}
        reload={candidates.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(candidate) => candidate.id}
            emptyTitle="No candidates."
            emptyHint="Add a candidate above and they will appear here."
            render={(candidate) => (
              <div className="leave-row">
                <div>
                  <strong>{candidate.name}</strong>
                  <span className="list__meta">
                    {' '}
                    · {candidate.email}
                    {candidate.phone ? ` · ${candidate.phone}` : ''}
                    {candidate.job_title ? ` · ${candidate.job_title}` : ''}
                    {candidate.applied_at
                      ? ` · ${new Date(candidate.applied_at).toLocaleDateString()}`
                      : ''}
                  </span>
                </div>
                <div className="field__actions">
                  <select
                    value={candidate.status}
                    onChange={(e) => void updateStatus(candidate.id, e.target.value)}
                  >
                    {STATUSES.map((status) => (
                      <option key={status} value={status}>
                        {status}
                      </option>
                    ))}
                  </select>
                  {candidate.status === 'Interviewed' ? (
                    <button
                      type="button"
                      className="btn btn--small"
                      onClick={() => void createOffer(candidate.id)}
                    >
                      Create offer
                    </button>
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
