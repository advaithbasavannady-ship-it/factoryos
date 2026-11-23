import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';

import { apiPost } from '@/lib/api';

export default function PasswordReset() {
  const [email, setEmail] = useState('');
  const [token, setToken] = useState('');
  const [password, setPassword] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function requestReset(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setMessage('');
    try {
      await apiPost('/auth/password-reset/request', { email });
      setMessage('If the account is eligible, reset instructions will be sent.');
    } catch { setError('Password reset is temporarily unavailable.'); }
    finally { setBusy(false); }
  }

  async function confirmReset(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setMessage('');
    try {
      await apiPost('/auth/password-reset/confirm', { token, new_password: password });
      setMessage('Password updated. You can now sign in.');
    } catch { setError('The reset token is invalid, expired, or the password does not meet requirements.'); }
    finally { setBusy(false); }
  }

  return (
    <main className="auth-page">
      <div className="auth-card">
        <div className="eyebrow">FactoryOS</div><h1>Password reset</h1>
        <form onSubmit={requestReset} className="auth-form">
          <label>Email<input type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
          <button type="submit" disabled={busy}>Request reset instructions</button>
        </form>
        <form onSubmit={confirmReset} className="auth-form">
          <label>One-time token<input value={token} onChange={(e) => setToken(e.target.value)} required /></label>
          <label>New password<input type="password" autoComplete="new-password" minLength={12} value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
          <button type="submit" disabled={busy}>Set new password</button>
        </form>
        {message && <div role="status">{message}</div>}{error && <div className="auth-error" role="alert">{error}</div>}
        <Link to="/login">Back to sign in</Link>
      </div>
    </main>
  );
}
