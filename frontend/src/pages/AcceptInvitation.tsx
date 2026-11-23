import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';

import { apiPost } from '@/lib/api';

export default function AcceptInvitation() {
  const [token, setToken] = useState('');
  const [password, setPassword] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setMessage('');
    try {
      await apiPost('/auth/invitations/accept', { token, ...(password ? { new_password: password } : {}) });
      setMessage('Invitation accepted. Sign in to continue.');
    } catch { setError('The invitation is invalid, expired, or requires a different password setup.'); }
    finally { setBusy(false); }
  }
  return <main className="auth-page"><form className="auth-card" onSubmit={submit}>
    <div className="eyebrow">FactoryOS</div><h1>Accept company invitation</h1>
    <label>One-time invitation token<input value={token} onChange={(e) => setToken(e.target.value)} required /></label>
    <label>Set password for a new account<input type="password" autoComplete="new-password" minLength={12} value={password} onChange={(e) => setPassword(e.target.value)} /></label>
    <button disabled={busy}>{busy ? 'Accepting…' : 'Accept invitation'}</button>
    {message && <div role="status">{message} <Link to="/login">Sign in</Link></div>}
    {error && <div className="auth-error" role="alert">{error}</div>}
  </form></main>;
}
