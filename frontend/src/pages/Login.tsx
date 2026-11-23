import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';

import { ApiError } from '@/lib/api';
import { useAuth } from '@/lib/auth-context';

interface CompanyChoice { company_id: string; membership_id: string; role: string }

export default function Login() {
  const { login } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [companyId, setCompanyId] = useState('');
  const [choices, setChoices] = useState<CompanyChoice[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError('');
    setBusy(true);
    try {
      await login(email, password, companyId || undefined);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409 && typeof cause.body === 'object' && cause.body !== null) {
        const body = cause.body as { memberships?: CompanyChoice[] };
        if (body.memberships?.length) {
          setChoices(body.memberships);
          setError('Choose the company you want to enter, then sign in again.');
        } else setError('You do not have an active company membership. Contact your administrator.');
      } else if (cause instanceof ApiError && cause.status === 401) {
        setError('Email or password was not recognized.');
      } else {
        setError('Sign in is temporarily unavailable. Please try again.');
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <div className="eyebrow">FactoryOS</div>
        <h1>Sign in</h1>
        <p>Use your company account to continue.</p>
        <label>Email<input autoComplete="username" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
        <label>Password<input autoComplete="current-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        {choices.length > 0 && (
          <label>Company
            <select value={companyId} onChange={(e) => setCompanyId(e.target.value)} required>
              <option value="">Select a company</option>
              {choices.map((choice) => <option key={choice.membership_id} value={choice.company_id}>{choice.company_id} · {choice.role}</option>)}
            </select>
          </label>
        )}
        {error && <div className="auth-error" role="alert">{error}</div>}
        <button type="submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
        <Link to="/password-reset">Forgot password?</Link>
      </form>
    </main>
  );
}
