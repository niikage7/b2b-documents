import { Layers, ListChecks, LogOut, Upload } from 'lucide-react';
import { NavLink, Outlet, useMatches } from 'react-router';
import { useAuth } from '../auth';

export function Logo({ compact }: { compact?: boolean }) {
  return (
    <span className="logo">
      <span className={compact ? 'logo__lights logo__lights--vertical' : 'logo__lights'} aria-hidden>
        <i className="dot dot--critical" />
        <i className="dot dot--warning" />
        <i className="dot dot--ok" />
      </span>
      {!compact && <span className="logo__name">B2B Tender</span>}
    </span>
  );
}

interface RouteHandle {
  compactSidebar?: boolean;
}

export function AppLayout() {
  const { me, canEditRules, signOut } = useAuth();
  const compact = useMatches().some((m) => (m.handle as RouteHandle | undefined)?.compactSidebar);
  const nav = [
    { to: '/batches', label: 'Пакеты', icon: <Layers size={18} />, end: false },
    { to: '/upload', label: 'Загрузить пакет', icon: <Upload size={18} />, end: true },
    ...(canEditRules ? [{ to: '/rules', label: 'Правила', icon: <ListChecks size={18} />, end: false }] : []),
  ];
  return (
    <div className="app">
      <aside className={compact ? 'sidebar sidebar--compact' : 'sidebar'}>
        <NavLink to="/batches" className="sidebar__logo" aria-label="B2B Tender — к списку пакетов">
          <Logo compact={compact} />
        </NavLink>
        <nav className="sidebar__nav" aria-label="Разделы">
          {nav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => (isActive ? 'sidebar__link sidebar__link--active' : 'sidebar__link')}
              title={compact ? item.label : undefined}
            >
              {item.icon}
              {!compact && <span>{item.label}</span>}
            </NavLink>
          ))}
        </nav>
        {me && (
          <div className="sidebar__user">
            <span className="avatar" title={compact ? `${me.full_name} · ${me.role_label}` : undefined}>
              {me.initials}
            </span>
            {!compact && (
              <span className="sidebar__user-text">
                <span className="sidebar__user-name">{me.full_name}</span>
                <span className="sidebar__user-role">{me.role_label}</span>
              </span>
            )}
            <button type="button" className="sidebar__logout" onClick={() => void signOut()} title="Выйти" aria-label="Выйти">
              <LogOut size={18} />
            </button>
          </div>
        )}
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
