/**
 * `/admin/import-users` — upload a CSV and track the async import job.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { PageHeader, Panel, RecordList, StatusPill } from '@/components/Panel';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { ImportJob } from '@/lib/types';

interface JobHistoryPayload {
  jobs: ImportJob[];
}

export function AdminImportUsersPage() {
  const { actor } = useSession();
  const history = usePanel<JobHistoryPayload>('/api/users/import?limit=10');
  const { pending } = useMutation();

  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [jobId, setJobId] = useState<number | null>(null);
  const [job, setJob] = useState<ImportJob | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, []);

  const stopPolling = () => {
    if (pollRef.current) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const pollJob = useCallback(async (id: number) => {
    try {
      const data = await api.get<ImportJob>(`/api/imports/${id}`);
      setJob(data);
      if (data.status === 'completed' || data.status === 'failed' || data.status === 'cancelled') {
        stopPolling();
        history.reload();
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not read the import job.');
    }
  }, [history]);

  const upload = useCallback(async () => {
    if (!selectedFile) {
      setError('Select a CSV file first.');
      return;
    }
    setError(null);
    setMessage('Uploading…');
    try {
      const form = new FormData();
      form.append('file', selectedFile);
      const result = await api.upload<{ job_id: number; total_rows: number }>(
        '/api/users/import',
        form,
      );
      setJobId(result.job_id);
      setMessage(`Import queued: ${result.total_rows} rows`);
      await pollJob(result.job_id);
      stopPolling();
      pollRef.current = window.setInterval(() => void pollJob(result.job_id), 1500);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Upload failed.');
      setMessage(null);
    }
  }, [pollJob, selectedFile]);

  const runJob = useCallback(async () => {
    if (!jobId) return;
    try {
      await api.post(`/api/users/import/${jobId}/run`);
      await pollJob(jobId);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not process the import.');
    }
  }, [jobId, pollJob]);

  const cancel = useCallback(async (id: number) => {
    try {
      await api.post(`/api/users/import/${id}/cancel`);
      await pollJob(id);
      history.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not cancel the import.');
    }
  }, [history, pollJob]);

  const jobs = history.data?.jobs ?? [];

  return (
    <div className="page">
      <PageHeader title="Import users" subtitle="Upload a CSV and track the async import job." />

      {message ? (
        <p className="notice notice--ok" role="status">
          {message}
        </p>
      ) : null}
      {error ? (
        <p className="notice notice--bad" role="alert">
          {error}
        </p>
      ) : null}

      {job ? (
        <Panel
          title={`Import job ${job.job_id}`}
          data={job}
          error={null}
          loading={false}
          reload={() => void (jobId && pollJob(jobId))}
        >
          {(item) => {
            const total = item.total_rows || 0;
            const processed = item.processed_rows || 0;
            const pct = total ? Math.round((processed / total) * 100) : 0;
            return (
              <div>
                <p>
                  Status: <strong>{item.status}</strong> · {processed} / {total}
                </p>
                <div
                  style={{
                    height: '8px',
                    background: '#E8ECF1',
                    borderRadius: '4px',
                    overflow: 'hidden',
                    marginBottom: '8px',
                  }}
                >
                  <div
                    style={{
                      width: `${pct}%`,
                      height: '100%',
                      background: '#0F172A',
                    }}
                  />
                </div>
                {item.status === 'pending' ? (
                  <button type="button" className="btn" onClick={() => void runJob()}>
                    Process now
                  </button>
                ) : null}
                {item.imported != null ? (
                  <p>
                    {item.imported} imported, {item.skipped ?? 0} skipped.
                  </p>
                ) : null}
                {item.errors?.length ? (
                  <pre style={{ whiteSpace: 'pre-wrap' }}>{item.errors.join('\n')}</pre>
                ) : null}
                {item.failure_reason ? (
                  <p className="notice notice--bad" role="alert">
                    {item.failure_reason}
                  </p>
                ) : null}
              </div>
            );
          }}
        </Panel>
      ) : null}

      <Panel
        title="Upload CSV"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <div className="form">
            <input
              type="file"
              accept=".csv"
              onChange={(e) => setSelectedFile(e.target.files?.[0] ?? null)}
            />
            <button
              type="button"
              className="btn"
              disabled={pending || !selectedFile}
              onClick={() => void upload()}
            >
              {pending ? 'Uploading…' : 'Upload'}
            </button>
            <input type="file" id="fileInput" style={{ display: 'none' }} accept=".csv" />
          </div>
        )}
      </Panel>

      <Panel
        title="Recent imports"
        data={jobs}
        error={history.error}
        loading={history.loading}
        reload={history.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(job) => job.job_id}
            emptyTitle="No imports yet."
            emptyHint="Upload a CSV above."
            render={(job) => (
              <div className="leave-row">
                <div>
                  <strong>Job {job.job_id}</strong>
                  <span className="list__meta">
                    {job.filename ? ` · ${job.filename}` : ''}
                    {' · '}
                    {job.processed_rows ?? 0} / {job.total_rows ?? 0}
                    {job.imported != null ? ` · ${job.imported} imported, ${job.skipped ?? 0} skipped` : ''}
                  </span>
                  <StatusPill status={job.status} />
                </div>
                {job.status === 'pending' ? (
                  <button
                    type="button"
                    className="btn btn--small btn--danger"
                    onClick={() => void cancel(job.job_id)}
                  >
                    Cancel
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
