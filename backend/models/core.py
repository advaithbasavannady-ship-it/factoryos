"""Domain models for FactoryOS. Mirrored by hand in frontend/src/lib/types.ts."""

import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field


def new_id() -> str:
    return str(uuid.uuid4())


# ---------- Company / settings ----------
class AutoConfirmRules(BaseModel):
    enabled: bool = True
    max_order_value: float = Field(default=25000, ge=0)
    repeat_customers_only: bool = True
    advance_payment_pct: float = Field(default=30, ge=0, le=100)
    require_stock_check: bool = True


class RouteTemplate(BaseModel):
    name: str = Field(min_length=1)
    departments: list[str] = Field(min_length=1)


DEFAULT_ROUTES = [
    {"name": "Machined parts", "departments": ["Machining", "Finishing", "Packaging"]},
    {"name": "Fabricated assemblies", "departments": ["Fabrication", "Assembly", "Finishing", "Packaging"]},
    {"name": "Assembly only", "departments": ["Assembly", "Packaging"]},
]


class Company(BaseModel):
    id: str
    name: str
    industry: str
    size: str
    location: str
    currency: str
    production_unit: str
    departments: list[str]
    production_departments: list[str]
    shift_start: str
    shift_end: str
    owner_name: str
    owner_role: str
    auto_confirm: AutoConfirmRules = AutoConfirmRules()
    routes: list[RouteTemplate] = [RouteTemplate(**r) for r in DEFAULT_ROUTES]


class CompanyUpdate(BaseModel):
    auto_confirm: Optional[AutoConfirmRules] = None
    routes: Optional[list[RouteTemplate]] = None
    name: Optional[str] = None
    industry: Optional[str] = None
    size: Optional[str] = None
    location: Optional[str] = None
    currency: Optional[str] = None
    production_unit: Optional[str] = Field(default=None, min_length=1)
    departments: Optional[list[str]] = None
    production_departments: Optional[list[str]] = None
    shift_start: Optional[str] = None
    shift_end: Optional[str] = None
    owner_name: Optional[str] = None
    owner_role: Optional[str] = None


class TodayInfo(BaseModel):
    today: str
    demo_mode: bool = True


# ---------- Customers ----------
class Customer(BaseModel):
    """Snapshot embedded in each order."""
    name: str = Field(min_length=1)
    contact_person: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""


class CustomerInput(Customer):
    industry: str = ""


class CustomerRecord(CustomerInput):
    id: str = Field(default_factory=new_id)
    company_id: str
    created_on: str


class CustomerStats(CustomerRecord):
    order_count: int
    open_orders: int
    total_value: float


# ---------- Users / roles / identity ----------
Role = Literal["owner", "ceo", "admin", "administrator", "manager", "department_manager", "department_employee",
               "inventory_manager", "accountant", "production_manager", "supervisor", "qc_inspector", "dispatch_clerk", "sales_staff"]


class User(BaseModel):
    id: str
    company_id: str
    name: str
    role: Role
    title: str
    department: Optional[str] = None


class UserAuthRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    email: str
    password_hash: str
    name: str
    status: Literal["active", "pending_verification", "suspended"] = "pending_verification"
    auth_provider: Literal["local", "sso"] = "local"
    mfa_enabled: bool = False
    mfa_verified: bool = False
    password_last_changed_at: Optional[str] = None
    last_login_at: Optional[str] = None
    auth_version: int = 1
    password_reset_token_hash: Optional[str] = None
    password_reset_expires_at: Optional[str] = None
    activation_token_hash: Optional[str] = None
    activation_expires_at: Optional[str] = None
    created_at: str = Field(default_factory=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"))
    updated_at: Optional[str] = None


class CompanyMembership(BaseModel):
    id: str = Field(default_factory=new_id)
    company_id: str
    user_id: str
    role: Role
    department: Optional[str] = None
    status: Literal["active", "invited", "suspended", "revoked"] = "active"
    permissions_override: list[str] = []
    invited_by: Optional[str] = None
    joined_at: str = Field(default_factory=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"))
    suspended_at: Optional[str] = None
    revoked_at: Optional[str] = None
    updated_at: Optional[str] = None
    expires_at: Optional[str] = None
    invitation_token_hash: Optional[str] = None
    invitation_expires_at: Optional[str] = None


class SessionRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    user_id: str
    company_id: str
    session_id: str
    issued_at: str = Field(default_factory=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"))
    expires_at: str
    status: Literal["active", "expired", "revoked"] = "active"
    auth_version: int = 1
    revoked_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    user_agent_hash: Optional[str] = None
    ip_hash: Optional[str] = None
    refresh_token_hash: Optional[str] = None
    refresh_token_history: list[str] = Field(default_factory=list)
    refresh_expires_at: Optional[str] = None
    refresh_version: int = 1
    refresh_reuse_detected_at: Optional[str] = None


class RefreshTokenFamily(BaseModel):
    id: str = Field(default_factory=new_id)
    user_id: str
    company_id: str
    session_id: str
    current_version: int = 1
    current_token_hash: str
    status: Literal["active", "replaced", "revoked", "expired"] = "active"
    issued_at: str = Field(default_factory=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"))
    expires_at: str
    last_rotated_at: Optional[str] = None
    revoked_at: Optional[str] = None
    replay_detected_at: Optional[str] = None
    retry_cache_key: Optional[str] = None
    retry_response_ciphertext: Optional[str] = None
    retry_response_expires_at: Optional[str] = None
    audit_note: Optional[str] = None


class RefreshTokenRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    family_id: str
    user_id: str
    company_id: str
    session_id: str
    token_version: int = 1
    token_hash: str
    status: Literal["active", "replaced", "revoked", "expired"] = "active"
    issued_at: str = Field(default_factory=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"))
    expires_at: str
    replaced_by: Optional[str] = None
    revoked_at: Optional[str] = None
    last_used_at: Optional[str] = None
    replayed_at: Optional[str] = None


class PasswordResetToken(BaseModel):
    id: str = Field(default_factory=new_id)
    user_id: str
    token_hash: str
    issued_at: str = Field(default_factory=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"))
    expires_at: str
    used_at: Optional[str] = None
    status: Literal["active", "used", "expired", "revoked"] = "active"


class AuditEvent(BaseModel):
    id: str = Field(default_factory=new_id)
    timestamp: str = Field(default_factory=lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"))
    tenant_id: str
    user_id: Optional[str] = None
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    result: str
    severity: Literal["info", "warning", "critical"] = "info"
    request_id: Optional[str] = None
    ip_hash: Optional[str] = None
    user_agent_hash: Optional[str] = None
    membership_id: Optional[str] = None
    reason: Optional[str] = None
    before_state: Optional[dict] = None
    after_state: Optional[dict] = None


class Notification(BaseModel):
    id: str = Field(default_factory=new_id)
    company_id: str
    recipient_roles: list[str]
    title: str
    body: str
    link: str
    created_at: str
    read_by: list[str] = []


class NotificationItem(BaseModel):
    id: str
    title: str
    body: str
    link: str
    created_at: str
    read: bool


class NotificationFeed(BaseModel):
    items: list[NotificationItem]
    unread: int


# ---------- Orders ----------
# Lifecycle: Received (awaiting_approval | pending_review) -> confirmed -> in_production
#            -> quality_check -> ready_for_dispatch -> dispatched   (+ correction_requested, rejected)
OrderStatus = Literal["awaiting_approval", "correction_requested", "rejected", "pending_review", "confirmed",
                      "in_production", "quality_check", "ready_for_dispatch", "dispatched"]
OrderSource = Literal["online", "offline"]
Priority = Literal["low", "medium", "high"]
HoldKey = Literal["payment", "stock", "customization", "value_limit", "new_customer", "manual"]


class Actor(BaseModel):
    user_id: str
    name: str
    role: str


class TimelineEntry(BaseModel):
    stage: str
    date: str
    note: str = ""
    at: Optional[str] = None  # ISO timestamp
    user: str = "System"
    role: str = "system"


class Hold(BaseModel):
    key: HoldKey
    label: str
    cleared: bool = False
    cleared_by: Optional[str] = None
    cleared_at: Optional[str] = None


class Payment(BaseModel):
    id: str = Field(default_factory=new_id)
    amount: float
    date: str
    method: str
    reference: str = ""
    recorded_by: str


class ProductionStage(BaseModel):
    department: str
    supervisor: Optional[str] = None
    assigned_employees: list[str] = []
    target_date: Optional[str] = None
    received: int = 0
    produced: int = 0  # good units made at this stage
    rejected: int = 0
    transferred: int = 0  # good units handed to the next stage
    status: Literal["waiting", "active", "done"] = "waiting"


class Order(BaseModel):
    id: str = Field(default_factory=new_id)
    company_id: str
    order_no: str
    customer_id: str
    customer: Customer
    product: str
    quantity: int
    unit_price: float
    status: OrderStatus
    priority: Priority
    order_date: str
    expected_delivery: str
    dispatched_date: Optional[str] = None
    notes: str = ""
    department: Optional[str] = None
    production_target_date: Optional[str] = None
    produced: int = 0
    qc_passed: int = 0
    qc_rejected: int = 0
    qc_inspector: Optional[str] = None
    dispatch_carrier: Optional[str] = None
    dispatch_ref: Optional[str] = None
    timeline: list[TimelineEntry] = []
    # approval / intake
    source: OrderSource = "offline"
    submitted_by: Optional[Actor] = None
    submitted_at: Optional[str] = None
    decision: Optional[Literal["approved", "rejected", "correction_requested"]] = None
    decided_by: Optional[Actor] = None
    decided_at: Optional[str] = None
    decision_note: str = ""
    holds: list[Hold] = []
    customization: str = ""
    # money (value = quantity * unit_price; payments are tracked separately)
    payment_terms: Literal["advance", "credit"] = "credit"
    amount_paid: float = 0
    payments: list[Payment] = []
    # production plan
    route_name: Optional[str] = None
    stages: list[ProductionStage] = []
    current_stage: int = 0
    qc_status: Optional[Literal["pending", "passed", "rework"]] = None


class OrderCreate(BaseModel):
    customer_id: str
    product: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    unit_price: float = Field(ge=0)
    priority: Priority = "medium"
    expected_delivery: str
    notes: str = ""
    source: OrderSource = "offline"
    payment_terms: Literal["advance", "credit"] = "credit"
    advance_paid: float = Field(default=0, ge=0)
    customization: str = ""


class OrderUpdate(BaseModel):
    priority: Optional[Priority] = None
    expected_delivery: Optional[str] = None
    notes: Optional[str] = None
    # editable only before confirmation (corrections)
    customer_id: Optional[str] = None
    product: Optional[str] = Field(default=None, min_length=1)
    quantity: Optional[int] = Field(default=None, gt=0)
    unit_price: Optional[float] = Field(default=None, ge=0)
    customization: Optional[str] = None


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject", "request_correction"]
    reason: str = ""


class PaymentIn(BaseModel):
    amount: float = Field(gt=0)
    method: str = Field(default="Bank transfer", min_length=1)
    reference: str = ""


class StageSetup(BaseModel):
    supervisor: Optional[str] = None
    assigned_employees: list[str] = []
    target_date: Optional[str] = None


class AssignIn(BaseModel):
    departments: list[str] = Field(min_length=1)
    route_name: Optional[str] = None
    stages: list[StageSetup] = []


class StageLog(BaseModel):
    produced: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)
    note: str = ""
    machine_id: Optional[str] = None


class TransferIn(BaseModel):
    quantity: int = Field(gt=0)


class InspectIn(BaseModel):
    passed: int = Field(ge=0)
    rejected: int = Field(default=0, ge=0)
    notes: str = ""


class DispatchIn(BaseModel):
    carrier: str = Field(min_length=1)
    reference: str = ""
    notes: str = ""


class OrderSummary(BaseModel):
    total: int
    awaiting_approval: int
    correction_requested: int
    rejected: int
    pending_review: int
    confirmed: int
    in_production: int
    quality_check: int
    ready_for_dispatch: int
    dispatched: int
    pending: int  # open orders: received but not yet dispatched (excludes rejected)
    overdue: int
    received_today: int
    dispatched_today: int
    units_produced: int  # final-stage output only, so units are never double counted
    qc_passed: int
    qc_rejected: int


class OrderList(BaseModel):
    items: list[Order]
    summary: OrderSummary


# ---------- Employees / attendance ----------
AttendanceStatus = Literal["present", "late", "absent", "leave"]


class Employee(BaseModel):
    id: str = Field(default_factory=new_id)
    company_id: str
    emp_code: str
    name: str
    department: str
    role: str
    shift: str
    phone: str
    email: str
    joined_on: str


class AttendanceRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    company_id: str
    employee_id: str
    date: str
    status: AttendanceStatus
    check_in: Optional[str] = None
    check_out: Optional[str] = None


class AttendanceUpdate(BaseModel):
    date: str
    status: AttendanceStatus
    check_in: Optional[str] = None
    check_out: Optional[str] = None


class AttendanceCounts(BaseModel):
    total: int
    present: int  # includes late arrivals
    late: int
    absent: int
    leave: int


class DeptAttendance(AttendanceCounts):
    department: str


class AttendanceRow(BaseModel):
    employee: Employee
    record: Optional[AttendanceRecord] = None


class AttendanceDay(BaseModel):
    date: str
    summary: AttendanceCounts
    departments: list[DeptAttendance]
    rows: list[AttendanceRow]


class AttendanceTrendPoint(BaseModel):
    date: str
    present: int
    late: int
    absent: int
    leave: int


class EmployeeHistory(BaseModel):
    employee: Employee
    records: list[AttendanceRecord]
    summary: AttendanceCounts


# ---------- Production ----------
class ProductionRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    company_id: str
    date: str
    machine_id: str
    department: str
    planned: int
    actual: int
    rejected: int


class ProductionLog(BaseModel):
    date: str
    machine_id: str
    planned: int = Field(ge=0)
    actual: int = Field(ge=0)
    rejected: int = Field(ge=0)


class ProductionTotals(BaseModel):
    planned: int
    actual: int
    rejected: int
    achievement: float  # actual / planned %
    efficiency: float  # good output / planned %
    rejection_rate: float  # rejected / actual %


class ProductionPoint(ProductionTotals):
    label: str


class DeptProduction(ProductionTotals):
    department: str


class MachineProduction(ProductionTotals):
    machine_id: str
    code: str
    name: str
    department: str


class ProductionSummary(BaseModel):
    unit: str
    period: str
    today: ProductionTotals
    week: ProductionTotals
    month: ProductionTotals
    daily: list[ProductionPoint]
    weekly: list[ProductionPoint]
    monthly: list[ProductionPoint]
    departments: list[DeptProduction]
    machines: list[MachineProduction]


# ---------- Machines ----------
MachineStatus = Literal["running", "idle", "maintenance", "offline"]


class Machine(BaseModel):
    id: str = Field(default_factory=new_id)
    company_id: str
    code: str
    name: str
    type: str
    department: str
    manufacturer: str
    status: MachineStatus
    health: int
    utilization: int
    runtime_hours_today: float
    downtime_hours_week: float
    last_maintenance: str
    next_maintenance: str
    installed_on: str
    simulated: bool = True


class MachineUpdate(BaseModel):
    status: MachineStatus


MaintenanceType = Literal["preventive", "breakdown", "calibration", "inspection"]
MaintenanceStatus = Literal["scheduled", "in_progress", "completed"]


class MaintenanceRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    company_id: str
    machine_id: str
    date: str
    type: MaintenanceType
    description: str
    technician: str
    duration_hours: float
    cost: float
    status: MaintenanceStatus


class MaintenanceCreate(BaseModel):
    machine_id: str
    date: str
    type: MaintenanceType
    description: str = Field(min_length=1)
    technician: str = Field(min_length=1)
    duration_hours: float = Field(ge=0)
    cost: float = Field(ge=0)
    status: MaintenanceStatus = "scheduled"


class MachineDetail(BaseModel):
    machine: Machine
    maintenance: list[MaintenanceRecord]
    production: list[ProductionPoint]


# ---------- Inventory ----------
ItemCategory = Literal["raw", "finished"]
PurchaseStatus = Literal["ordered", "received", "cancelled"]


class Supplier(BaseModel):
    id: str = Field(default_factory=new_id)
    company_id: str
    name: str
    contact_person: str
    email: str
    phone: str
    city: str
    materials: list[str]
    rating: float
    lead_time_days: int


class InventoryItem(BaseModel):
    id: str = Field(default_factory=new_id)
    company_id: str
    sku: str
    name: str
    category: ItemCategory
    unit: str
    quantity: float
    reorder_level: float
    unit_cost: float
    supplier_id: Optional[str] = None
    location: str


class StockAdjust(BaseModel):
    delta: float
    reason: str = ""


class Purchase(BaseModel):
    id: str = Field(default_factory=new_id)
    company_id: str
    po_no: str
    supplier_id: str
    item_id: str
    quantity: float
    unit_cost: float
    order_date: str
    expected_date: str
    status: PurchaseStatus


class PurchaseCreate(BaseModel):
    item_id: str
    supplier_id: Optional[str] = None
    quantity: float = Field(gt=0)
    unit_cost: Optional[float] = Field(default=None, ge=0)
    expected_date: str


class PurchaseUpdate(BaseModel):
    status: PurchaseStatus


class ConsumptionSeriesItem(BaseModel):
    item_id: str
    name: str
    unit: str
    total: float
    values: list[float]


class ConsumptionSeries(BaseModel):
    dates: list[str]
    items: list[ConsumptionSeriesItem]


class InventorySummary(BaseModel):
    raw_count: int
    finished_count: int
    stock_value: float
    low_stock: int
    open_purchases: int
