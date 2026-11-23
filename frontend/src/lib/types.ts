export interface DashboardKpis {
  orders_today: number;
  dispatched_today: number;
  pending_orders: number;
  overdue_orders: number;
  employees_total: number;
  employees_present: number;
  employees_absent: number;
  employees_leave: number;
  employees_late: number;
  machines_total: number;
  machines_running: number;
  machines_idle: number;
  machines_maintenance: number;
  machines_offline: number;
  production_today: number;
  production_target: number;
  achievement: number;
  rejected_today: number;
  efficiency_today: number;
}

export interface DashboardAlert {
  id: string;
  severity: 'critical' | 'warning' | 'info';
  category: string;
  title: string;
  detail: string;
  link: string;
}

export interface DashboardOrderSummary {
  total: number;
  awaiting_approval: number;
  correction_requested: number;
  rejected: number;
  pending_review: number;
  confirmed: number;
  in_production: number;
  quality_check: number;
  ready_for_dispatch: number;
  dispatched: number;
  pending: number;
  overdue: number;
  received_today: number;
  dispatched_today: number;
  units_produced: number;
  qc_passed: number;
  qc_rejected: number;
}

export interface DashboardTrendPoint {
  label: string;
  actual: number;
  planned: number;
  efficiency: number;
  rejected: number;
}

export interface DashboardDepartment {
  department: string;
  target: number;
  actual: number;
  achievement: number;
  rejected: number;
  efficiency: number;
}

export interface DashboardOverview {
  date: string;
  unit: string;
  company_name: string;
  owner_name: string;
  kpis: DashboardKpis;
  alerts: DashboardAlert[];
  order_status: Array<{ status: string; count: number }>;
  order_summary: DashboardOrderSummary;
  attendance_trend: Array<{ label: string; present: number; absent: number; late: number }>;
  today_events: Array<{ id: string; title: string; date: string; start_time: string; end_time: string; status: string; category: string; location?: string }>;
  departments: DashboardDepartment[];
  trend: DashboardTrendPoint[];
}

export interface ApiErrorShape {
  status: number;
  body: unknown;
}
