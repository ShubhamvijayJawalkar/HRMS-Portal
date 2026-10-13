/**
 * `/admin/assets` — issue and return company assets (UI-09).
 *
 * Reaches parity with `templates/assets.html`: pick an active employee and
 * asset details, issue it, then mark it returned from the list. The asset
 * state machine (Issued/Returned) stays on the server.
 */
import { useCallback, useEffect, useState } from 'react';

import { PageHeader, Panel, RecordList, StatusPill } from '@/components/Panel';
import { ValidationErrors } from '@/components/states';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { AssetRecord } from '@/lib/types';

interface UserOption {
  emp_id: string;
  name?: string | null;
}
interface UserListPayload {
  data: UserOption[];
}

interface AssetForm {
  emp_id: string;
  asset_type: string;
  asset_tag: string;
  brand: string;
  model: string;
  serial: string;
}

const TYPES = ['Laptop', 'Monitor', 'Keyboard', 'Mouse', 'Phone', 'Headset', 'Other'];

export function AdminAssetsPage() {
  const { actor } = useSession();
  const assets = usePanel<AssetRecord[]>('/api/assets');
  const users = usePanel<UserListPayload>('/api/users?active=1');
  const { run, pending } = useMutation();

  const [form, setForm] = useState<AssetForm>({
    emp_id: '',
    asset_type: 'Laptop',
    asset_tag: '',
    brand: '',
    model: '',
    serial: '',
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const options = users.data?.data ?? [];
  useEffect(() => {
    if (options.length && !form.emp_id) {
      setForm((f) => ({ ...f, emp_id: options[0]?.emp_id ?? f.emp_id }));
    }
  }, [form.emp_id, options]);

  const issue = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setNotice(null);
      setErrors({});
      if (!form.emp_id) {
        setErrors({ emp_id: 'Select an employee.' });
        return;
      }
      const result = await run(async () => {
        try {
          const created = await api.post<{ id?: number; message?: string }>('/api/assets', {
            emp_id: form.emp_id,
            asset_type: form.asset_type,
            asset_tag: form.asset_tag.trim(),
            brand: form.brand.trim(),
            model: form.model.trim(),
            serial_number: form.serial.trim(),
            issued_date: new Date().toISOString().split('T')[0],
          });
          return { ok: true as const, message: created?.message ?? 'Asset issued.' };
        } catch (err) {
          const message = err instanceof ApiError ? err.message : 'Could not issue the asset.';
          return { ok: false as const, message, fieldErrors: {} as Record<string, string> };
        }
      });
      if (!result) return;
      if (result.ok) {
        setNotice(result.message);
        setForm((f) => ({
          ...f,
          asset_tag: '',
          brand: '',
          model: '',
          serial: '',
        }));
        assets.reload();
      } else {
        setErrors({ _: result.message });
      }
    },
    [assets, form, run],
  );

  const returnAsset = useCallback(
    async (id: number) => {
      if (!window.confirm('Confirm return?')) return;
      try {
        await api.post(`/api/assets/${id}/return`);
        assets.reload();
      } catch (err) {
        setErrors({
          _: err instanceof ApiError ? err.message : 'Could not record the return.',
        });
      }
    },
    [assets],
  );

  const rows = assets.data ?? [];

  return (
    <div className="page">
      <PageHeader
        title="Assets"
        subtitle="Issue and return company assets."
      />

      {notice ? (
        <p className="notice notice--ok" role="status">
          {notice}
        </p>
      ) : null}

      <Panel
        title="Issue asset"
        data={{ ok: true }}
        error={null}
        loading={false}
        reload={() => {}}
      >
        {() => (
          <form className="form" onSubmit={(event) => void issue(event)} noValidate>
            <ValidationErrors errors={errors} />
            <div className="form__row">
              <div className="field">
                <label htmlFor="asset-employee">Employee</label>
                <select
                  id="asset-employee"
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
                <label htmlFor="asset-type">Asset type</label>
                <select
                  id="asset-type"
                  value={form.asset_type}
                  onChange={(e) => setForm((f) => ({ ...f, asset_type: e.target.value }))}
                >
                  {TYPES.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="asset-tag">Asset tag</label>
                <input
                  id="asset-tag"
                  value={form.asset_tag}
                  onChange={(e) => setForm((f) => ({ ...f, asset_tag: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="asset-brand">Brand</label>
                <input
                  id="asset-brand"
                  value={form.brand}
                  onChange={(e) => setForm((f) => ({ ...f, brand: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="asset-model">Model</label>
                <input
                  id="asset-model"
                  value={form.model}
                  onChange={(e) => setForm((f) => ({ ...f, model: e.target.value }))}
                />
              </div>
              <div className="field">
                <label htmlFor="asset-serial">Serial</label>
                <input
                  id="asset-serial"
                  value={form.serial}
                  onChange={(e) => setForm((f) => ({ ...f, serial: e.target.value }))}
                />
              </div>
              <button type="submit" className="btn" disabled={pending || users.loading}>
                {pending ? 'Issuing…' : 'Issue'}
              </button>
            </div>
          </form>
        )}
      </Panel>

      <Panel
        title="All assets"
        data={rows}
        error={assets.error}
        loading={assets.loading}
        reload={assets.reload}
      >
        {(items) => (
          <RecordList
            items={items}
            keyOf={(asset) => asset.id}
            emptyTitle="No assets."
            emptyHint="Issue an asset above and it will appear here."
            render={(asset) => (
              <div className="leave-row">
                <div>
                  <StatusPill status={asset.status} />{' '}
                  <strong>{asset.employee || asset.emp_id}</strong>
                  <span className="list__meta">
                    {' '}
                    · {asset.type}
                    {asset.tag ? ` · tag ${asset.tag}` : ''}
                    {asset.brand || asset.model
                      ? ` · ${[asset.brand, asset.model].filter(Boolean).join(' ')}`
                      : ''}
                    {asset.serial ? ` · serial ${asset.serial}` : ''}
                    {asset.issued ? ` · issued ${new Date(asset.issued).toLocaleDateString()}` : ''}
                    {asset.returned
                      ? ` · returned ${new Date(asset.returned).toLocaleDateString()}`
                      : ''}
                  </span>
                </div>
                {asset.status === 'Issued' ? (
                  <button
                    type="button"
                    className="btn btn--small btn btn--success"
                    onClick={() => void returnAsset(asset.id)}
                  >
                    Return
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
