/**
 * `/admin/users` — user directory (UI-09).
 */
import { useCallback, useEffect, useState } from 'react';

import { PageHeader, Panel, RecordList, StatusPill } from '@/components/Panel';
import { ApiError, api } from '@/lib/api';
import { useMutation } from '@/lib/hooks';

import type { UserRecord } from '@/lib/types';

interface UserResponse {
  data: UserRecord[];
  total: number;
  page: number;
  per_page: number;
}

export function AdminUsersPage() {
  const { pending } = useMutation();
  const [users, setUsers] = useState<UserRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [roleFilter, setRoleFilter] = useState('');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({
    emp_id: '',
    name: '',
    email: '',
    password: '',
    role: 'Employee',
    department: '',
    status: 'Active',
  });

  const roles = ['Employee', 'Team Leader', 'HR', 'Finance', 'Admin', 'Super Admin'];

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      params.set('page', String(page));
      params.set('per_page', '25');
      params.set('sort_by', 'emp_id');
      if (search) params.set('search', search);
      if (statusFilter) params.set('status', statusFilter);
      if (roleFilter) params.set('role', roleFilter);
      const result = await api.get<UserResponse>(`/api/users?${params.toString()}`);
      setUsers(result.data);
      setTotal(result.total);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not load users.');
    } finally {
      setLoading(false);
    }
  }, [page, roleFilter, search, statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  const runAction = useCallback(
    async (empId: string, path: 'block' | 'unblock' | 'archive' | 'restore') => {
      try {
        await api.post(`/api/users/${empId}/${path}`);
        setMessage(`User ${path}ed.`);
        await load();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Could not update the user.');
      }
    },
    [load],
  );

  const createUser = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setCreating(true);
      setError(null);
      try {
        await api.post('/api/users', {
          emp_id: form.emp_id.trim(),
          name: form.name.trim(),
          email: form.email.trim(),
          password: form.password,
          role: form.role,
          department: form.department.trim() || null,
          status: form.status,
        });
        setMessage('User created.');
        setForm({
          emp_id: '',
          name: '',
          email: '',
          password: '',
          role: 'Employee',
          department: '',
          status: 'Active',
        });
        await load();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : 'Could not create the user.');
      } finally {
        setCreating(false);
      }
    },
    [form, load],
  );

  const totalPages = Math.max(1, Math.ceil(total / 25));

  return (
    <div className="page">
      <PageHeader title="User management" subtitle="Directory and user lifecycle." />

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
        title="Create user"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <form className="form" onSubmit={(e) => void createUser(e)}>
            <div className="form__row">
              <div className="field">
                <label htmlFor="u-empid">Employee ID</label>
                <input
                  id="u-empid"
                  value={form.emp_id}
                  onChange={(e) => setForm((f) => ({ ...f, emp_id: e.target.value }))}
                  required
                />
              </div>
              <div className="field">
                <label htmlFor="u-name">Name</label>
                <input
                  id="u-name"
                  value={form.name}
                  onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                  required
                />
              </div>
              <div className="field">
                <label htmlFor="u-email">Email</label>
                <input
                  id="u-email"
                  type="email"
                  value={form.email}
                  onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
                  required
                />
              </div>
              <div className="field">
                <label htmlFor="u-password">Password</label>
                <input
                  id="u-password"
                  type="password"
                  value={form.password}
                  onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="u-role">Role</label>
                <select
                  id="u-role"
                  value={form.role}
                  onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))}
                >
                  {roles.map((role) => (
                    <option key={role} value={role}>
                      {role}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="u-dept">Department</label>
                <input
                  id="u-dept"
                  value={form.department}
                  onChange={(e) => setForm((f) => ({ ...f, department: e.target.value }))}
                />
              </div>
              <button
                type="submit"
                className="btn"
                disabled={creating}
              >
                {creating ? 'Creating…' : 'Create user'}
              </button>
            </div>
          </form>
        )}
      </Panel>

      <Panel
        title="Filters"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <div className="form__row">
            <div className="field">
              <label htmlFor="u-search">Search</label>
              <input
                id="u-search"
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  setPage(1);
                }}
              />
            </div>
            <div className="field">
              <label htmlFor="u-status-filter">Status</label>
              <select
                id="u-status-filter"
                value={statusFilter}
                onChange={(e) => {
                  setStatusFilter(e.target.value);
                  setPage(1);
                }}
              >
                <option value="">All statuses</option>
                <option value="Active">Active</option>
                <option value="Onboarding">Onboarding</option>
                <option value="Blocked">Blocked</option>
                <option value="Archived">Archived</option>
                <option value="Inactive">Inactive</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="u-role-filter">Role</label>
              <select
                id="u-role-filter"
                value={roleFilter}
                onChange={(e) => {
                  setRoleFilter(e.target.value);
                  setPage(1);
                }}
              >
                <option value="">All roles</option>
                {roles.map((role) => (
                  <option key={role} value={role}>
                    {role}
                  </option>
                ))}
              </select>
            </div>
          </div>
        )}
      </Panel>

      <Panel
        title="Directory"
        data={users}
        error={null}
        loading={loading}
        reload={() => void load()}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(user) => user.emp_id}
            emptyTitle="No users."
            emptyHint="Create a user above."
            render={(user) => (
              <div className="leave-row">
                <div>
                  <StatusPill status={user.status ?? 'Active'} />{' '}
                  <strong>{user.name || user.emp_id}</strong>
                  <span className="list__meta">
                    {` · ${user.emp_id} · ${user.email || ''} · ${user.role || ''} · ${user.department || 'No department'}`}
                    {user.locked ? ' · locked' : ''}
                  </span>
                </div>
                <div className="field__actions">
                  {user.status === 'Blocked' ? (
                    <button
                      type="button"
                      className="btn btn--small"
                      disabled={pending}
                      onClick={() => void runAction(user.emp_id, 'unblock')}
                    >
                      Unblock
                    </button>
                  ) : null}
                  {user.status !== 'Blocked' && user.status !== 'Archived' ? (
                    <button
                      type="button"
                      className="btn btn--small"
                      disabled={pending}
                      onClick={() => void runAction(user.emp_id, 'block')}
                    >
                      Block
                    </button>
                  ) : null}
                  {user.status === 'Archived' ? (
                    <button
                      type="button"
                      className="btn btn--small"
                      disabled={pending}
                      onClick={() => void runAction(user.emp_id, 'restore')}
                    >
                      Restore
                    </button>
                  ) : null}
                  {user.status !== 'Archived' ? (
                    <button
                      type="button"
                      className="btn btn--small btn--danger"
                      disabled={pending}
                      onClick={() => void runAction(user.emp_id, 'archive')}
                    >
                      Archive
                    </button>
                  ) : null}
                </div>
              </div>
            )}
          />
        )}
      </Panel>

      <div className="field__actions">
        <button
          type="button"
          className="btn btn--small"
          disabled={page <= 1}
          onClick={() => setPage((p) => Math.max(1, p - 1))}
        >
          Previous
        </button>
        <span className="list__meta">
          Page {page} / {totalPages} · {total} users
        </span>
        <button
          type="button"
          className="btn btn--small"
          disabled={page >= totalPages}
          onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
        >
          Next
        </button>
      </div>
    </div>
  );
}