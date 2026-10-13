/**
 * `/admin/holidays` — holiday calendar maintenance (UI-09).
 */
import { useCallback, useState } from 'react';

import { PageHeader, Panel, RecordList } from '@/components/Panel';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import type { HolidayOptIn, HolidayRecord } from '@/lib/types';

interface OptInResponse {
  status: string;
  optins: HolidayOptIn[];
}

export function AdminHolidaysPage() {
  const holidays = usePanel<{ holidays: HolidayRecord[] }>('/api/holidays');
  const optins = usePanel<OptInResponse>('/api/holidays/opt-ins?status=Pending');
  const { pending } = useMutation();

  const [form, setForm] = useState({
    name: '',
    date: new Date().toISOString().slice(0, 10),
    type: 'National',
    location: '',
  });
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState({
    id: 0,
    name: '',
    date: '',
    type: 'National',
    location: '',
  });
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = () => {
    holidays.reload();
    optins.reload();
  };

  const create = useCallback(async () => {
    setError(null);
    try {
      await api.post('/api/holidays', {
        name: form.name.trim(),
        date: form.date,
        type: form.type,
        location: form.location.trim() || undefined,
      });
      setForm((f) => ({ ...f, name: '', location: '' }));
      setMessage('Holiday added.');
      reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not add the holiday.');
    }
  }, [form, holidays, optins]);

  const save = useCallback(
    async (holiday: HolidayRecord) => {
      setError(null);
      try {
        await api.put(`/api/holidays/${holiday.id}`, {
          name: holiday.name,
          date: holiday.date,
          type: holiday.type,
          location: holiday.location ?? null,
        });
        setEditingId(null);
        setMessage('Holiday updated.');
        reload();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Could not update the holiday.');
      }
    },
    [holidays, optins],
  );

  const remove = useCallback(
    async (id: number) => {
      if (!window.confirm('Delete this holiday?')) return;
      setError(null);
      try {
        await api.del(`/api/holidays/${id}`);
        setMessage('Holiday deleted.');
        reload();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Could not delete the holiday.');
      }
    },
    [holidays, optins],
  );

  const reviewOptIn = useCallback(
    async (oid: number, status: 'Approved' | 'Rejected') => {
      setError(null);
      try {
        await api.post(`/api/holidays/opt-ins/${oid}/${status.toLowerCase()}`, {});
        setMessage(`Opt-in ${status.toLowerCase()}.`);
        reload();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Could not review the opt-in.');
      }
    },
    [holidays, optins],
  );

  const rows = holidays.data?.holidays ?? [];
  const pendingOptins = optins.data?.optins ?? [];

  return (
    <div className="page">
      <PageHeader title="Holidays" subtitle="Holiday calendar management." />

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

      <Panel
        title="Add holiday"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <div className="form">
            <div className="form__row">
              <div className="field">
                <label htmlFor="holiday-name">Name</label>
                <input
                  id="holiday-name"
                  value={form.name}
                  onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="holiday-date">Date</label>
                <input
                  id="holiday-date"
                  type="date"
                  value={form.date}
                  onChange={(e) => setForm((f) => ({ ...f, date: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="holiday-type">Type</label>
                <select
                  id="holiday-type"
                  value={form.type}
                  onChange={(e) => setForm((f) => ({ ...f, type: e.target.value }))}
                >
                  <option value="National">National</option>
                  <option value="Optional">Optional</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="holiday-location">Location</label>
                <input
                  id="holiday-location"
                  value={form.location}
                  onChange={(e) => setForm((f) => ({ ...f, location: e.target.value }))}
                  placeholder="Organisation-wide if blank"
                />
              </div>
              <button
                type="button"
                className="btn"
                disabled={pending || !form.name.trim() || !form.date}
                onClick={() => void create()}
              >
                Add
              </button>
            </div>
          </div>
        )}
      </Panel>

      <Panel
        title="Calendar"
        data={rows}
        error={holidays.error}
        loading={holidays.loading}
        reload={holidays.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(holiday) => holiday.id}
            emptyTitle="No holidays."
            emptyHint="Add a holiday above."
            render={(holiday) => (
              <div className="leave-row">
                <div>
                  <strong>{holiday.name}</strong>
                  <span className="list__meta">
                    {` · ${holiday.date} · ${holiday.type}`}
                    {holiday.location ? ` · ${holiday.location}` : ' · organisation-wide'}
                  </span>
                </div>
                <div className="field__actions">
                  <button
                    type="button"
                    className="btn btn--small"
                    onClick={() => {
                      setEditForm({
                        id: holiday.id,
                        name: holiday.name,
                        date: holiday.date,
                        type: holiday.type,
                        location: holiday.location ?? '',
                      });
                      setEditingId(holiday.id);
                    }}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="btn btn--small btn--danger"
                    onClick={() => void remove(holiday.id)}
                  >
                    Delete
                  </button>
                </div>
                {editingId === holiday.id ? (
                  <div className="form" style={{ width: '100%', marginTop: '8px' }}>
                    <div className="form__row">
                      <div className="field">
                        <label htmlFor={`holiday-edit-name-${holiday.id}`}>Name</label>
                        <input
                          id={`holiday-edit-name-${holiday.id}`}
                          value={editForm.name}
                          onChange={(e) =>
                            setEditForm((f) => ({ ...f, name: e.target.value }))}
                        />
                      </div>
                      <div className="field">
                        <label htmlFor={`holiday-edit-date-${holiday.id}`}>Date</label>
                        <input
                          id={`holiday-edit-date-${holiday.id}`}
                          type="date"
                          value={editForm.date}
                          onChange={(e) =>
                            setEditForm((f) => ({ ...f, date: e.target.value }))}
                        />
                      </div>
                      <div className="field">
                        <label htmlFor={`holiday-edit-type-${holiday.id}`}>Type</label>
                        <select
                          id={`holiday-edit-type-${holiday.id}`}
                          value={editForm.type}
                          onChange={(e) =>
                            setEditForm((f) => ({
                              ...f,
                              type: e.target.value,
                            }))}
                        >
                          <option value="National">National</option>
                          <option value="Optional">Optional</option>
                        </select>
                      </div>
                      <div className="field">
                        <label htmlFor={`holiday-edit-location-${holiday.id}`}>Location</label>
                        <input
                          id={`holiday-edit-location-${holiday.id}`}
                          value={editForm.location}
                          onChange={(e) =>
                            setEditForm((f) => ({ ...f, location: e.target.value }))}
                        />
                      </div>
                      <button
                        type="button"
                        className="btn btn--small"
                        onClick={() =>
                          void save({
                            ...holiday,
                            name: editForm.name,
                            date: editForm.date,
                            type: editForm.type,
                            location: editForm.location || null,
                          })
                        }
                      >
                        Save
                      </button>
                      <button
                        type="button"
                        className="btn btn--small"
                        onClick={() => setEditingId(null)}
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : null}
              </div>
            )}
          />
        )}
      </Panel>

      <Panel
        title="Optional holiday requests"
        data={pendingOptins}
        error={optins.error}
        loading={optins.loading}
        reload={optins.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(request) => request.optin_id}
            emptyTitle="No pending opt-ins."
            emptyHint="Employee requests will appear here."
            render={(request) => (
              <div className="leave-row">
                <div>
                  <strong>{request.emp_id}</strong>
                  <span className="list__meta">
                    {` · ${request.holiday} · ${request.holiday_date} · ${request.holiday_type}`}
                  </span>
                </div>
                <div className="field__actions">
                  <button
                    type="button"
                    className="btn btn--small"
                    disabled={pending}
                    onClick={() => void reviewOptIn(request.optin_id, 'Approved')}
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    className="btn btn--small btn--danger"
                    disabled={pending}
                    onClick={() => void reviewOptIn(request.optin_id, 'Rejected')}
                  >
                    Reject
                  </button>
                </div>
              </div>
            )}
          />
        )}
      </Panel>
    </div>
  );
}