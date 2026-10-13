/**
 * `/tickets` — raise and track support tickets (UI-08).
 *
 * Reaches parity with `templates/tickets.html`. `GET /api/tickets` is
 * policy-scoped (own + assigned + department), so the list is labelled from the
 * payload exactly as the leave list is — see `LeavePanel` for why that heading
 * cannot be hard-coded.
 *
 * The list is **not** filtered client-side. `tickets.can_view` is the single rule
 * for the list, the detail view, commenting and status changes, and a client that
 * filtered on `emp_id === me` would hide an IT officer's own queue.
 */
import { useCallback, useState } from 'react';

import { PageHeader, Panel, RecordList, RefreshButton, StatusPill } from '@/components/Panel';
import { ValidationErrors } from '@/components/states';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { Ticket } from '@/lib/types';

const PRIORITIES = ['Low', 'Medium', 'High', 'Critical'];

export function TicketsPage() {
  const { actor } = useSession();
  const tickets = usePanel<Ticket[]>('/api/tickets');
  const { run, pending } = useMutation();

  const [form, setForm] = useState({ subject: '', description: '', category: 'IT', priority: 'Medium' });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const viewer = actor?.emp_id ?? '';
  const rows = tickets.data ?? [];
  const showOwner = rows.some((t) => t.emp_id !== viewer);

  const create = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setNotice(null);
      setErrors({});

      if (!form.subject.trim()) {
        setErrors({ subject: 'A subject is required.' });
        return;
      }

      const result = await run(async () => {
        try {
          const created = await api.post<{ id: number; message?: string }>('/api/tickets', {
            subject: form.subject.trim(),
            description: form.description.trim(),
            category: form.category,
            priority: form.priority,
          });
          return { ok: true as const, message: created.message };
        } catch (err) {
          return {
            ok: false as const,
            errors:
              err instanceof ApiError && err.payload && typeof err.payload === 'object'
                ? (err.payload as Record<string, string>)
                : { _: err instanceof ApiError ? err.message : 'Could not raise the ticket.' },
          };
        }
      });

      if (!result) return;
      if (result.ok) {
        setForm({ subject: '', description: '', category: 'IT', priority: 'Medium' });
        setNotice(result.message ?? 'Ticket created.');
        tickets.reload();
      } else {
        setErrors(result.errors);
      }
    },
    [form, run, tickets],
  );

  return (
    <div className="page">
      <PageHeader
        title="Tickets"
        subtitle="Raise a support request and track its progress."
        actions={<RefreshButton onClick={tickets.reload} pending={pending} />}
      />

      {notice ? (
        <p className="notice notice--ok" role="status">
          {notice}
        </p>
      ) : null}

      <Panel
        title="Raise a ticket"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <form className="form" onSubmit={(event) => void create(event)} noValidate>
            <ValidationErrors errors={errors} />
            <div className="field">
              <label htmlFor="t-subject">Subject</label>
              <input
                id="t-subject"
                value={form.subject}
                aria-invalid={Boolean(errors.subject) || undefined}
                aria-describedby={errors.subject ? 't-subject-error' : undefined}
                onChange={(e) => setForm((f) => ({ ...f, subject: e.target.value }))}
              />
              {errors.subject ? (
                <p className="field__error" id="t-subject-error">
                  {errors.subject}
                </p>
              ) : null}
            </div>
            <div className="form__row">
              <div className="field">
                <label htmlFor="t-category">Category</label>
                <select
                  id="t-category"
                  value={form.category}
                  onChange={(e) => setForm((f) => ({ ...f, category: e.target.value }))}
                >
                  {['IT', 'HR', 'Facilities', 'Access', 'Other'].map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="t-priority">Priority</label>
                <select
                  id="t-priority"
                  value={form.priority}
                  onChange={(e) => setForm((f) => ({ ...f, priority: e.target.value }))}
                >
                  {PRIORITIES.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <div className="field">
              <label htmlFor="t-description">Description</label>
              <textarea
                id="t-description"
                rows={3}
                value={form.description}
                onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
              />
            </div>
            <button type="submit" className="btn" disabled={pending}>
              {pending ? 'Raising…' : 'Raise ticket'}
            </button>
          </form>
        )}
      </Panel>

      <Panel
        title={showOwner ? 'Tickets' : 'Your tickets'}
        data={rows}
        error={tickets.error}
        loading={tickets.loading}
        reload={tickets.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(t) => t.id}
            emptyTitle="No tickets."
            emptyHint="Anything you raise will appear here with its current status."
            render={(ticket) => (
              <div className="leave-row">
                <div>
                  <StatusPill status={ticket.status} />{' '}
                  <strong>{ticket.subject}</strong>
                  {showOwner && ticket.emp_id !== viewer ? (
                    <span className="list__meta list__meta--owner">
                      {ticket.employee || ticket.emp_id}
                    </span>
                  ) : null}
                  <span className="list__meta">
                    {ticket.category || 'General'} · {ticket.priority || 'Medium'}
                    {ticket.assigned_to ? ` · assigned to ${ticket.assigned_to}` : ''}
                  </span>
                </div>
              </div>
            )}
          />
        )}
      </Panel>
    </div>
  );
}