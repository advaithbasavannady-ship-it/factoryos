import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

import { apiGet, apiPost, loginRequest, logoutRequest, restoreAccessToken, setAccessToken } from '@/lib/api';

export interface AuthUser {
  id: string;
  name: string;
  email?: string;
  company_id: string;
  membership_id: string;
  role: string;
  department?: string | null;
  department_ids: string[];
}

interface SessionResponse {
  user: AuthUser;
  permissions: string[];
}

interface MembershipChoice { id: string; company_id: string; role: string; department?: string | null }

interface AuthContextValue {
  user: AuthUser | null;
  permissions: Set<string>;
  memberships: MembershipChoice[];
  loading: boolean;
  login: (email: string, password: string, companyId?: string) => Promise<void>;
  logout: () => Promise<void>;
  switchCompany: (companyId: string) => Promise<void>;
  hasPermission: (permission: string) => boolean;
  hasAnyPermission: (...permissions: string[]) => boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [permissions, setPermissions] = useState<Set<string>>(new Set());
  const [memberships, setMemberships] = useState<MembershipChoice[]>([]);
  const [loading, setLoading] = useState(true);

  const loadSession = useCallback(async () => {
    const session = await apiGet<SessionResponse>('/auth/session');
    const memberships = await apiGet<MembershipChoice[]>('/auth/memberships');
    setUser(session.user);
    setPermissions(new Set(session.permissions));
    setMemberships(memberships);
  }, []);

  useEffect(() => {
    let alive = true;
    const onUnauthorized = () => {
      if (alive) {
        setUser(null);
        setPermissions(new Set());
      }
    };
    window.addEventListener('factoryos:unauthorized', onUnauthorized);
    void (async () => {
      try {
        if (await restoreAccessToken()) await loadSession();
      } catch {
        setAccessToken(null);
        setUser(null);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
      window.removeEventListener('factoryos:unauthorized', onUnauthorized);
    };
  }, [loadSession]);

  const login = useCallback(async (email: string, password: string, companyId?: string) => {
    await loginRequest(email, password, companyId);
    await loadSession();
  }, [loadSession]);

  const logout = useCallback(async () => {
    try {
      await logoutRequest();
    } catch {
      // Clear local auth state even if the server cannot be reached.
    } finally {
      setUser(null);
      setPermissions(new Set());
      setMemberships([]);
    }
  }, []);

  const switchCompany = useCallback(async (companyId: string) => {
    const response = await apiPost<{ access_token: string }>('/auth/switch-company', { company_id: companyId });
    setAccessToken(response.access_token);
    await loadSession();
  }, [loadSession]);

  const value = useMemo<AuthContextValue>(() => ({
    user, permissions, memberships, loading, login, logout, switchCompany,
    hasPermission: (permission) => permissions.has(permission),
    hasAnyPermission: (...items) => items.some((permission) => permissions.has(permission)),
  }), [user, permissions, memberships, loading, login, logout, switchCompany]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside AuthProvider');
  return context;
}
