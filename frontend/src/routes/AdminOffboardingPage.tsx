/**
 * `/admin/offboarding` — offboarding task tracking (UI-09).
 */
import { useState } from 'react';

import { PageHeader, Panel, RecordList, StatusPill } from '@/components/Panel';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import type { OffboardingTaskRecord } from '@/lib/types';

export function AdminOffboardingPage() {
  const tasks = usePanel<OffboardingTaskRecord[]>('/api/offboarding-tasks');
  const { pending } = useMutation();
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function completeTask(id: number) {
    setError(null);
    try {
      await api.post(`/api/offboarding-tasks/${id}/complete`);
      setNotice('Task completed.');
      tasks.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not complete the task.');
    }
  }

  return (
    <div className="page">
      <PageHeader title="Offboarding" subtitle="Track resignation/offboarding stage tasks." />

      {notice ? <p className="notice notice--ok" role="status">{notice}</p> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}

      <Panel
        title="Offboarding tasks"
        data={tasks.data ?? []}
        error={tasks.error}
        loading={tasks.loading}
        reload={tasks.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(task) => task.id}
            emptyTitle="No offboarding tasks."
            emptyHint="Resignations trigger these tasks. "
            render={(task) => (
              <div className="leave-row">
                <div>
                  <StatusPill status={task.status} />{' '}
                  <strong>{task.employee}</strong>
                  <span className="list__meta">
                    {` · ${task.task}`}
                    {task.assigned_to ? ` · ${task.assigned_to}` : ''}
                    {task.due_date ? ` · due ${task.due_date}` : ''}
                    {` · stage ${task.stage ?? '-'}`}
                  </span>
                </div>
                {task.status !== 'Completed' ? (
                  <div className="field__actions">
                    <button
                      type="button"
                      className="btn btn--small"
                      disabled={pending}
                      onClick={() => void completeTask(task.id)}
                    >
                      Complete
                    </button>
                  </div>
                ) : null}
              </div>
            )}
          />
        )}
      </Panel>
    </div>
  );
}