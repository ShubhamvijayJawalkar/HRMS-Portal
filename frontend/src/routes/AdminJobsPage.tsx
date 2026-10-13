/**
 * `/admin/jobs` — post and close job postings (UI-09).
 *
 * Reaches parity with `templates/jobs.html`: title, department, location,
 * description, plus a close action for open postings.
 */
import { useCallback, useState } from 'react';

import { PageHeader, Panel, RecordList, StatusPill } from '@/components/Panel';
import { ValidationErrors } from '@/components/states';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { JobRecord } from '@/lib/types';

interface JobForm {
  title: string;
  department: string;
  location: string;
  description: string;
}

export function AdminJobsPage() {
  const { actor } = useSession();
  const jobs = usePanel<JobRecord[]>('/api/jobs');
  const { run, pending } = useMutation();

  const [form, setForm] = useState<JobForm>({
    title: '',
    department: '',
    location: '',
    description: '',
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const create = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setNotice(null);
      setErrors({});
      if (!form.title.trim()) {
        setErrors({ title: 'Title is required.' });
        return;
      }
      const result = await run(async () => {
        try {
          const created = await api.post<{ id?: number; message?: string }>('/api/jobs', {
            title: form.title.trim(),
            department: form.department.trim(),
            location: form.location.trim(),
            description: form.description.trim(),
          });
          return { ok: true as const, message: created?.message ?? 'Job posted.' };
        } catch (err) {
          const message = err instanceof ApiError ? err.message : 'Could not post the job.';
          return { ok: false as const, message };
        }
      });
      if (!result) return;
      if (result.ok) {
        setNotice(result.message);
        setForm({ title: '', department: '', location: '', description: '' });
        jobs.reload();
      } else {
        setErrors({ _: result.message });
      }
    },
    [form, jobs, run],
  );

  const closeJob = useCallback(
    async (id: number) => {
      if (!window.confirm('Close this job posting?')) return;
      try {
        await api.post(`/api/jobs/${id}/close`);
        jobs.reload();
      } catch (err) {
        setErrors({
          _: err instanceof ApiError ? err.message : 'Could not close the job.',
        });
      }
    },
    [jobs],
  );

  const rows = jobs.data ?? [];

  return (
    <div className="page">
      <PageHeader
        title="Jobs"
        subtitle="Post and close job openings."
      />

      {notice ? (
        <p className="notice notice--ok" role="status">
          {notice}
        </p>
      ) : null}

      <Panel
        title="New job"
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
                <label htmlFor="job-title">Title</label>
                <input
                  id="job-title"
                  value={form.title}
                  onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="job-department">Department</label>
                <input
                  id="job-department"
                  value={form.department}
                  onChange={(e) => setForm((f) => ({ ...f, department: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="job-location">Location</label>
                <input
                  id="job-location"
                  value={form.location}
                  onChange={(e) => setForm((f) => ({ ...f, location: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="job-description">Description</label>
                <input
                  id="job-description"
                  value={form.description}
                  onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
                />
              </div>
              <button type="submit" className="btn" disabled={pending}>
                {pending ? 'Posting…' : 'Post'}
              </button>
            </div>
          </form>
        )}
      </Panel>

      <Panel
        title="All jobs"
        data={rows}
        error={jobs.error}
        loading={jobs.loading}
        reload={jobs.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(job) => job.id}
            emptyTitle="No jobs."
            emptyHint="Post a job above and it will appear here."
            render={(job) => (
              <div className="leave-row">
                <div>
                  <StatusPill status={job.status} />{' '}
                  <strong>{job.title}</strong>
                  <span className="list__meta">
                    {job.department ? ` · ${job.department}` : ''}
                    {job.location ? ` · ${job.location}` : ''}
                    {job.created_at
                      ? ` · ${new Date(job.created_at).toLocaleDateString()}`
                      : ''}
                  </span>
                </div>
                {job.status === 'Open' ? (
                  <button
                    type="button"
                    className="btn btn--small btn--danger"
                    onClick={() => void closeJob(job.id)}
                  >
                    Close
                  </button>
                ) : null}
              </div>
            )}
          />
        )}
      </Panel>

      {actor?.emp_id ? null : null}
    </div>
  );
}
