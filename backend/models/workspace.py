"""Schedule, assistant, dashboard and report models. Mirrored in frontend/src/lib/types.ts."""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from models.core import (
    AttendanceTrendPoint,
    DeptProduction,
    OrderSummary,
    ProductionPoint,
    new_id,
)

# ---------- Schedule ----------
EventPriority = Literal["low", "medium", "high", "critical"]
EventStatus = Literal["scheduled", "in_progress", "completed", "cancelled"]
EventCategory = Literal["meeting", "maintenance", "delivery", "audit", "other"]


class EventInput(BaseModel):
    title: str = Field(min_length=1)
    description: str = ""
    date: str
    start_time: str
    end_time: str
    participants: list[str] = []
    location: str = ""
    reminder_minutes: int = 15
    priority: EventPriority = "medium"
    status: EventStatus = "scheduled"
    category: EventCategory = "meeting"


class Event(EventInput):
    id: str = Field(default_factory=new_id)
    company_id: str
    created_by: str = "Owner"


# ---------- AI assistant ----------
class ChatMessage(BaseModel):
    id: str = Field(default_factory=new_id)
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    status: Literal["ok", "not_connected"] = "ok"
    link: Optional[str] = None
    link_label: Optional[str] = None


class Conversation(BaseModel):
    id: str = Field(default_factory=new_id)
    company_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessage] = []


class ConversationSummary(BaseModel):
    id: str
    title: str
    updated_at: datetime
    message_count: int


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class AssistantStatus(BaseModel):
    connected: bool
    provider: Optional[str] = None
    message: str


# ---------- Dashboard ----------
class Alert(BaseModel):
    id: str
    severity: Literal["critical", "warning", "info"]
    category: str
    title: str
    detail: str
    link: str


class OverviewKpis(BaseModel):
    orders_today: int
    dispatched_today: int
    pending_orders: int
    overdue_orders: int
    employees_total: int
    employees_present: int
    employees_absent: int
    employees_leave: int
    employees_late: int
    machines_total: int
    machines_running: int
    machines_idle: int
    machines_maintenance: int
    machines_offline: int
    production_today: int
    production_target: int
    achievement: float
    rejected_today: int
    efficiency_today: float


class StatusCount(BaseModel):
    status: str
    count: int


class Overview(BaseModel):
    date: str
    unit: str
    company_name: str
    owner_name: str
    kpis: OverviewKpis
    trend: list[ProductionPoint]
    departments: list[DeptProduction]
    order_status: list[StatusCount]
    order_summary: OrderSummary
    attendance_trend: list[AttendanceTrendPoint]
    alerts: list[Alert]
    today_events: list[Event]


# ---------- Reports ----------
ReportType = Literal[
    "production_daily",
    "production_weekly",
    "performance_monthly",
    "attendance",
    "orders",
    "machines",
    "inventory",
]


class ReportColumn(BaseModel):
    key: str
    label: str


class Report(BaseModel):
    type: ReportType
    title: str
    description: str
    start: str
    end: str
    columns: list[ReportColumn]
    rows: list[dict[str, Any]]
    chart_kind: Literal["bar", "line", "area"]
    chart_x: str
    chart_series: list[ReportColumn]
    totals: list[ReportColumn]  # label holds the formatted total
