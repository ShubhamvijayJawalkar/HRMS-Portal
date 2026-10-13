/**
 * `/profile` — the employee's own record (UI-08).
 *
 * Reaches parity with `templates/profile.html`: view and edit the profile,
 * manage dependents, and change the password.
 *
 * **MFA is shown read-only.** `/api/mfa/status` is read and displayed, and the
 * disable control is deliberately absent: the migration plan defers MFA
 * (FR-AUTH-11), so this screen neither enables it nor assumes it. Surfacing the
 * status keeps the information the legacy page had without offering an action
 * this phase is not chartered to own.
 */
import { useCallback, useEffect, useState } from 'react';

import { Panel, PageHeader, RecordList, RefreshButton } from '@/components/Panel';
import { ValidationErrors } from '@/components/states';
import { ApiError, api } from '@/lib/api';
import { useMutation, usePanel } from '@/lib/hooks';
import { useSession } from '@/lib/session';
import type { Dependent, MfaStatus, Profile } from '@/lib/types';

/**
 * The fields an employee may change about themselves.
 *
 * Deliberately *not* the whole profile: role, department, designation, employee
 * id and the reporting line are set by HR, and offering them as editable fields
 * implies a control that does not exist. The server rejects them anyway — this is
 * about not asking for them.
 */
const EDITABLE = [
  { key: 'name', label: 'Full name', type: 'text' },
  { key: 'email', label: 'Email', type: 'email' },
  { key: 'phone', label: 'Phone', type: 'tel' },
  { key: 'address', label: 'Address', type: 'text' },
  { key: 'emergency_contact_name', label: 'Emergency contact', type: 'text' },
  { key: 'emergency_contact_phone', label: 'Emergency contact phone', type: 'tel' },
] as const;

type EditableKey = (typeof EDITABLE)[number]['key'];

export function ProfilePage() {
  const { actor } = useSession();
  const profile = usePanel<Profile>('/api/profile');
  const dependents = usePanel<Dependent[]>('/api/dependents');
  const mfa = usePanel<MfaStatus>('/api/mfa/status');
  const { run, pending } = useMutation();

  const [draft, setDraft] = useState<Partial<Record<EditableKey, string>>>({});
  const [notice, setNotice] = useState<{ tone: 'ok' | 'bad'; text: string } | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [dirty, setDirty] = useState(false);

  const [dependent, setDependent] = useState({ name: '', relationship: 'Child', date_of_birth: '' });
  const [password, setPassword] = useState({ current: '', next: '', confirm: '' });

  /*
   * Seed the form from the server payload, once it arrives.
   *
   * The legacy page filled the form in `onload`, which meant a reload discarded
   * unsaved edits silently. Re-seeding only while the form is pristine means a
   * background refresh cannot wipe what somebody is halfway through typing.
   */
  useEffect(() => {
    if (!profile.data || dirty) return;
    const seed: Partial<Record<EditableKey, string>> = {};
    for (const field of EDITABLE) {
      seed[field.key] = String(profile.data[field.key] ?? '');
    }
    setDraft(seed);
  }, [profile.data, dirty]);

  const save = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setNotice(null);
      setErrors({});

      const payload: Record<string, string> = {};
      for (const field of EDITABLE) {
        const value = (draft[field.key] ?? '').trim();
        if (value) payload[field.key] = value;
      }
      if (Object.keys(payload).length === 0) {
        setErrors({ _: 'Nothing to save — no field has a value.' });
        return;
      }

      const result = await run(async () => {
        try {
          const updated = await api.put<Profile>('/api/profile', payload);
          return { ok: true as const, data: updated };
        } catch (err) {
          return {
            ok: false as const,
            errors:
              err instanceof ApiError && err.payload && typeof err.payload === 'object'
                ? flattenErrors(err.payload as Record<string, unknown>, err.message)
                : { _: err instanceof ApiError ? err.message : 'Could not save.' },
          };
        }
      });

      if (!result) return;
      if (result.ok) {
        setDirty(false);
        setNotice({ tone: 'ok', text: 'Profile updated.' });
        profile.reload();
      } else {
        setErrors(result.errors);
      }
    },
    [draft, run, profile],
  );

  const addDependent = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setNotice(null);
      if (!dependent.name.trim()) {
        setErrors({ dependent_name: 'A name is required.' });
        return;
      }
      const result = await run(async () => {
        try {
          await api.post('/api/dependents', {
            name: dependent.name.trim(),
            relationship: dependent.relationship,
            date_of_birth: dependent.date_of_birth || null,
          });
          return { ok: true as const };
        } catch (err) {
          return {
            ok: false as const,
            text: err instanceof ApiError ? err.message : 'Could not add the dependent.',
          };
        }
      });
      if (!result) return;
      if (result.ok) {
        setDependent({ name: '', relationship: 'Child', date_of_birth: '' });
        setNotice({ tone: 'ok', text: 'Dependent added.' });
        dependents.reload();
      } else {
        setNotice({ tone: 'bad', text: result.text });
      }
    },
    [dependent, run, dependents],
  );

  const removeDependent = useCallback(
    async (id: number) => {
      setNotice(null);
      const result = await run(async () => {
        try {
          await api.del(`/api/dependents/${id}`);
          return { ok: true as const };
        } catch (err) {
          return {
            ok: false as const,
            text: err instanceof ApiError ? err.message : 'Could not remove the dependent.',
          };
        }
      });
      if (!result) return;
      if (result.ok) {
        setNotice({ tone: 'ok', text: 'Dependent removed.' });
        dependents.reload();
      } else {
        setNotice({ tone: 'bad', text: result.text });
      }
    },
    [run, dependents],
  );

  const changePassword = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setNotice(null);
      // Clear first. Without it a previous attempt's mismatch message stays on
      // screen after a *successful* change, so the form claims the passwords
      // disagree while the password has in fact been changed.
      setErrors({});
      if (password.next !== password.confirm) {
        setErrors({ password_confirm: 'The two passwords do not match.' });
        return;
      }
      if (!password.current || !password.next) {
        setErrors({
          password_current: !password.current ? 'Enter your current password.' : '',
          password_next: !password.next ? 'Enter a new password.' : '',
        });
        return;
      }
      const result = await run(async () => {
        try {
          const response = await api.post<{ message?: string }>('/api/change-password', {
            current_password: password.current,
            new_password: password.next,
          });
          return { ok: true as const, message: response.message };
        } catch (err) {
          return {
            ok: false as const,
            text: err instanceof ApiError ? err.message : 'Could not change the password.',
          };
        }
      });
      if (!result) return;
      if (result.ok) {
        setPassword({ current: '', next: '', confirm: '' });
        setNotice({ tone: 'ok', text: result.message ?? 'Password changed.' });
      } else {
        setNotice({ tone: 'bad', text: result.text });
      }
    },
    [password, run],
  );

  const refreshAll = () => {
    profile.reload();
    dependents.reload();
    mfa.reload();
  };

  return (
    <div className="page">
      <PageHeader
        title="Profile"
        subtitle={
          <>
            Signed in as <strong>{actor?.emp_id}</strong>
            {actor?.role ? ` · ${actor.role}` : ''}
          </>
        }
        actions={<RefreshButton onClick={refreshAll} pending={pending} />}
      />

      {notice ? (
        <p className={notice.tone === 'ok' ? 'notice notice--ok' : 'notice notice--bad'}
           role={notice.tone === 'ok' ? 'status' : 'alert'}>
          {notice.text}
        </p>
      ) : null}

      <Panel
        title="Your details"
        data={profile.data}
        error={profile.error}
        loading={profile.loading}
        reload={profile.reload}
      >
        {() => (
          /*
           * The render callback ignores the payload deliberately: this form is
           * seeded by the `useEffect` above from `profile.data`, and re-seeding
           * it on every render would discard what somebody is typing. `dirty`
           * guards the seeding; the callback must not re-seed on its own.
           */
          <form className="form" onSubmit={(event) => void save(event)} noValidate>
            <ValidationErrors errors={errors} />
            <div className="form__row">
              {EDITABLE.map((field) => (
                <div className="field" key={field.key}>
                  <label htmlFor={`p-${field.key}`}>{field.label}</label>
                  <input
                    id={`p-${field.key}`}
                    type={field.type}
                    value={draft[field.key] ?? ''}
                    aria-invalid={Boolean(errors[field.key]) || undefined}
                    onChange={(event) => {
                      setDraft((d) => ({ ...d, [field.key]: event.target.value }));
                      setDirty(true);
                    }}
                  />
                  {errors[field.key] ? (
                    <p className="field__error">{errors[field.key]}</p>
                  ) : null}
                </div>
              ))}
            </div>
            <button type="submit" className="btn" disabled={pending || !dirty}>
              {pending ? 'Saving…' : 'Save changes'}
            </button>
            {!dirty ? (
              <p className="metric__note">
                No unsaved changes. HR manages your role, department, designation and
                reporting line.
              </p>
            ) : null}
          </form>
        )}
      </Panel>

      <div className="grid">
        <Panel
          title="Employment"
          data={profile.data}
          error={profile.error}
          loading={profile.loading}
          reload={profile.reload}
        >
          {(data) => (
            <dl className="kv">
              <dt>Employee ID</dt>
              <dd>{data.emp_id}</dd>
              <dt>Role</dt>
              <dd>{String(data.role ?? '—')}</dd>
              <dt>Department</dt>
              <dd>{String(data.department ?? '—')}</dd>
              <dt>Designation</dt>
              <dd>{String(data.designation ?? '—')}</dd>
              <dt>Joined</dt>
              <dd>{String(data.date_of_joining ?? '—')}</dd>
              <dt>Manager</dt>
              <dd>{data.manager_emp_id ? String(data.manager_emp_id) : '—'}</dd>
            </dl>
          )}
        </Panel>

        <Panel
          title="Security"
          data={mfa.data}
          error={mfa.error}
          loading={mfa.loading}
          reload={mfa.reload}
        >
          {(data) => (
            <>
              <dl className="kv">
                <dt>Two-factor</dt>
                <dd>{data.enrolled ? 'Enrolled' : 'Not enrolled'}</dd>
                <dt>Required for role</dt>
                <dd>{data.mandatory ? 'Yes' : 'No'}</dd>
              </dl>
              <p className="metric__note">
                Multi-factor authentication is not managed in this release. Ask an
                administrator if your role requires it.
              </p>
            </>
          )}
        </Panel>
      </div>

      <Panel
        title="Dependents"
        data={dependents.data ?? []}
        error={dependents.error}
        loading={dependents.loading}
        reload={dependents.reload}
        actions={<RefreshButton onClick={dependents.reload} />}
      >
        {(rows) => (
          <>
            <RecordList
              items={rows}
              keyOf={(d) => d.id}
              emptyTitle="No dependents recorded."
              emptyHint="Add anyone who is covered under your benefits."
              render={(d) => (
                <div className="leave-row">
                  <div>
                    <strong>{d.name}</strong>
                    <span className="list__meta">
                      {d.relationship}
                      {d.date_of_birth ? ` · born ${d.date_of_birth}` : ''}
                    </span>
                  </div>
                  <button
                    type="button"
                    className="btn btn--small"
                    disabled={pending}
                    onClick={() => void removeDependent(d.id)}
                  >
                    Remove
                  </button>
                </div>
              )}
            />
            <form className="form form--inline" onSubmit={(event) => void addDependent(event)} noValidate>
              <h3 className="panel__subhead">Add a dependent</h3>
              <div className="form__row">
                <div className="field">
                  <label htmlFor="dep-name">Name</label>
                  <input
                    id="dep-name"
                    value={dependent.name}
                    onChange={(e) => setDependent((d) => ({ ...d, name: e.target.value }))}
                  />
                  {errors.dependent_name ? (
                    <p className="field__error">{errors.dependent_name}</p>
                  ) : null}
                </div>
                <div className="field">
                  <label htmlFor="dep-relationship">Relationship</label>
                  <select
                    id="dep-relationship"
                    value={dependent.relationship}
                    onChange={(e) => setDependent((d) => ({ ...d, relationship: e.target.value }))}
                  >
                    {['Spouse', 'Child', 'Parent', 'Other'].map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="dep-dob">Date of birth</label>
                  <input
                    id="dep-dob"
                    type="date"
                    value={dependent.date_of_birth}
                    onChange={(e) => setDependent((d) => ({ ...d, date_of_birth: e.target.value }))}
                  />
                </div>
              </div>
              <button type="submit" className="btn" disabled={pending}>
                Add dependent
              </button>
            </form>
          </>
        )}
      </Panel>

      <Panel title="Change password" data={{ ok: true }} error={null} loading={false} reload={refreshAll}>
        {() => (
          <form className="form" onSubmit={(event) => void changePassword(event)} noValidate>
            <div className="form__row">
              <div className="field">
                <label htmlFor="pw-current">Current password</label>
                <input
                  id="pw-current"
                  type="password"
                  autoComplete="current-password"
                  value={password.current}
                  aria-invalid={Boolean(errors.password_current) || undefined}
                  onChange={(e) => setPassword((p) => ({ ...p, current: e.target.value }))}
                />
                {errors.password_current ? (
                  <p className="field__error">{errors.password_current}</p>
                ) : null}
              </div>
              <div className="field">
                <label htmlFor="pw-new">New password</label>
                <input
                  id="pw-new"
                  type="password"
                  autoComplete="new-password"
                  value={password.next}
                  aria-invalid={Boolean(errors.password_next) || undefined}
                  onChange={(e) => setPassword((p) => ({ ...p, next: e.target.value }))}
                />
                {errors.password_next ? (
                  <p className="field__error">{errors.password_next}</p>
                ) : null}
              </div>
              <div className="field">
                <label htmlFor="pw-confirm">Confirm new password</label>
                <input
                  id="pw-confirm"
                  type="password"
                  autoComplete="new-password"
                  value={password.confirm}
                  aria-invalid={Boolean(errors.password_confirm) || undefined}
                  onChange={(e) => setPassword((p) => ({ ...p, confirm: e.target.value }))}
                />
                {errors.password_confirm ? (
                  <p className="field__error">{errors.password_confirm}</p>
                ) : null}
              </div>
            </div>
            <p className="metric__note">
              At least 10 characters, and it must not appear in a known breach list.
            </p>
            <button type="submit" className="btn" disabled={pending}>
              {pending ? 'Changing…' : 'Change password'}
            </button>
          </form>
        )}
      </Panel>
    </div>
  );
}

/**
 * The directory validator returns `{"field": ["message", status]}` pairs, which
 * is a shape a form can use directly if flattened by hand — and cannot use at
 * all if rendered as `[object Object]`.
 */
function flattenErrors(payload: Record<string, unknown>, fallback: string): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const [key, value] of Object.entries(payload)) {
    if (Array.isArray(value)) {
      errors[key] = value.map((v) => String(v)).join(' ');
    } else if (typeof value === 'string') {
      errors[key] = value;
    }
  }
  if (Object.keys(errors).length === 0) errors._ = fallback;
  return errors;
}