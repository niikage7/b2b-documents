import { useQuery, useQueryClient } from '@tanstack/react-query';
import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo } from 'react';
import { Navigate, Outlet, useLocation, useNavigate } from 'react-router';
import { api } from './api/endpoints';
import { ApiError, setUnauthorizedHandler } from './api/http';
import { keys } from './api/queries';
import type { Me } from './api/types';
import { Spinner } from './components/ui';

interface AuthApi {
  me: Me | null;
  loading: boolean;
  canEditRules: boolean;
  signIn: (me: Me) => void;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthApi | null>(null);

async function fetchMe(): Promise<Me | null> {
  try {
    return await api.me();
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const meQuery = useQuery({ queryKey: keys.me, queryFn: fetchMe, staleTime: Infinity, retry: 1 });

  // Любой 401 после входа — сессия истекла: на экран входа с возвратом на текущую страницу
  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (!qc.getQueryData(keys.me)) return;
      qc.setQueryData(keys.me, null);
      qc.removeQueries({ predicate: (q) => q.queryKey[0] !== 'me' });
      const next = `${window.location.pathname}${window.location.search}`;
      navigate(`/login?expired=1&next=${encodeURIComponent(next)}`, { replace: true });
    });
    return () => setUnauthorizedHandler(null);
  }, [qc, navigate]);

  const signIn = useCallback((me: Me) => qc.setQueryData(keys.me, me), [qc]);
  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      qc.clear();
      qc.setQueryData(keys.me, null);
      navigate('/login', { replace: true });
    }
  }, [qc, navigate]);

  const me = meQuery.data ?? null;
  const value = useMemo<AuthApi>(
    () => ({ me, loading: meQuery.isPending, canEditRules: me?.role === 'methodologist', signIn, signOut }),
    [me, meQuery.isPending, signIn, signOut],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthApi {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth вне AuthProvider');
  return ctx;
}

export function RootLayout() {
  return (
    <AuthProvider>
      <Outlet />
    </AuthProvider>
  );
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const { me, loading } = useAuth();
  const location = useLocation();
  if (loading) {
    return (
      <div style={{ display: 'flex', height: '100%', alignItems: 'center', justifyContent: 'center' }}>
        <Spinner large />
      </div>
    );
  }
  if (!me) {
    const next = `${location.pathname}${location.search}`;
    return <Navigate to={next === '/' ? '/login' : `/login?next=${encodeURIComponent(next)}`} replace />;
  }
  return <>{children}</>;
}
