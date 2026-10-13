/**
 * `/admin/salary-structures` — salary structures (UI-09).
 */
import { useCallback, useEffect, useState } from 'react';

import { PageHeader, Panel, RecordList } from '@/components/Panel';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import type { SalaryStructureRecord } from '@/lib/types';

interface UserOption {
  emp_id: string;
  name?: string | null;
}
interface UserListPayload {
  data: UserOption[];
}

export function AdminSalaryStructuresPage() {
  const structures = usePanel<SalaryStructureRecord[]>('/api/salary-structures');
  const users = usePanel<UserListPayload>('/api/users?per_page=200&sort_by=emp_id');
  const { pending } = useMutation();
  const [form, setForm] = useState({
    emp_id: '',
    basic: '',
    hra: '',
    allowances: '',
    deductions: '',
    effective_from: new Date().toISOString().slice(0, 10),
    effective_to: '',
  });
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const options = users.data?.data ?? [];
  useEffect(() => {
    if (options.length && !form.emp_id) {
      setForm((f) => ({ ...f, emp_id: options[0]?.emp_id ?? f.emp_id }));
    }
  }, [form.emp_id, options]);

  const create = useCallback(async () => {
    setError(null);
    try {
      await api.post('/api/salary-structures', {
        emp_id: form.emp_id,
        basic: Number(form.basic),
        hra: Number(form.hra || 0),
        allowances: Number(form.allowances || 0),
        deductions: Number(form.deductions || 0),
        effective_from: form.effective_from,
        effective_to: form.effective_to || undefined,
      });
      setMessage('Salary structure saved.');
      setForm((f) => ({ ...f, basic: '', hra: '', allowances: '', deductions: '', effective_to: '' }));
      structures.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not save the structure.');
    }
  }, [form, structures]);

  const money = (value: number | undefined): string =>
    Number.isFinite(value) ? `₹${Number(value).toFixed(2)}` : '';

  return (
    <div className="page">
      <PageHeader title="Salary structures" subtitle="Define salary structures." />

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
        title="New structure"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <div className="form">
            <div className="form__row">
              <div className="field">
                <label htmlFor="s-employee">Employee</label>
                <select
                  id="s-employee"
                  value={form.emp_id}
                  onChange={(e) => setForm((f) => ({ ...f, emp_id: e.target.value }))}
                >
                  {options.map((user) => (
                    <option key={user.emp_id} value={user.emp_id}>
                      {user.name || user.emp_id} ({user.emp_id})
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="s-basic">Basic</label>
                <input
                  id="s-basic"
                  value={form.basic}
                  onChange={(e) => setForm((f) => ({ ...f, basic: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="s-hra">HRA</label>
                <input
                  id="s-hra"
                  value={form.hra}
                  onChange={(e) => setForm((f) => ({ ...f, hra: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="s-allowances">Allowances</label>
                <input
                  id="s-allowances"
                  value={form.allowances}
                  onChange={(e) => setForm((f) => ({ ...f, allowances: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="s-deductions">Deductions</label>
                <input
                  id="s-deductions"
                  value={form.deductions}
                  onChange={(e) => setForm((f) => ({ ...f, deductions: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="s-from">Effective from</label>
                <input
                  id="s-from"
                  type="date"
                  value={form.effective_from}
                  onChange={(e) => setForm((f) => ({ ...f, effective_from: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="s-to">Effective to</label>
                <input
                  id="s-to"
                  type="date"
                  value={form.effective_to}
                  onChange={(e) => setForm((f) => ({ ...f, effective_to: e.target.value }))}
                />
              </div>
              <button
                type="button"
                className="btn"
                disabled={pending || !form.emp_id || !form.basic}
                onClick={() => void create()}
              >
                Save
              </button>
            </div>
          </div>
        )}
      </Panel>

      <Panel
        title="Structures"
        data={structures.data ?? []}
        error={structures.error}
        loading={structures.loading}
        reload={structures.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(structure) => structure.id}
            emptyTitle="No salary structures."
            emptyHint="Add a structure above."
            render={(structure) => (
              <div className="leave-row">
                <div>
                  <strong>{structure.employee || structure.emp_id}</strong>
                  <span className="list__meta">
                    {` · basic ${money(structure.basic)} · HRA ${money(structure.hra)} · allowances ${money(structure.allowances)} · deductions ${money(structure.deductions)}`}
                    {structure.effective_from ? ` · from ${structure.effective_from}` : ''}
                    {structure.effective_to ? ` · to ${structure.effective_to}` : ''}
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