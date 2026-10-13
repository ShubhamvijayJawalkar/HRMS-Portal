/**
 * `/login` — the React UI-12 auth pilot.
 *
 * The SPA cannot skip server-side auth side effects. It only calls the same
 * `/login` and `/api/mfa/*` endpoints that the Jinja form does; Flask still
 * owns the session, attendance and lockout behaviour.
 */
import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { ApiError, api, resetCsrfToken } from '@/lib/api';

type MfaStep = 'none' | 'enrol' | 'challenge';

export function LoginPage() {
  const [searchParams] = useSearchParams();
  const [step, setStep] = useState<'password' | 'mfa'>('password');
  const [empId, setEmpId] = useState('');
  const [password, setPassword] = useState('');
  const [mfaCode, setMfaCode] = useState('');
  const [mfaType, setMfaType] = useState<MfaStep>('none');
  const [qrUrl, setQrUrl] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const redirectTo = searchParams.get('redirect') || '/app/dashboard';

  useEffect(() => {
    document.title = 'Login';
  }, []);

  const finishLogin = useCallback((redirect?: string) => {
    resetCsrfToken();
    window.location.assign(redirect || redirectTo);
  }, [redirectTo]);

  const submitPassword = useCallback(async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await api.post<{
        mfa_required?: string;
        redirect?: string;
        message?: string;
      }>('/login', {
        emp_id: empId.trim().toUpperCase(),
        password,
      });

      if (result?.mfa_required) {
        setMfaType(result.mfa_required === 'challenge_required' ? 'challenge' : 'enrol');
        setStep('mfa');

        if (result.mfa_required === 'enrol_required') {
          const enrol = await api.post<{ qr_url?: string }>('/api/mfa/enrol');
          if (enrol?.qr_url) setQrUrl(enrol.qr_url);
        }
      } else {
        setMessage(result?.message || 'Login successful. Redirecting…');
        finishLogin(result?.redirect);
      }
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : 'Connection error. Please try again.',
      );
    } finally {
      setBusy(false);
    }
  }, [empId, finishLogin, password]);

  const submitMfa = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const path = mfaType === 'challenge' ? '/api/mfa/challenge' : '/api/mfa/confirm';
      const result = await api.post<{ redirect?: string; message?: string }>(path, {
        code: mfaCode,
      });
      if (mfaType === 'enrol' && !result?.redirect) {
        // optional enrol done outside login — just stop
      }
      setMessage('Login successful. Redirecting…');
      finishLogin(result?.redirect);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : 'Could not verify the code.',
      );
    } finally {
      setBusy(false);
    }
  }, [finishLogin, mfaCode, mfaType]);

  const forgotPassword = useCallback(async () => {
    const id = window.prompt('Enter your Employee ID:');
    if (!id) return;
    const email = window.prompt('Enter your registered email:');
    if (!email) return;
    try {
      const result = await api.post<{ message?: string; success?: boolean }>(
        '/api/forgot-password',
        { emp_id: id.trim().toUpperCase(), email: email.trim() },
      );
      window.alert(result?.message || 'If details match, an email has been sent.');
    } catch {
      window.alert('Error processing request.');
    }
  }, []);

  if (step === 'mfa') {
    return (
      <div className="login-page">
        <div className="login-card">
          <h1>Two-factor authentication</h1>
          <p>
            {mfaType === 'enrol'
              ? 'Scan the QR code, then enter the 6-digit code.'
              : 'Enter the 6-digit code from your authenticator app.'}
          </p>

          {mfaType === 'enrol' && qrUrl ? (
            <div style={{ textAlign: 'center', margin: '16px 0' }}>
              <img src={qrUrl} alt="MFA QR code" />
            </div>
          ) : null}

          <input
            value={mfaCode}
            onChange={(e) => setMfaCode(e.target.value)}
            placeholder="123456"
            inputMode="numeric"
          />
          {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
          {message ? <p className="notice notice--ok" role="status">{message}</p> : null}
          <button type="button" disabled={busy} onClick={() => void submitMfa()}>
            Verify
          </button>
          <button type="button" onClick={() => setStep('password')}>
            Back
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <h1>HRMS Portal</h1>
        <p>Sign in to your account.</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submitPassword();
          }}
        >
          <div>
            <label htmlFor="login-emp-id">Employee ID</label>
            <input
              id="login-emp-id"
              value={empId}
              onChange={(e) => setEmpId(e.target.value)}
              required
            />
          </div>
          <div>
            <label htmlFor="login-password">Password</label>
            <input
              id="login-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
          {message ? <p className="notice notice--ok" role="status">{message}</p> : null}
          <button type="submit" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
          <button type="button" onClick={() => void forgotPassword()}>
            Forgot password?
          </button>
        </form>
      </div>
    </div>
  );
}

export default LoginPage;