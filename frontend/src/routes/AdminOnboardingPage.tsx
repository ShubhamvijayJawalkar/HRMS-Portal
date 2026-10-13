/**
 * `/admin/onboarding` — onboarding workflow/task tracking (UI-09).
 */
import { useCallback, useEffect, useState } from 'react';

import { PageHeader, Panel, RecordList, StatusPill } from '@/components/Panel';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import type { OnboardingTaskRecord, OnboardingWorkflow } from '@/lib/types';

export function AdminOnboardingPage() {
  const workflows = usePanel<OnboardingWorkflow[]>('/api/onboarding-workflows');
  const tasks = usePanel<OnboardingTaskRecord[]>('/api/onboarding-tasks');
  const { pending } = useMutation();
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    document.title = 'Onboarding';
  }, []);

  const completeTask = useCallback(
    async (id: number) => {
      setError(null);
      try {
        await api.post(`/api/onboarding-tasks/${id}/complete`);
        setNotice('Task completed.');
        tasks.reload();
        workflows.reload();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Could not complete the task.');
      }
    },
    [tasks, workflows],
  );

  const flowRows = workflows.data ?? [];

  return (
    <div className="page">
      <PageHeader title="Onboarding" subtitle="Track pre-hire onboarding workflows and tasks." />

      {notice ? (
        <p className="notice notice--ok" role="status">
          {notice}
        </p>
      ) : null}
      {error ? (
        <p className="notice notice--bad" role="alert">
          {error}
        </p>
      ) : null}

      <Panel
        title="Workflows"
        data={flowRows}
        error={workflows.error}
        loading={workflows.loading}
        reload={workflows.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(flow) => flow.workflow_id}
            emptyTitle="No onboarding workflows."
            emptyHint="Workflows appear when an offer is accepted or a candidate is hired."
            render={(flow) => (
              <article className="leave-row">
                <div>
                  <strong>{flow.employee}</strong>
                  <StatusPill status={flow.completed ? 'Completed' : 'Active'} />
                  <span className="list__meta">
                    {` · step ${flow.current_step ?? 1} · ${flow.days_in_step ?? 0} day(s) in step`}
                    {flow.user_status ? ` · user status: ${flow.user_status}` : ''}
                    {flow.email ? ` · ${flow.email}` : ''}
                  </span>
                  {(flow.tasks ?? []).length ? (
                    <ul className="list__meta" style={{ marginTop: '4px' }}>
                      {flow.tasks!.map((task) => (
                        <li key={task.id}>
                          {task.task} <StatusPill status={task.status} />
                          {task.assigned_to ? ` · ${task.assigned_to}` : ''}
                          {task.due_date ? ` · due ${task.due_date}` : ''}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {(flow.checklist ?? []).length ? (
                    <ul className="list__meta" style={{ marginTop: '4px' }}>
                      {flow.checklist!.map((item) => (
                        <li key={item.id}>
                          {item.doc_type} <StatusPill status={item.status} />
                          {item.review_note ? ` · ${item.review_note}` : ''}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </div>
                <div className="field__actions">
                  {(tasks.data ?? [])
                    .filter((task) => task.emp_id === flow.emp_id && task.status !== 'Completed')
                    .map((task) => (
                      <button
                        key={task.id}
                        type="button"
                        className="btn btn--small"
                        disabled={pending}
                        onClick={() => void completeTask(task.id)}
                      >
                        Complete: {task.task}
                      </button>
                    ))}
                </div>
              </article>
            )}
          />
        )}
      </Panel>

      <Panel
        title="All onboarding tasks"
        data={tasks.data ?? []}
        error={tasks.error}
        loading={tasks.loading}
        reload={tasks.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(task) => task.id}
            emptyTitle="No onboarding tasks."
            emptyHint="Assigned onboarding tasks will appear here."
            render={(task) => (
              <div className="leave-row">
                <div>
                  <StatusPill status={task.status} />{' '}
                  <strong>{task.employee}</strong>
                  <span className="list__meta">
                    {` · ${task.task}`}
                    {task.assigned_to ? ` · ${task.assigned_to}` : ''}
                    {task.due_date ? ` · due ${task.due_date}` : ''}
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