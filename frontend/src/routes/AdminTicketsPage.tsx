/**
 * `/admin/tickets` — triage the company ticket queue (UI-09).
 *
 * Reaches parity with the admin aspect of `templates/admin_tickets.html`: list
 * everything, open a ticket to read it, add a comment, and move it along the
 * FR-TKT-04 chain. The allowed transitions come from the server's response,
 * not from a client-side copy of the state machine.
 */
import { useCallback, useState } from 'react';

import { PageHeader, Panel, RecordList, StatusPill } from '@/components/Panel';
import { ValidationErrors } from '@/components/states';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { Ticket } from '@/lib/types';

interface TicketComment {
  id: number;
  emp_id: string;
  name: string;
  comment: string;
  created_at?: string | null;
}

interface TicketDetail extends Ticket {
  employee?: string;
  comments?: TicketComment[];
  allowed?: string[];
}

interface CommentResult {
  message?: string;
  id?: number;
  status?: string;
  reopened?: boolean;
}

export function AdminTicketsPage() {
  const { actor } = useSession();
  const tickets = usePanel<Ticket[]>('/api/tickets');
  const { run, pending } = useMutation();

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<TicketDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [comment, setComment] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});


  const openTicket = useCallback(async (id: number) => {
    setSelectedId(id);
    setDetail(null);
    setDetailError(null);
    setDetailLoading(true);
    try {
      const data = await api.get<TicketDetail>(`/api/tickets/${id}`);
      setDetail(data);
    } catch (err) {
      setDetailError(err instanceof ApiError ? err.message : 'Could not load the ticket.');
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const addComment = useCallback(async () => {
    if (!selectedId || !comment.trim()) return;
    setErrors({});
    const result = await run(async () => {
      try {
        const created = await api.post<CommentResult>(`/api/tickets/${selectedId}/comment`, {
          comment: comment.trim(),
        });
        return { ok: true as const, created };
      } catch (err) {
        const message = err instanceof ApiError ? err.message : 'Could not add the comment.';
        return { ok: false as const, message };
      }
    });
    if (!result) return;
    if (result.ok) {
      setComment('');
      await openTicket(selectedId);
      tickets.reload();
    } else {
      setErrors({ _: result.message });
    }
  }, [comment, openTicket, run, selectedId, tickets]);

  const changeStatus = useCallback(
    async (target: string) => {
      if (!selectedId || !detail) return;
      setErrors({});
      if (target === detail.status) return;
      try {
        await run(async () => {
          await api.put(`/api/tickets/${selectedId}/status`, { status: target });
          return null;
        });
      } catch (err) {
        setErrors({
          _: err instanceof ApiError ? err.message : 'Could not change the status.',
        });
      } finally {
        await openTicket(selectedId);
        tickets.reload();
      }
    },
    [detail, openTicket, run, selectedId, tickets],
  );

  const rows = tickets.data ?? [];

  return (
    <div className="page">
      <PageHeader
        title="Tickets"
        subtitle="Triage the company support queue."
      />

      <Panel
        title="All tickets"
        data={rows}
        error={tickets.error}
        loading={tickets.loading}
        reload={tickets.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(ticket) => ticket.id}
            emptyTitle="No tickets."
            emptyHint="Nothing has been raised yet."
            render={(ticket) => (
              <div className="leave-row">
                <div>
                  <StatusPill status={ticket.status} />{' '}
                  <strong>{ticket.subject}</strong>
                  <span className="list__meta">
                    {' '}
                    · {ticket.employee || ticket.emp_id}
                    {ticket.category ? ` · ${ticket.category}` : ''}
                    {ticket.priority ? ` · ${ticket.priority}` : ''}
                    {ticket.assigned_to ? ` · assigned to ${ticket.assigned_to}` : ''}
                    {ticket.created_at
                      ? ` · ${new Date(ticket.created_at).toLocaleDateString()}`
                      : ''}
                  </span>
                </div>
                <button
                  type="button"
                  className="btn btn--small"
                  onClick={() => void openTicket(ticket.id)}
                >
                  View
                </button>
              </div>
            )}
          />
        )}
      </Panel>

      {selectedId ? (
        <Panel
          title={`Ticket #${selectedId}`}
          data={detail}
          error={detailError ? new ApiError(0, detailError) : null}
          loading={detailLoading}
          reload={() => void openTicket(selectedId)}
        >
          {(item) => (
            <div className="panel__detail">
              <h3>{item.subject}</h3>
              <p className="list__meta">
                {item.employee || item.emp_id} · {item.category || 'General'} · {item.priority || 'Medium'}
              </p>
              <p>{item.description || 'No description.'}</p>
              <div className="form__row">
                <div className="field">
                  <label htmlFor="ticket-status">Status</label>
                  <select
                    id="ticket-status"
                    value={item.status}
                    onChange={(e) => void changeStatus(e.target.value)}
                    disabled={pending}
                  >
                    {['Open', 'In Progress', 'Resolved', 'Closed', 'Reopened'].map((status) => (
                      <option key={status} value={status}>
                        {status}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div className="comment-list">
                {(item.comments ?? []).map((entry) => (
                  <div key={entry.id} className="comment">
                    <strong>{entry.name}</strong>{' '}
                    <small>
                      {entry.created_at ? new Date(entry.created_at).toLocaleString() : ''}
                    </small>
                    <div>{entry.comment}</div>
                  </div>
                ))}
              </div>
              <div className="form__row">
                <div className="field">
                  <label htmlFor="ticket-comment">Add comment</label>
                  <input
                    id="ticket-comment"
                    value={comment}
                    onChange={(e) => setComment(e.target.value)}
                  />
                </div>
                <button
                  type="button"
                  className="btn"
                  onClick={() => void addComment()}
                  disabled={pending || !comment.trim()}
                >
                  {pending ? 'Saving…' : 'Comment'}
                </button>
              </div>
              <ValidationErrors errors={errors} />
            </div>
          )}
        </Panel>
      ) : null}

      {actor?.emp_id ? null : null}
    </div>
  );
}
