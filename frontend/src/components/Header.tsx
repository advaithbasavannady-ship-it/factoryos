import { Bell, Search, Sparkles } from 'lucide-react';
import { useAuth } from '@/lib/auth-context';

export default function Header() {
  const { user, memberships, switchCompany } = useAuth();
  return (
    <header className="topbar">
      <div className="topbar-search">
        <Search size={16} />
        <input aria-label="Search" placeholder="Search orders, machines, inventory..." />
      </div>

      <div className="topbar-actions">
        <div className="status-pill live-pill">
          <Sparkles size={14} />
          <span>FactoryOS</span>
        </div>

        <button className="icon-button" type="button" aria-label="Notifications">
          <Bell size={16} />
        </button>

        {memberships.length > 1 ? (
          <label aria-label="Active company">
            <select className="factory-pill" value={user?.company_id} onChange={(event) => void switchCompany(event.target.value)}>
              {memberships.map((membership) => <option key={membership.id} value={membership.company_id}>{membership.company_id}</option>)}
            </select>
          </label>
        ) : (
          <div className="factory-pill"><span>{user?.company_id}</span></div>
        )}
      </div>
    </header>
  );
}
