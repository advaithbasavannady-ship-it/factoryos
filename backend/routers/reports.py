from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from lib.analytics import group_by, pct, production_records, sum_records, week_start
from lib.dates import today_iso
from lib.db import db
from lib.tenant import get_company_id
from lib.authz import current_user, require_permission
from models.workspace import Report, ReportColumn, ReportType

router = APIRouter(prefix="/reports")


def C(*pairs: tuple[str, str]) -> list[ReportColumn]:
    return [ReportColumn(key=k, label=v) for k, v in pairs]


def T(**kv: str) -> list[ReportColumn]:
    return [ReportColumn(key=k.replace("_", " ").title(), label=v) for k, v in kv.items()]


def prod_row(label: str, recs: list[dict]) -> dict:
    t = sum_records(recs)
    return {"period": label, "planned": t.planned, "actual": t.actual, "rejected": t.rejected,
            "achievement": t.achievement, "efficiency": t.efficiency, "rejection_rate": t.rejection_rate}


PROD_COLS = C(("period", "Period"), ("planned", "Planned"), ("actual", "Actual"), ("rejected", "Rejected"),
              ("achievement", "Achievement %"), ("efficiency", "Efficiency %"), ("rejection_rate", "Rejection %"))


@router.get("/{rtype}", response_model=Report)
async def get_report(rtype: ReportType, start: Optional[str] = None, end: Optional[str] = None,
                     cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "reports.company.read")
    today = date.fromisoformat(today_iso())
    e = end or today.isoformat()
    s = start or (today - timedelta(days=29)).isoformat()
    if s > e:
        raise HTTPException(422, "start must be on or before end")
    company = await db.companies.find_one({"id": cid}, {"_id": 0}) or {}
    unit = company.get("production_unit", "units")
    base = {"type": rtype, "start": s, "end": e}

    if rtype in ("production_daily", "production_weekly", "performance_monthly"):
        recs = await production_records(cid, s, e)
        if rtype == "production_daily":
            rows = [prod_row(d, v) for d, v in sorted(group_by(recs, lambda r: r["date"]).items())]
            title, desc = "Daily Production Report", f"Planned vs actual output per day ({unit})."
        elif rtype == "production_weekly":
            rows = [prod_row(f"Week of {d}", v) for d, v in
                    sorted(group_by(recs, lambda r: week_start(date.fromisoformat(r["date"])).isoformat()).items())]
            title, desc = "Weekly Production Report", f"Output aggregated by week starting Monday ({unit})."
        else:
            rows = [prod_row(d, v) for d, v in sorted(group_by(recs, lambda r: r["date"][:7]).items())]
            orders = await db.orders.find({"company_id": cid, "order_date": {"$gte": s, "$lte": e}}, {"_id": 0}).to_list(10000)
            att = await db.attendance.find({"company_id": cid, "date": {"$gte": s, "$lte": e}}, {"_id": 0, "date": 1, "status": 1}).to_list(100000)
            o_by = group_by(orders, lambda o: o["order_date"][:7])
            a_by = group_by(att, lambda a: a["date"][:7])
            for r in rows:
                a = a_by.get(r["period"], [])
                r["orders"] = len(o_by.get(r["period"], []))
                r["attendance_rate"] = pct(sum(1 for x in a if x["status"] in ("present", "late")), len(a))
            title, desc = "Monthly Performance Report", "Production, orders and attendance by month."
        cols = PROD_COLS + (C(("orders", "Orders received"), ("attendance_rate", "Attendance %")) if rtype == "performance_monthly" else [])
        tot = sum_records(recs)
        return Report(**base, title=title, description=desc, columns=cols, rows=rows, chart_kind="bar", chart_x="period",
                      chart_series=C(("planned", "Planned"), ("actual", "Actual")),
                      totals=T(planned=f"{tot.planned:,}", actual=f"{tot.actual:,}", rejected=f"{tot.rejected:,}",
                               efficiency=f"{tot.efficiency}%"))

    if rtype == "attendance":
        emps = await db.employees.find({"company_id": cid}, {"_id": 0}).sort("emp_code", 1).to_list(5000)
        recs = await db.attendance.find({"company_id": cid, "date": {"$gte": s, "$lte": e}}, {"_id": 0}).to_list(100000)
        by_emp = group_by(recs, lambda r: r["employee_id"])
        rows = []
        for emp in emps:
            c: dict = defaultdict(int)
            for r in by_emp.get(emp["id"], []):
                c[r["status"]] += 1
            days = sum(c.values())
            rows.append({"emp_code": emp["emp_code"], "name": emp["name"], "department": emp["department"],
                         "present": c["present"] + c["late"], "late": c["late"], "absent": c["absent"], "leave": c["leave"],
                         "attendance_rate": pct(c["present"] + c["late"], days)})
        total = len(recs)
        present = sum(1 for r in recs if r["status"] in ("present", "late"))
        return Report(**base, title="Attendance Report", description="Days present, late, absent and on leave per employee.",
                      columns=C(("emp_code", "Code"), ("name", "Employee"), ("department", "Department"), ("present", "Present"),
                                ("late", "Late"), ("absent", "Absent"), ("leave", "Leave"), ("attendance_rate", "Attendance %")),
                      rows=rows, chart_kind="bar", chart_x="name",
                      chart_series=C(("present", "Present"), ("absent", "Absent"), ("leave", "Leave")),
                      totals=T(records=f"{total:,}", attendance_rate=f"{pct(present, total)}%",
                               late_arrivals=f"{sum(r['late'] for r in rows):,}"))

    if rtype == "orders":
        orders = await db.orders.find({"company_id": cid, "order_date": {"$gte": s, "$lte": e}}, {"_id": 0}).sort("order_date", 1).to_list(10000)
        rows = [{"order_no": o["order_no"], "order_date": o["order_date"], "source": o.get("source", "offline"),
                 "customer": o["customer"]["name"], "product": o["product"], "quantity": o["quantity"],
                 "value": round(o["quantity"] * o["unit_price"], 2), "paid": o.get("amount_paid", 0),
                 "balance": round(o["quantity"] * o["unit_price"] - o.get("amount_paid", 0), 2),
                 "status": o["status"].replace("_", " "), "produced": o.get("produced", 0), "qc_passed": o.get("qc_passed", 0),
                 "qc_rejected": o.get("qc_rejected", 0), "submitted_by": (o.get("submitted_by") or {}).get("name", ""),
                 "decided_by": (o.get("decided_by") or {}).get("name", ""), "expected_delivery": o["expected_delivery"],
                 "dispatched_date": o.get("dispatched_date") or ""} for o in orders]
        cur = company.get("currency", "USD")
        return Report(**base, title="Order Report", description="Orders received in the period, their source, payments, production and status.",
                      columns=C(("order_no", "Order"), ("order_date", "Order date"), ("source", "Source"), ("customer", "Customer"),
                                ("product", "Product"), ("quantity", "Qty"), ("value", f"Value ({cur})"), ("paid", f"Paid ({cur})"),
                                ("balance", f"Balance ({cur})"), ("status", "Status"), ("produced", "Produced"), ("qc_passed", "QC passed"),
                                ("qc_rejected", "QC rejected"), ("submitted_by", "Submitted by"), ("decided_by", "Approved/rejected by"),
                                ("expected_delivery", "Expected"), ("dispatched_date", "Dispatched")),
                      rows=rows, chart_kind="bar", chart_x="order_date", chart_series=C(("quantity", "Quantity")),
                      totals=T(orders=f"{len(rows):,}", units=f"{sum(r['quantity'] for r in rows):,}",
                               value=f"{sum(r['value'] for r in rows):,.0f}", paid=f"{sum(r['paid'] for r in rows):,.0f}"))

    if rtype == "machines":
        machines = await db.machines.find({"company_id": cid}, {"_id": 0}).sort("code", 1).to_list(1000)
        recs = group_by(await production_records(cid, s, e), lambda r: r["machine_id"])
        maint = group_by(await db.maintenance.find({"company_id": cid, "date": {"$gte": s, "$lte": e}}, {"_id": 0}).to_list(5000),
                         lambda m: m["machine_id"])
        rows = []
        for m in machines:
            t = sum_records(recs.get(m["id"], []))
            mm = maint.get(m["id"], [])
            rows.append({"code": m["code"], "name": m["name"], "department": m["department"], "status": m["status"],
                         "actual": t.actual, "rejected": t.rejected, "achievement": t.achievement,
                         "maintenance_events": len(mm), "maintenance_hours": round(sum(x["duration_hours"] for x in mm), 1),
                         "maintenance_cost": round(sum(x["cost"] for x in mm), 0), "health": m["health"]})
        return Report(**base, title="Machine Performance Report",
                      description="Output, maintenance and health per machine. Machine data is simulated.",
                      columns=C(("code", "Code"), ("name", "Machine"), ("department", "Department"), ("status", "Status (sim.)"),
                                ("actual", "Output"), ("rejected", "Rejected"), ("achievement", "Achievement %"),
                                ("maintenance_events", "Maint. events"), ("maintenance_hours", "Maint. hours"),
                                ("maintenance_cost", "Maint. cost"), ("health", "Health %")),
                      rows=rows, chart_kind="bar", chart_x="code", chart_series=C(("actual", "Output"), ("rejected", "Rejected")),
                      totals=T(machines=str(len(rows)), output=f"{sum(r['actual'] for r in rows):,}",
                               maintenance_hours=f"{sum(r['maintenance_hours'] for r in rows):,.1f}"))

    # inventory
    items = await db.inventory.find({"company_id": cid}, {"_id": 0}).sort("sku", 1).to_list(5000)
    cons = group_by(await db.consumption.find({"company_id": cid, "date": {"$gte": s, "$lte": e}}, {"_id": 0}).to_list(50000),
                    lambda c: c["item_id"])
    rows = [{"sku": i["sku"], "name": i["name"], "category": "Raw material" if i["category"] == "raw" else "Finished good",
             "quantity": i["quantity"], "unit": i["unit"], "reorder_level": i["reorder_level"],
             "value": round(i["quantity"] * i["unit_cost"], 2),
             "consumed": round(sum(c["quantity"] for c in cons.get(i["id"], [])), 1),
             "stock_status": "Low" if i["quantity"] <= i["reorder_level"] else "OK"} for i in items]
    return Report(**base, title="Inventory Report", description="Current stock, value and consumption in the period.",
                  columns=C(("sku", "SKU"), ("name", "Item"), ("category", "Category"), ("quantity", "In stock"), ("unit", "Unit"),
                            ("reorder_level", "Reorder level"), ("value", "Stock value"), ("consumed", "Consumed (period)"),
                            ("stock_status", "Status")),
                  rows=rows, chart_kind="bar", chart_x="sku", chart_series=C(("quantity", "In stock"), ("reorder_level", "Reorder level")),
                  totals=T(items=str(len(rows)), stock_value=f"{sum(r['value'] for r in rows):,.0f}",
                           low_stock=str(sum(1 for r in rows if r["stock_status"] == "Low"))))
