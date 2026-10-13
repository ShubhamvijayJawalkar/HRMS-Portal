import { useCallback, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { ApiError, api } from '@/lib/api';

export function ResetPasswordPage() {
  const [searchParams] = useSearchParams();
  const token = searchParams.get('token') || '';
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = useCallback(async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    if (password !== confirm) {
      setError('Passwords do not match.');
      setBusy(false);
      return;
    }
    try {
      await api.post('/api/reset-password', { token, new_password: password });
      setMessage('Password reset successful. You can now log in.');
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not reset the password.');
    } finally {
      setBusy(false);
    }
  }, [confirm, password, token]);

  return (
    <div className="login-page">
      <div className="login-card">
        <h1>Reset password</h1>
        {token ? null : <p className="notice notice--bad">Missing token.</p>}
        {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
        {message ? <p className="notice notice--ok" role="status">{message}</p> : null}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <div>
            <label htmlFor="new-password">New password</label>
            <input
              id="new-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <div>
            <label htmlFor="confirm-password">Confirm password</label>
            <input
              id="confirm-password"
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
            />
          </div>
          <button type="submit" disabled={busy || !token}>
            {busy ? 'Resetting…' : 'Reset password'}
          </button>
        </form>
      </div>
    </div>
  );
}

export default ResetPasswordPage;