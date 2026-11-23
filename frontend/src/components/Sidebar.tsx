import { Activity, BarChart3, BriefcaseBusiness, CalendarDays, Factory, FileText, LayoutDashboard, PackageCheck, Settings, ShieldCheck, Users } from 'lucide-react';
import { NavLink, useNavigate } from 'react-router-dom';
import { useAuth } from '@/lib/auth-context';

const navItems = [
  { label: 'Overview', to: '/', icon: LayoutDashboard, permissions: ['reports.company.read'] },
  { label: 'Orders', to: '/orders', icon: BriefcaseBusiness, permissions: ['orders.read', 'orders.department.read'] },
  { label: 'Attendance', to: '/attendance', icon: Users, permissions: ['attendance.company.read', 'attendance.department.read'] },
  { label: 'Production', to: '/production', icon: Activity, permissions: ['production.company.read', 'production.department.read'] },
  { label: 'Machines', to: '/machines', icon: Factory, permissions: ['production.company.read'] },
  { label: 'Inventory', to: '/inventory', icon: PackageCheck, permissions: ['inventory.read'] },
  { label: 'Schedule', to: '/schedule', icon: CalendarDays, permissions: ['schedule.manage'] },
  { label: 'AI Assistant', to: '/assistant', icon: ShieldCheck, permissions: ['assistant.use'] },
  { label: 'Reports', to: '/reports', icon: BarChart3, permissions: ['reports.company.read', 'reports.department.read'] },
  { label: 'Settings', to: '/settings', icon: Settings, permissions: ['company.manage'] },
];

export default function Sidebar() {
  const { user, hasAnyPermission, logout } = useAuth();
  const navigate = useNavigate();
  const visibleItems = navItems.filter((item) => hasAnyPermission(...item.permissions));
  const initials = (user?.name || 'U').split(/\s+/).map((part) => part[0]).slice(0, 2).join('').toUpperCase();

  return (
    <aside className="sidebar-shell">
      <div className="sidebar-header">
        <div className="brand-mark">
          <Factory size={18} />
        </div>
        <div>
          <div className="brand-name">FactoryOS</div>
          <div className="brand-subtitle">Operations Suite</div>
        </div>
      </div>

      <div className="nav-block">
        <div className="nav-section-label">Workspace</div>
        <nav className="nav-list" aria-label="Main navigation">
          {visibleItems.map(({ label, to, icon: Icon }) => (
            <NavLink
              key={label}
              to={to}
              className={({ isActive }) => `nav-item ${isActive ? 'nav-item-active' : ''}`}
            >
              <Icon size={16} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
      </div>

      <div className="sidebar-footer">
        <div className="profile-card">
          <div className="profile-avatar">{initials}</div>
          <div>
            <div className="profile-name">{user?.name}</div>
            <div className="profile-role">{user?.role}{user?.department ? ` · ${user.department}` : ''}</div>
          </div>
        </div>
        <div className="mini-stat">
          <FileText size={14} />
          <span>{user?.company_id}</span>
        </div>
        <button className="sign-out-button" type="button" onClick={async () => { await logout(); navigate('/login', { replace: true }); }}>Sign out</button>
      </div>
    </aside>
  );
}
