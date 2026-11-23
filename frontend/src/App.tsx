import { Navigate, Route, Routes } from 'react-router-dom';

import Layout from '@/components/Layout';
import Home from '@/pages/Home';
import Login from '@/pages/Login';
import PasswordReset from '@/pages/PasswordReset';
import AcceptInvitation from '@/pages/AcceptInvitation';
import { useAuth } from '@/lib/auth-context';

function HomeAccess() {
  const { hasPermission } = useAuth();
  if (!hasPermission('reports.company.read')) {
    return <div className="panel state-panel"><div><div className="state-title">Dashboard access restricted</div><div className="state-message">Your company role does not include factory-wide dashboard access.</div></div></div>;
  }
  return <Home />;
}

function ProtectedLayout() {
  const { user, loading } = useAuth();
  if (loading) return <div className="auth-loading">Restoring your session…</div>;
  if (!user) return <Navigate to="/login" replace />;
  return <Layout />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/password-reset" element={<PasswordReset />} />
      <Route path="/accept-invitation" element={<AcceptInvitation />} />
      <Route element={<ProtectedLayout />}>
        <Route path="/" element={<HomeAccess />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
