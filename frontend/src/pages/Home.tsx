import {
  AlertTriangle,
  ArrowUpRight,
  BriefcaseBusiness,
  CircleDashed,
  Factory,
  PackageCheck,
  ShieldCheck,
  TrendingUp,
  Users,
  type LucideIcon,
} from 'lucide-react';
import { useQuery } from '@tanstack/react-query';

import { apiGet } from '@/lib/api';
import type { DashboardOverview } from '@/lib/types';

const metricIcons: Record<'orders' | 'attendance' | 'machines' | 'production', LucideIcon> = {
  orders: BriefcaseBusiness,
  attendance: Users,
  machines: Factory,
  production: TrendingUp,
};

function DashboardCard({
  title,
  value,
  meta,
  icon: Icon,
}: {
  title: string;
  value: string;
  meta: string;
  icon: LucideIcon;
}) {
  return (
    <div className="kpi-card">
      <div className="kpi-header">
        <div className="kpi-label">{title}</div>
        <div className="kpi-icon">
          <Icon size={16} />
        </div>
      </div>
      <div className="kpi-value">{value}</div>
      <div className="kpi-meta">{meta}</div>
    </div>
  );
}

export default function Home() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['dashboard-overview'],
    queryFn: () => apiGet<DashboardOverview>('/dashboard/overview'),
  });

  if (isLoading) {
    return (
      <div className="panel state-panel">
        <CircleDashed className="spin" size={18} />
        <div>Loading production overview…</div>
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="panel state-panel error-panel">
        <AlertTriangle size={18} />
        <div>
          <div className="state-title">Connection unavailable</div>
          <div className="state-message">
            FactoryOS could not reach the backend API at /api/dashboard/overview.
            {error instanceof Error ? ` ${error.message}` : ''}
          </div>
        </div>
      </div>
    );
  }

  const overview = data;
  const hasOverviewData = Boolean(
    overview.kpis ||
      overview.trend.length ||
      overview.departments.length ||
      overview.alerts.length ||
      overview.today_events.length,
  );

  if (!hasOverviewData) {
    return (
      <div className="panel state-panel empty-panel">
        <PackageCheck size={18} />
        <div>
          <div className="state-title">No dashboard data available</div>
          <div className="state-message">
            The backend returned an empty overview payload for /api/dashboard/overview.
          </div>
        </div>
      </div>
    );
  }

  const keyMetrics = [
    {
      title: 'Orders Today',
      value: String(overview.kpis.orders_today),
      meta: `${overview.kpis.pending_orders} pending approval`,
      icon: metricIcons.orders,
    },
    {
      title: 'Attendance',
      value: `${overview.kpis.employees_present}/${overview.kpis.employees_total}`,
      meta: `${overview.kpis.employees_late} late arrivals`,
      icon: metricIcons.attendance,
    },
    {
      title: 'Machines',
      value: String(overview.kpis.machines_running),
      meta: `${overview.kpis.machines_maintenance} in maintenance`,
      icon: metricIcons.machines,
    },
    {
      title: 'Production',
      value: `${overview.kpis.production_today.toLocaleString()} ${overview.unit}`,
      meta: `${overview.kpis.achievement.toFixed(1)}% target achievement`,
      icon: metricIcons.production,
    },
  ];

  return (
    <div className="page-stack">
      <section className="hero-row panel">
        <div>
          <div className="eyebrow">Operations overview</div>
          <h1>{overview.company_name}</h1>
          <p>
            Owner view for {overview.owner_name}. {overview.date} · {overview.unit} output tracked in real time.
          </p>
        </div>
        <div className="hero-badges">
          <span className="status-badge success">System online</span>
          <span className="status-badge neutral">Authenticated session</span>
        </div>
      </section>

      <section className="metrics-grid">
        {keyMetrics.map((metric) => (
          <DashboardCard key={metric.title} {...metric} />
        ))}
      </section>

      <section className="content-grid">
        <div className="panel chart-panel">
          <div className="panel-header-row">
            <div>
              <div className="eyebrow">Production trend</div>
              <h2>Output vs target</h2>
            </div>
            <span className="status-badge neutral">Last 14 days</span>
          </div>

          {overview.trend.length ? (
            <div className="chart-visual" aria-label="Production trend chart">
              {overview.trend.slice(-7).map((point) => (
                <div key={point.label} className="chart-bar-group">
                  <div className="chart-bars">
                    <span
                      style={{
                        height: `${Math.min((point.actual / Math.max(point.planned || 1, 1)) * 100, 100)}%`,
                      }}
                      className="chart-bar actual"
                    />
                    <span
                      style={{
                        height: `${Math.min((point.planned / Math.max(point.planned || 1, 1)) * 100, 100)}%`,
                      }}
                      className="chart-bar target"
                    />
                  </div>
                  <small>{point.label.slice(5)}</small>
                </div>
              ))}
            </div>
          ) : (
            <div className="state-message">No production trend data is currently available.</div>
          )}
        </div>

        <div className="panel side-panel">
          <div className="panel-header-row">
            <div>
              <div className="eyebrow">Alerts</div>
              <h2>Operational watchlist</h2>
            </div>
          </div>

          {overview.alerts.length ? (
            <div className="alert-list">
              {overview.alerts.slice(0, 4).map((alert) => (
                <div key={alert.id} className={`alert-item alert-${alert.severity}`}>
                  <div className="alert-dot" />
                  <div>
                    <div className="alert-title">{alert.title}</div>
                    <div className="alert-copy">{alert.detail}</div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="state-message">No active alerts are being reported right now.</div>
          )}
        </div>
      </section>

      <section className="three-column-grid">
        <div className="panel">
          <div className="panel-header-row">
            <div>
              <div className="eyebrow">Department output</div>
              <h2>Production by department</h2>
            </div>
          </div>
          {overview.departments.length ? (
            <div className="list-stack compact">
              {overview.departments.slice(0, 4).map((dept) => (
                <div key={dept.department} className="row-item">
                  <div className="row-head">
                    <span>{dept.department}</span>
                    <strong>{dept.achievement.toFixed(0)}%</strong>
                  </div>
                  <div className="progress-track">
                    <span style={{ width: `${Math.min(dept.achievement, 100)}%` }} className="progress-fill" />
                  </div>
                  <div className="meta-row">
                    <span>{dept.actual} / {dept.target}</span>
                    <span>{dept.rejected} rejected</span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="state-message">No department output data available.</div>
          )}
        </div>

        <div className="panel">
          <div className="panel-header-row">
            <div>
              <div className="eyebrow">Factory health</div>
              <h2>Machine status</h2>
            </div>
          </div>
          <div className="status-grid">
            <div className="mini-tile success">
              <span className="mini-label">Running</span>
              <strong>{overview.kpis.machines_running}</strong>
            </div>
            <div className="mini-tile warning">
              <span className="mini-label">Idle</span>
              <strong>{overview.kpis.machines_idle}</strong>
            </div>
            <div className="mini-tile neutral">
              <span className="mini-label">Maintenance</span>
              <strong>{overview.kpis.machines_maintenance}</strong>
            </div>
            <div className="mini-tile danger">
              <span className="mini-label">Offline</span>
              <strong>{overview.kpis.machines_offline}</strong>
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel-header-row">
            <div>
              <div className="eyebrow">Workforce</div>
              <h2>Attendance</h2>
            </div>
          </div>
          <div className="attendance-summary">
            <div className="attendance-row">
              <Users size={16} />
              <span>Present</span>
              <strong>{overview.kpis.employees_present}</strong>
            </div>
            <div className="attendance-row">
              <ShieldCheck size={16} />
              <span>Absent</span>
              <strong>{overview.kpis.employees_absent}</strong>
            </div>
            <div className="attendance-row">
              <PackageCheck size={16} />
              <span>On leave</span>
              <strong>{overview.kpis.employees_leave}</strong>
            </div>
          </div>
        </div>
      </section>

      <section className="panel slim-panel">
        <div className="panel-header-row">
          <div>
            <div className="eyebrow">Today’s schedule</div>
            <h2>Upcoming events</h2>
          </div>
        </div>

        {overview.today_events.length ? (
          <div className="event-list">
            {overview.today_events.slice(0, 4).map((event) => (
              <div key={event.id} className="event-item">
                <div className="event-time">{event.start_time}</div>
                <div className="event-copy">
                  <div className="event-title">{event.title}</div>
                  <div className="event-meta">{event.category} · {event.location || 'Factory floor'}</div>
                </div>
                <span className="status-badge neutral">{event.status}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="state-message">No scheduled events are available for today.</div>
        )}
      </section>

      <section className="footer-tray">
        <div className="meta-box">
          <div className="eyebrow">Operational performance</div>
          <div className="big-stat">{overview.kpis.efficiency_today.toFixed(1)}%</div>
          <div className="meta-copy">Efficiency today</div>
        </div>
        <div className="meta-box">
          <div className="eyebrow">Rejected output</div>
          <div className="big-stat">{overview.kpis.rejected_today}</div>
          <div className="meta-copy">Units requiring review</div>
        </div>
        <div className="meta-box accent-box">
          <div className="eyebrow">Dispatch</div>
          <div className="big-stat">{overview.kpis.dispatched_today}</div>
          <div className="meta-copy">Orders dispatched</div>
        </div>
      </section>

      <div className="link-row">
        <ArrowUpRight size={14} />
        <span>Data source: /api/dashboard/overview</span>
      </div>
    </div>
  );
}
