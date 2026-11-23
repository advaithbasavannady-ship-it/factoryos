from datetime import date, timedelta

from fastapi import APIRouter, Depends

from lib.analytics import attendance_trend, count_attendance, group_by, order_summary, pct, production_records, sum_records
from lib.dates import today_iso
from lib.db import db
from lib.tenant import get_company_id
from lib.authz import current_user, require_permission
from models.core import AttendanceTrendPoint, DeptProduction, ProductionPoint
from models.workspace import Alert, Event, Overview, OverviewKpis, StatusCount

router = APIRouter(prefix="/dashboard")


@router.get("/overview", response_model=Overview)
async def overview(cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "reports.company.read")
    company = await db.companies.find_one({"id": cid}, {"_id": 0}) or {}
    today = date.fromisoformat(today_iso())
    t = today.isoformat()
    orders = await order_summary(cid, t)

    emp_total = await db.employees.count_documents({"company_id": cid})
    att = count_attendance(await db.attendance.find({"company_id": cid, "date": t}, {"_id": 0}).to_list(5000), emp_total)

    machines = await db.machines.find({"company_id": cid}, {"_id": 0}).to_list(1000)
    ms = {s: sum(1 for m in machines if m["status"] == s) for s in ("running", "idle", "maintenance", "offline")}

    recs = await production_records(cid, (today - timedelta(days=13)).isoformat(), t)
    trend = [ProductionPoint(label=d, **sum_records(v).model_dump()) for d, v in sorted(group_by(recs, lambda r: r["date"]).items())]
    today_recs = [r for r in recs if r["date"] == t]
    tt = sum_records(today_recs)

    # ---- alerts (computed from records; every one links to its module) ----
    alerts: list[Alert] = []
    if orders.awaiting_approval:
        alerts.append(Alert(id="orders-approval", severity="warning", category="Orders",
                            title=f"{orders.awaiting_approval} orders awaiting approval",
                            detail="Offline order requests cannot enter production until approved.", link="/orders?status=awaiting_approval"))
    if orders.pending_review:
        alerts.append(Alert(id="orders-review", severity="info", category="Orders",
                            title=f"{orders.pending_review} online orders pending review",
                            detail="Waiting on payment, stock or customization checks.", link="/orders?status=pending_review"))
    if orders.overdue:
        alerts.append(Alert(id="orders-overdue", severity="critical", category="Orders",
                            title=f"{orders.overdue} orders past expected delivery",
                            detail="Not yet dispatched after the promised delivery date.", link="/orders?overdue=1"))
    for m in machines:
        if m["status"] == "offline":
            alerts.append(Alert(id=f"m-off-{m['code']}", severity="critical", category="Machines",
                                title=f"{m['code']} {m['name']} is offline", detail="Simulated status.", link="/machines"))
        elif m["status"] == "maintenance":
            alerts.append(Alert(id=f"m-mnt-{m['code']}", severity="warning", category="Machines",
                                title=f"{m['code']} under maintenance", detail=f"{m['name']} · {m['department']}", link="/machines"))
        elif m["next_maintenance"] <= (today + timedelta(days=3)).isoformat():
            alerts.append(Alert(id=f"m-due-{m['code']}", severity="warning", category="Maintenance",
                                title=f"{m['code']} maintenance due {m['next_maintenance']}",
                                detail=f"Health score {m['health']}%", link="/machines"))
    low = await db.inventory.find({"company_id": cid, "$expr": {"$lte": ["$quantity", "$reorder_level"]}}, {"_id": 0}).to_list(500)
    for i in low:
        crit = i["quantity"] < i["reorder_level"] * 0.5
        alerts.append(Alert(id=f"stock-{i['sku']}", severity="critical" if crit else "warning", category="Inventory",
                            title=f"Low stock: {i['name']}",
                            detail=f"{i['quantity']:g} {i['unit']} left · reorder at {i['reorder_level']:g}", link="/inventory"))
    unavailable = att.absent + att.leave
    if emp_total and unavailable / emp_total > 0.08:
        alerts.append(Alert(id="att-absent", severity="info", category="Attendance",
                            title=f"{unavailable} employees absent or on leave",
                            detail=f"{pct(unavailable, emp_total)}% of workforce unavailable today", link="/attendance"))
    if att.late:
        alerts.append(Alert(id="att-late", severity="info", category="Attendance", title=f"{att.late} late arrivals today",
                            detail="Checked in more than 15 minutes after shift start", link="/attendance"))
    order = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: order[a.severity])

    depts = [DeptProduction(department=d, **sum_records(v).model_dump())
             for d, v in sorted(group_by(today_recs, lambda r: r["department"]).items())]
    events = await db.events.find({"company_id": cid, "date": t}, {"_id": 0}).sort("start_time", 1).to_list(100)
    att_trend = await attendance_trend(cid, (today - timedelta(days=6)).isoformat(), t)

    return Overview(
        date=t, unit=company.get("production_unit", "units"), company_name=company.get("name", ""),
        owner_name=company.get("owner_name", ""),
        kpis=OverviewKpis(
            orders_today=orders.received_today, dispatched_today=orders.dispatched_today, pending_orders=orders.pending,
            overdue_orders=orders.overdue, employees_total=emp_total, employees_present=att.present,
            employees_absent=att.absent, employees_leave=att.leave, employees_late=att.late,
            machines_total=len(machines), machines_running=ms["running"], machines_idle=ms["idle"],
            machines_maintenance=ms["maintenance"], machines_offline=ms["offline"],
            production_today=tt.actual, production_target=tt.planned, achievement=tt.achievement,
            rejected_today=tt.rejected, efficiency_today=tt.efficiency,
        ),
        trend=trend, departments=depts,
        order_status=[StatusCount(status=s, count=getattr(orders, s)) for s in
                      ("awaiting_approval", "pending_review", "confirmed", "in_production", "quality_check", "ready_for_dispatch", "dispatched")],
        order_summary=orders,
        attendance_trend=[AttendanceTrendPoint(**p) for p in att_trend],
        alerts=alerts, today_events=[Event(**e) for e in events],
    )
